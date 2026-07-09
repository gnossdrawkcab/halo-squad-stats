"""Push notifications for the Halo tracker via ntfy.

Fired at the end of every scraper run (see stats.py). Detects notable events for
each tracked player from the freshly-updated database and pushes them to a
self-hosted ntfy topic. State is persisted to notify_state.json so each event is
sent exactly once.

Config (env):
  HALO_NTFY_ENABLED   "1"/"true" to enable (default enabled if a URL is set)
  HALO_NTFY_URL       base ntfy server URL (unset = ntfy notifications disabled)
  HALO_NTFY_TOPIC     topic to publish to (default "halo")
  HALO_SITE_URL       public site base URL for ntfy click-through links
  HALO_SESSION_GAP_MINUTES  minutes of inactivity that close a session (default 30)

Everything is best-effort: any failure is logged and swallowed so notifications
can never break or slow the scrape.
"""
import json
import logging
import os
import urllib.request
from datetime import datetime, timezone

from sqlalchemy import text

from halo_paths import data_path

logger = logging.getLogger("notify")

STATE_PATH = data_path("notify_state.json")

# CSR tier ladder (sub-tiers 1-6 within each, Onyx is a flat number).
STREAK_MILESTONES = (3, 5, 7, 10, 15, 20, 25, 30)


def _enabled() -> bool:
    flag = os.getenv("HALO_NTFY_ENABLED")
    if flag is not None:
        return flag.strip().lower() in ("1", "true", "yes", "on")
    # Default: enabled as long as a server URL is configured.
    return bool(_base_url())


def _base_url() -> str:
    return os.getenv("HALO_NTFY_URL", "").rstrip("/")


def _topic() -> str:
    return os.getenv("HALO_NTFY_TOPIC", "halo")


def _site_url() -> str:
    """Public site base URL used for ntfy click-through links (unset = no link)."""
    return os.getenv("HALO_SITE_URL", "").rstrip("/")


def _session_gap_minutes() -> int:
    try:
        return int(os.getenv("HALO_SESSION_GAP_MINUTES", "30"))
    except (TypeError, ValueError):
        return 30


def _publish(message: str, title: str = "", tags: str = "", priority: str = "", click: str = "") -> None:
    """POST a single notification to ntfy. ASCII-only headers (emoji go in tags)."""
    url = f"{_base_url()}/{_topic()}"
    data = message.encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    if title:
        req.add_header("Title", title.encode("ascii", "ignore").decode("ascii"))
    if tags:
        req.add_header("Tags", tags)
    if priority:
        req.add_header("Priority", priority)
    if click:
        req.add_header("Click", click)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            resp.read()
        logger.info("ntfy_sent title=%r", title)
    except Exception as e:
        logger.warning("ntfy_publish_failed title=%r error=%s", title, e)


def _load_state() -> dict:
    try:
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.warning("notify_state_write_failed error=%s", e)


def _to_float(v):
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


_CSR_TIERS = [("Bronze", 0), ("Silver", 300), ("Gold", 600),
              ("Platinum", 900), ("Diamond", 1200)]


def _csr_to_rank(csr) -> str | None:
    """Map a numeric CSR to a Halo Infinite rank label (Onyx is flat)."""
    val = _to_float(csr)
    if val is None or val <= 0:
        return None
    val = int(val)
    if val >= 1500:
        return f"Onyx {val}"
    for name, base in reversed(_CSR_TIERS):
        if val >= base:
            sub = min((val - base) // 50 + 1, 6)
            return f"{name} {sub}"
    return None


def _rank_label(row) -> str | None:
    """Human-readable rank. Prefer the stored tier snapshot; otherwise derive it
    from the post-match CSR (the tier columns are often null for recent rows)."""
    tier = row.get("current_csr_tier")
    if tier and str(tier).strip():
        tier = str(tier).strip()
        if tier.lower() == "onyx":
            val = _to_float(row.get("current_csr_value")) or _to_float(row.get("post_match_csr"))
            return f"Onyx {int(val)}" if val else "Onyx"
        sub = row.get("current_csr_sub_tier")
        try:
            return f"{tier} {int(sub)}"
        except (TypeError, ValueError):
            return tier
    # Fallback: derive from CSR value.
    return _csr_to_rank(row.get("current_csr_value") or row.get("post_match_csr"))


def _rank_key(label: str | None) -> str | None:
    """Comparison key for promotion/demotion. Collapses Onyx to a single bucket
    so we don't alert on every CSR point at Onyx (peak-CSR PBs cover that)."""
    if not label:
        return None
    return "Onyx" if label.lower().startswith("onyx") else label


def _fetch_player_matches(engine, gamertag: str, limit: int = 40):
    """Most-recent RANKED matches for a player, newest first."""
    sql = text(
        """
        SELECT match_id, date, kills, deaths, assists, kda, accuracy, outcome,
               post_match_csr, current_csr_value, current_csr_tier,
               current_csr_sub_tier, damage_dealt, damage_taken, map, playlist
        FROM halo_match_stats
        WHERE player_gamertag = :gt
          AND playlist ILIKE '%%ranked%%'
          AND date IS NOT NULL
        ORDER BY date DESC
        LIMIT :lim
        """
    )
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(sql, {"gt": gamertag, "lim": limit})]
    return rows


def _current_streak(matches) -> int:
    """Signed streak from newest matches: +N win streak, -N loss streak."""
    streak = 0
    for m in matches:  # newest first
        oc = str(m.get("outcome") or "").lower()
        if oc not in ("win", "loss"):
            break
        if streak == 0:
            streak = 1 if oc == "win" else -1
        elif (oc == "win") == (streak > 0):
            streak += 1 if streak > 0 else -1
        else:
            break
    return streak


def _player_events(engine, player, state) -> dict:
    """Detect events for one player; sends notifications and returns new state."""
    gamertag = player["gamertag"]
    matches = _fetch_player_matches(engine, gamertag)
    pstate = dict(state.get(gamertag, {}))
    if not matches:
        return pstate

    newest = matches[0]
    newest_id = str(newest.get("match_id"))

    # First time we see this player: snapshot a baseline silently (no alert spam).
    if not pstate:
        kdas = [_to_float(m.get("kda")) for m in matches if _to_float(m.get("kda")) is not None]
        csrs = [_to_float(m.get("post_match_csr")) for m in matches if _to_float(m.get("post_match_csr")) is not None]
        baseline_rank = _rank_label(newest)
        return {
            "last_match_id": newest_id,
            "rank": baseline_rank,
            "rank_key": _rank_key(baseline_rank),
            "best_kda": max(kdas) if kdas else None,
            "best_csr": max(csrs) if csrs else None,
            "streak_milestone": 0,
        }

    # Nothing new since last run → nothing to announce.
    if pstate.get("last_match_id") == newest_id:
        return pstate

    # Which matches are new (down to, but excluding, the last one we processed).
    last_id = pstate.get("last_match_id")
    new_matches = []
    for m in matches:
        if str(m.get("match_id")) == last_id:
            break
        new_matches.append(m)
    if not new_matches:
        new_matches = [newest]

    # --- Rank change (compare keys so Onyx point-churn doesn't spam) --------
    new_rank = _rank_label(newest)
    old_rank = pstate.get("rank")
    new_key = _rank_key(new_rank)
    old_key = pstate.get("rank_key") or _rank_key(old_rank)
    if new_key and old_key and new_key != old_key:
        up = _rank_is_up(old_key, new_key)
        arrow = "promoted to" if up else "dropped to"
        _publish(
            f"{gamertag} {arrow} {new_rank} (was {old_rank}).",
            title=f"{gamertag} rank {'up' if up else 'down'}",
            tags="chart_with_upwards_trend" if up else "chart_with_downwards_trend",
            priority="default",
        )

    # --- Streak milestone --------------------------------------------------
    streak = _current_streak(matches)
    mag = abs(streak)
    prev_milestone = int(pstate.get("streak_milestone") or 0)
    if mag in STREAK_MILESTONES and mag != prev_milestone:
        if streak > 0:
            _publish(
                f"{gamertag} is on a {mag}-game WIN streak! 🔥",
                title=f"{gamertag}: {mag}-win streak", tags="fire", priority="high",
            )
        else:
            _publish(
                f"{gamertag} has lost {mag} in a row. Time to regroup.",
                title=f"{gamertag}: {mag}-loss streak", tags="snowflake", priority="default",
            )
        pstate["streak_milestone"] = mag
    elif mag not in STREAK_MILESTONES:
        pstate["streak_milestone"] = 0

    # --- Personal bests (only on genuinely new matches) --------------------
    best_kda = _to_float(pstate.get("best_kda")) or float("-inf")
    best_csr = _to_float(pstate.get("best_csr")) or float("-inf")
    pb_kda_match = None
    pb_csr_val = None
    for m in new_matches:
        k = _to_float(m.get("kda"))
        if k is not None and k > best_kda:
            best_kda = k
            pb_kda_match = m
        c = _to_float(m.get("post_match_csr"))
        if c is not None and c > best_csr:
            best_csr = c
            pb_csr_val = c
    if pb_kda_match is not None:
        _publish(
            f"{gamertag} set a new KDA personal best: {best_kda:.2f} "
            f"on {pb_kda_match.get('map') or 'a match'}.",
            title=f"{gamertag}: KDA personal best", tags="trophy", priority="high",
        )
    if pb_csr_val is not None:
        _publish(
            f"{gamertag} hit a new peak CSR of {int(pb_csr_val)}!",
            title=f"{gamertag}: new peak CSR", tags="star", priority="high",
        )

    pstate.update({
        "last_match_id": newest_id,
        "rank": new_rank or old_rank,
        "rank_key": new_key or old_key,
        "best_kda": None if best_kda == float("-inf") else best_kda,
        "best_csr": None if best_csr == float("-inf") else best_csr,
    })
    return pstate


# Ordering for promotion/demotion detection.
_TIER_ORDER = {t: i for i, t in enumerate(
    ["bronze", "silver", "gold", "platinum", "diamond", "onyx"])}


def _rank_is_up(old_rank: str, new_rank: str) -> bool:
    def score(label):
        parts = label.split()
        tier = parts[0].lower()
        base = _TIER_ORDER.get(tier, 0) * 1000
        if tier == "onyx":
            try:
                return base + int(parts[1])
            except (IndexError, ValueError):
                return base
        try:
            return base + int(parts[1]) if len(parts) > 1 else base
        except ValueError:
            return base
    return score(new_rank) >= score(old_rank)


def _session_recap(engine, players, state) -> dict:
    """Once a session goes quiet (no games for SESSION_GAP), push a squad recap."""
    gap = _session_gap_minutes()
    sql = text(
        """
        SELECT player_gamertag, date, outcome, kda, post_match_csr, pre_match_csr
        FROM halo_match_stats
        WHERE playlist ILIKE '%%ranked%%' AND date IS NOT NULL
        ORDER BY date DESC
        LIMIT 400
        """
    )
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(sql)]
    if not rows:
        return state

    def as_dt(v):
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        try:
            d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    times = [as_dt(r["date"]) for r in rows]
    rows = [r for r, t in zip(rows, times) if t is not None]
    times = [t for t in times if t is not None]
    if not rows:
        return state

    last_match = times[0]
    now = datetime.now(timezone.utc)
    # Session still live (recent game) → wait.
    if (now - last_match).total_seconds() / 60.0 < gap:
        return state
    # Already recapped this session.
    if state.get("_last_recap_end") == last_match.isoformat():
        return state

    # Walk back through the squad timeline while gaps stay under the threshold.
    session_rows = [rows[0]]
    for i in range(1, len(rows)):
        if (times[i - 1] - times[i]).total_seconds() / 60.0 > gap:
            break
        session_rows.append(rows[i])

    if len(session_rows) < 2:
        state["_last_recap_end"] = last_match.isoformat()
        return state

    per_player = {}
    for r in session_rows:
        p = r["player_gamertag"]
        d = per_player.setdefault(p, {"w": 0, "l": 0, "kda": [], "csr": 0.0})
        oc = str(r.get("outcome") or "").lower()
        if oc == "win":
            d["w"] += 1
        elif oc == "loss":
            d["l"] += 1
        k = _to_float(r.get("kda"))
        if k is not None:
            d["kda"].append(k)
        post = _to_float(r.get("post_match_csr"))
        pre = _to_float(r.get("pre_match_csr"))
        if post is not None and pre is not None:
            d["csr"] += post - pre

    lines = []
    for p, d in sorted(per_player.items(), key=lambda kv: -(kv[1]["w"])):
        avg_kda = sum(d["kda"]) / len(d["kda"]) if d["kda"] else 0
        csr = f"{d['csr']:+.0f} CSR" if d["csr"] else ""
        lines.append(f"{p}: {d['w']}-{d['l']}, {avg_kda:.2f} KDA {csr}".strip())

    total_games = len({r["date"] for r in session_rows})
    _publish(
        "\n".join(lines),
        title=f"Session recap — {total_games} games",
        tags="checkered_flag", priority="default",
    )
    state["_last_recap_end"] = last_match.isoformat()
    return state


# ── Twitch "squad is live" alerts ──────────────────────────────────────────
# Mirrors the gamertag→channel map the webapp uses for the Live Now banner; kept
# in sync via the same HALO_TWITCH_CHANNELS override. Toggle HALO_STREAM_ALERTS.


def _twitch_channels() -> dict:
    """Gamertag → Twitch login map from HALO_TWITCH_CHANNELS (JSON object).
    Empty when unset — stream alerts are simply skipped."""
    raw = os.getenv("HALO_TWITCH_CHANNELS", "")
    if raw.strip():
        try:
            m = json.loads(raw)
            if isinstance(m, dict):
                return {str(k): str(v) for k, v in m.items()}
        except ValueError:
            logger.warning("HALO_TWITCH_CHANNELS not valid JSON — ignoring")
    return {}


def _fetch_twitch_live() -> dict:
    """Who's broadcasting — direct Twitch Helix when creds are set (no
    MultiTwitch needed), else the MultiTwitch /api/live proxy."""
    try:
        import twitch_live
        if twitch_live.creds_configured():
            return twitch_live.fetch_live_map(list(_twitch_channels().values()))
    except Exception as e:
        logger.warning("twitch_direct_failed error=%s", e)
    base = os.getenv("HALO_MULTITWITCH_API", "").rstrip("/")
    if not base:
        return {}
    try:
        req = urllib.request.Request(f"{base}/api/live", method="GET")
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("twitch_live_fetch_failed error=%s", e)
        return {}


def _stream_alert(state: dict) -> dict:
    """Push once when a tracked player's Twitch channel transitions to live."""
    if os.getenv("HALO_STREAM_ALERTS", "true").strip().lower() in ("0", "false", "no", "off"):
        return state
    channels = _twitch_channels()
    if not channels:
        return state
    livemap = _fetch_twitch_live()
    if not livemap:
        return state
    prev = set(state.get("_live_streams", []))
    now_live = set()
    for gt, ch in channels.items():
        info = livemap.get(ch) or livemap.get(ch.lower()) or {}
        if info.get("live"):
            now_live.add(gt)
    for gt in sorted(now_live - prev):
        ch = channels.get(gt, "")
        info = livemap.get(ch) or livemap.get(ch.lower()) or {}
        title = str(info.get("streamTitle") or "").strip()
        msg = f"{gt} just went LIVE on Twitch" + (f": {title}" if title else "") + "."
        _publish(msg, title=f"{gt} is LIVE", tags="red_circle", priority="high")
        # Also web-push to installed PWAs (fires even when the app is closed).
        try:
            import push
            push.send_push(f"{gt} is LIVE", msg, "/live", tag="halo-live")
        except Exception as e:
            logger.warning("webpush_live_failed error=%s", e)
    state["_live_streams"] = sorted(now_live)
    return state


def _compose_game_summary(mrows, record, game_no) -> tuple[str, str]:
    """(title, body) for one finished game. mrows = halo_match_stats rows for
    every tracked player in that match. Pure — unit-testable."""
    first = mrows[0]
    oc = str(first.get("outcome") or "").lower()
    icon, word = ("✅", "WIN") if oc == "win" else (("🤝", "TIE") if oc == "tie" else ("❌", "LOSS"))
    mode = str(first.get("game_type") or "").split(":")[0].strip() or "Ranked"
    title = f"{icon} {word} · {mode} on {first.get('map') or '?'}"
    lines = []
    try:
        from grades import compute_match_grade
    except Exception:                       # pragma: no cover - defensive
        compute_match_grade = None
    for r in sorted(mrows, key=lambda x: _to_float(x.get("kda")) or 0, reverse=True):
        k, d, a = int(_to_float(r.get("kills")) or 0), int(_to_float(r.get("deaths")) or 0), int(_to_float(r.get("assists")) or 0)
        bits = [f"{k}/{d}/{a}"]
        acc = _to_float(r.get("accuracy"))
        if acc:
            bits.append(f"{acc * 100 if acc <= 1 else acc:.0f}% acc")
        dd = _to_float(r.get("dmg_difference"))
        if dd is not None:
            bits.append(f"{dd:+,.0f} dmg")
        pre, post = _to_float(r.get("pre_match_csr")), _to_float(r.get("post_match_csr"))
        if post and post > 0:
            delta = f" ({post - pre:+.0f})" if pre and pre > 0 else ""
            bits.append(f"CSR {post:.0f}{delta}")
        label = str(r.get('player_gamertag'))
        if compute_match_grade:
            try:
                g = compute_match_grade(
                    kda=_to_float(r.get("kda")), accuracy=_to_float(r.get("accuracy")),
                    dmg_dealt=r.get("damage_dealt"), dmg_taken=r.get("damage_taken"),
                    outcome=r.get("outcome"))
            except Exception:
                g = None
            if g and g.get("grade"):        # same absolute grade the site shows
                label += f" [{g['grade']}]"
        lines.append(f"{label}: {' · '.join(bits)}")
    if record:
        lines.append(f"Tonight: {record}" + (f" · game {game_no}" if game_no else ""))
    return title, "\n".join(lines)


def _game_summaries(engine, state) -> dict:
    """Live look: one push after EACH new ranked game with every tracked
    player's line + the session record so far. Freshness-gated (45 min) so
    backfills and long-downtime restarts never replay a flood.

    Mirrors the site's stat-tracking rules (Ranked Arena only, no DNF/left
    outcomes, no voided short "ties" from a teammate quitting) so a game the
    dashboards don't count is never announced — let alone called a loss."""
    if os.getenv("HALO_GAME_NOTIFY", "1").lower() in ("0", "false", "no"):
        return state
    sql = text(
        """
        SELECT player_gamertag, match_id, date, map, game_type, outcome,
               kills, deaths, assists, kda, accuracy, dmg_difference,
               damage_dealt, damage_taken,
               pre_match_csr, post_match_csr
        FROM halo_match_stats
        WHERE playlist ILIKE 'Ranked Arena' AND date IS NOT NULL
          AND date > NOW() - INTERVAL '45 minutes'
          AND LOWER(outcome) NOT IN ('dnf', 'left')
          AND NOT (LOWER(outcome) = 'tie'
                   AND COALESCE(kills, 0) <= 1 AND COALESCE(duration, 0) < 120)
        ORDER BY date
        """
    )
    with engine.connect() as conn:
        fresh = [dict(r._mapping) for r in conn.execute(sql)]
    if not fresh:
        return state
    seen = set(str(x) for x in (state.get("_game_notified") or []))
    by_mid: dict = {}
    for r in fresh:
        by_mid.setdefault(str(r["match_id"]), []).append(r)

    def _pair(mid, r):
        return f"{mid}|{r.get('player_gamertag')}"

    # Per-(match, player) dedup: only a player's rows we haven't announced yet.
    # A bare match_id in `seen` is legacy (old whole-match dedup) — skip that
    # whole match so a redeploy doesn't replay history.
    new_by_mid: dict = {}
    new_keys: list = []
    for mid, rows in by_mid.items():
        if mid in seen:
            continue
        fresh_rows = [r for r in rows if _pair(mid, r) not in seen]
        if fresh_rows:
            new_by_mid[mid] = fresh_rows
            new_keys.extend(_pair(mid, r) for r in fresh_rows)
    if not new_by_mid:
        return state

    # Session-so-far record: walk today's games back over the session gap.
    gap = _session_gap_minutes()
    sql_rec = text(
        """
        SELECT DISTINCT ON (match_id) match_id, date, outcome
        FROM halo_match_stats
        WHERE playlist ILIKE 'Ranked Arena' AND date IS NOT NULL
          AND date > NOW() - INTERVAL '18 hours'
          AND LOWER(outcome) NOT IN ('dnf', 'left')
          AND NOT (LOWER(outcome) = 'tie'
                   AND COALESCE(kills, 0) <= 1 AND COALESCE(duration, 0) < 120)
        ORDER BY match_id, date DESC
        """
    )
    with engine.connect() as conn:
        rec_rows = sorted((dict(r._mapping) for r in conn.execute(sql_rec)),
                          key=lambda r: r["date"], reverse=True)
    session_rows, prev = [], None
    for r in rec_rows:
        if prev is not None and (prev - r["date"]).total_seconds() / 60 > gap:
            break
        session_rows.append(r)
        prev = r["date"]
    wins = sum(1 for r in session_rows if str(r.get("outcome") or "").lower() == "win")
    losses = sum(1 for r in session_rows if str(r.get("outcome") or "").lower() == "loss")
    record = f"{wins}-{losses}" if session_rows else ""
    game_no = len(session_rows)

    for mid in sorted(new_by_mid, key=lambda m: new_by_mid[m][0]["date"]):
        try:
            title, body = _compose_game_summary(new_by_mid[mid], record, game_no)
            # Click-through lands on the right dashboard: solo game → Solo Dash,
            # squad game → Squad Dash (?stay=1 skips the streaming→/live redirect).
            solo = len({r.get("player_gamertag") for r in by_mid[mid]}) <= 1
            dash = "/solo" if solo else "/?stay=1"
            site = _site_url()
            _publish(body, title=title, tags="video_game",
                     priority="default", click=(site + dash) if site else "")
            try:
                import push
                push.send_push(title, body, dash, tag="halo-game")
            except Exception as e:
                logger.warning("webpush_game_failed error=%s", e)
        except Exception as e:
            logger.warning("notify_game_compose_failed match=%s error=%s", mid, e)
    state["_game_notified"] = (list(seen) + new_keys)[-600:]
    return state


def process_notifications(engine, players) -> None:
    """Entry point called by the scraper after each run. Never raises."""
    if not _enabled():
        return
    try:
        state = _load_state()
        try:
            state = _game_summaries(engine, state)
        except Exception as e:
            logger.warning("notify_game_summary_failed error=%s", e)
        for player in players:
            try:
                state[player["gamertag"]] = _player_events(engine, player, state)
            except Exception as e:
                logger.warning("notify_player_failed gamertag=%s error=%s",
                               player.get("gamertag"), e)
        try:
            state = _session_recap(engine, players, state)
        except Exception as e:
            logger.warning("notify_session_recap_failed error=%s", e)
        try:
            state = _stream_alert(state)
        except Exception as e:
            logger.warning("notify_stream_alert_failed error=%s", e)
        _save_state(state)
    except Exception as e:
        logger.warning("notify_process_failed error=%s", e)
