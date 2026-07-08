"""Xbox Live presence poller — who is in Halo Infinite RIGHT NOW.

The scraper only learns about a session after the first match finishes and
gets scraped; presence knows the moment the game boots. This module runs as
a daemon thread inside the long-lived entrypoint process, polls the Xbox
presence API with the same tokens the scraper already refreshes, and:

  * writes a snapshot to <data>/presence.json for the webapp (live page
    chips, dashboard banner, freshness fingerprint),
  * appends state transitions to halo_presence_log (future playtime /
    menus-vs-match analytics),
  * pushes a "squad forming" notification (ntfy + web push) when a second
    player boots Halo, and one per extra player who joins the episode.

Everything is best-effort: no tokens, no DB, no network — the loop just
idles and retries. Config:

  HALO_PRESENCE                1 (default) / 0 to disable
  HALO_PRESENCE_POLL_SECONDS   poll cadence, default 75
  HALO_PRESENCE_NOTIFY         1 (default) / 0 to silence forming pushes
"""
import json
import logging
import os
import threading
import time

import requests

from halo_paths import data_path

logger = logging.getLogger(__name__)

HALO_TITLE_ID = "1777860928"          # Halo Infinite's Xbox title id
PRESENCE_FILE = data_path("presence.json")
POLL_SECONDS = max(30, int(os.getenv("HALO_PRESENCE_POLL_SECONDS", "75")))
REQUEST_TIMEOUT = 20

_state_lock = threading.Lock()


# ── roster ──────────────────────────────────────────────────────────────────
def _engine():
    """Light DB engine (transition log + roster); None if unconfigured."""
    try:
        from sqlalchemy import create_engine
        host = os.getenv("HALO_DB_HOST")
        if not host:
            return None
        url = (f"postgresql+psycopg2://{os.getenv('HALO_DB_USER', 'postgres')}:"
               f"{os.getenv('HALO_DB_PASSWORD', '')}@{host}:"
               f"{os.getenv('HALO_DB_PORT', '5432')}/{os.getenv('HALO_DB_NAME', 'halodb')}")
        return create_engine(url, pool_pre_ping=True)
    except Exception:
        return None


_roster_cache: dict = {"ts": 0.0, "players": []}


def _roster(engine) -> list[dict]:
    """Tracked players as [{'gamertag','xuid'}]. Prefers HALO_TRACKED_PLAYERS,
    falls back to the tracked names already in the DB (env-free, works in
    both trees without baking gamertags into source)."""
    now = time.time()
    if _roster_cache["players"] and now - _roster_cache["ts"] < 3600:
        return _roster_cache["players"]
    players: list[dict] = []
    raw = os.getenv("HALO_TRACKED_PLAYERS", "").strip()
    if raw:
        try:
            players = [p for p in json.loads(raw) if p.get("xuid")]
        except Exception:
            players = []
    if not players and engine is not None:
        try:
            from sqlalchemy import text
            with engine.connect() as conn:
                rows = conn.execute(text(
                    "SELECT DISTINCT player_gamertag, player_xuid "
                    "FROM halo_match_stats WHERE player_xuid IS NOT NULL"
                )).fetchall()
            players = [{"gamertag": r[0], "xuid": str(r[1])} for r in rows if r[0] and r[1]]
        except Exception as exc:
            logger.warning("presence roster query failed: %s", exc)
    if players:
        _roster_cache.update(ts=now, players=players)
    return players


# ── token chain (mirrors auth.py, but for the xboxlive.com relying party) ──
_xbl: dict = {"uhs": "", "xsts": "", "expires": 0.0}


def _mint_xbl_tokens() -> bool:
    try:
        import auth
        toks = json.load(open(auth.TOKEN_FILE))
        fresh = auth.refresh_tokens(toks["refresh_token"])
        access = fresh["access_token"]
        r = requests.post("https://user.auth.xboxlive.com/user/authenticate", json={
            "RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT",
            "Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com",
                           "RpsTicket": f"d={access}"}}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        ut = r.json()["Token"]
        r = requests.post("https://xsts.auth.xboxlive.com/xsts/authorize", json={
            "RelyingParty": "http://xboxlive.com", "TokenType": "JWT",
            "Properties": {"SandboxId": "RETAIL", "UserTokens": [ut]}}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        d = r.json()
        _xbl.update(uhs=d["DisplayClaims"]["xui"][0]["uhs"], xsts=d["Token"],
                    expires=time.time() + 6 * 3600)  # XSTS lives longer; stay safe
        return True
    except Exception as exc:
        logger.warning("presence token mint failed: %s", exc)
        return False


def _fetch_presence(xuids: list[str]) -> list[dict] | None:
    if time.time() > _xbl["expires"] and not _mint_xbl_tokens():
        return None
    r = requests.post("https://userpresence.xboxlive.com/users/batch",
                      headers={"Authorization": f"XBL3.0 x={_xbl['uhs']};{_xbl['xsts']}",
                               "x-xbl-contract-version": "3", "Accept-Language": "en-US"},
                      json={"users": xuids, "level": "all"}, timeout=REQUEST_TIMEOUT)
    if r.status_code == 401:           # stale XSTS — re-mint once and retry
        if not _mint_xbl_tokens():
            return None
        r = requests.post("https://userpresence.xboxlive.com/users/batch",
                          headers={"Authorization": f"XBL3.0 x={_xbl['uhs']};{_xbl['xsts']}",
                                   "x-xbl-contract-version": "3", "Accept-Language": "en-US"},
                          json={"users": xuids, "level": "all"}, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _halo_detail(user: dict) -> tuple[bool, str, bool]:
    """(in_halo, rich-presence detail, online) for one presence record."""
    online = str(user.get("state") or "").lower() == "online"
    for dev in (user.get("devices") or []):
        for t in (dev.get("titles") or []):
            if str(t.get("id") or "") == HALO_TITLE_ID or t.get("name") == "Halo Infinite":
                if str(t.get("state") or "").lower() != "active":
                    continue
                act = (t.get("activity") or {}).get("richPresence", "") or ""
                return True, act, online
    return False, "", online


# ── transitions: DB log + squad-forming notifications ───────────────────────
def _ensure_log_table(engine) -> None:
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS halo_presence_log (
                ts        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                gamertag  TEXT NOT NULL,
                in_halo   BOOLEAN NOT NULL,
                detail    TEXT
            )
            """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_hpl_ts ON halo_presence_log (ts)"))


def _log_transitions(engine, changes: list[dict]) -> None:
    if engine is None or not changes:
        return
    try:
        from sqlalchemy import text
        _ensure_log_table(engine)
        with engine.begin() as conn:
            for c in changes:
                conn.execute(text(
                    "INSERT INTO halo_presence_log (gamertag, in_halo, detail) "
                    "VALUES (:g, :i, :d)"),
                    {"g": c["gamertag"], "i": c["in_halo"], "d": c.get("detail") or ""})
    except Exception as exc:
        logger.warning("presence log failed: %s", exc)


def _notify_forming(prev_in: set, now_in: set, episode: dict) -> dict:
    """Squad-forming pushes. episode = {'formed': bool, 'notified': [names]}
    persists in presence.json so restarts don't re-announce."""
    if os.getenv("HALO_PRESENCE_NOTIFY", "1").lower() in ("0", "false", "no"):
        return episode
    try:
        import notify
        import push
    except Exception:
        return episode
    site = ""
    try:
        site = notify._site_url()
    except Exception:
        site = ""
    click = (site + "/live") if site else ""

    if len(now_in) >= 2 and not episode.get("formed"):
        names = " + ".join(sorted(now_in))
        title = "🎮 Squad forming"
        body = f"{names} are on Halo right now"
        notify._publish(body, title=title, tags="video_game", priority="default", click=click)
        try:
            push.send_push(title, body, "/live", tag="halo-presence")
        except Exception:
            pass
        return {"formed": True, "notified": sorted(now_in)}

    if episode.get("formed") and len(now_in) >= 2:
        known = set(episode.get("notified") or [])
        fresh = sorted(now_in - known)
        for name in fresh:
            body = f"{name} joined — {len(now_in)} on Halo now"
            notify._publish(body, title="🎮 Squad growing", tags="video_game",
                            priority="default", click=click)
            try:
                push.send_push("🎮 Squad growing", body, "/live", tag="halo-presence")
            except Exception:
                pass
        if fresh:
            return {"formed": True, "notified": sorted(known | now_in)}
        return episode

    if len(now_in) < 2:
        return {"formed": False, "notified": []}
    return episode


# ── snapshot ────────────────────────────────────────────────────────────────
def _load_snapshot() -> dict:
    try:
        with open(PRESENCE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _write_snapshot(snap: dict) -> None:
    tmp = str(PRESENCE_FILE) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snap, f)
    os.replace(tmp, PRESENCE_FILE)


def poll_once(engine) -> None:
    players = _roster(engine)
    if not players:
        return
    data = _fetch_presence([str(p["xuid"]) for p in players])
    if data is None:
        return
    by_xuid = {str(u.get("xuid")): u for u in data}
    prev = _load_snapshot()
    prev_players = prev.get("players") or {}
    now = time.time()

    out, changes = {}, []
    for p in players:
        gt, xuid = p.get("gamertag"), str(p["xuid"])
        u = by_xuid.get(xuid) or {}
        in_halo, detail, online = _halo_detail(u)
        was = prev_players.get(gt) or {}
        since = was.get("since") if was.get("in_halo") == in_halo else now
        out[gt] = {"online": online, "in_halo": in_halo,
                   "detail": detail, "since": since or now}
        if bool(was.get("in_halo")) != in_halo or (in_halo and was.get("detail") != detail):
            changes.append({"gamertag": gt, "in_halo": in_halo, "detail": detail})

    prev_in = {g for g, v in (prev_players or {}).items() if v.get("in_halo")}
    now_in = {g for g, v in out.items() if v.get("in_halo")}
    episode = _notify_forming(prev_in, now_in, prev.get("episode") or {})
    _log_transitions(engine, changes)
    _write_snapshot({"updated": now, "poll_seconds": POLL_SECONDS,
                     "players": out, "episode": episode})


def run_forever() -> None:
    engine = None
    while True:
        try:
            if not os.path.exists(data_path("tokens.json")):
                time.sleep(300)
                continue
            if engine is None:
                engine = _engine()
            poll_once(engine)
        except Exception as exc:
            logger.warning("presence poll failed: %s", exc)
        time.sleep(POLL_SECONDS)


def start_presence_thread() -> None:
    if os.getenv("HALO_PRESENCE", "1").lower() in ("0", "false", "no"):
        print("presence poller disabled (HALO_PRESENCE=0)")
        return
    t = threading.Thread(target=run_forever, name="presence", daemon=True)
    t.start()
    print(f"🎮 presence poller started (every {POLL_SECONDS}s)")
