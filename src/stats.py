import json
import asyncio
import logging
import os
import time
from datetime import datetime, timezone as dt_timezone
from aiohttp import ClientSession, ClientResponseError
from spnkr.client import HaloInfiniteClient
import pandas as pd
from pytz import timezone
import random
from sqlalchemy import create_engine, inspect, text
from halo_paths import data_path

LOG_LEVEL = os.getenv("HALO_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

update_status = {
    "new_rows_added": False,
    "new_row_count": 0,
    "last_update": None
}

TOKENS_PATH = data_path("tokens.json")
UPDATE_STATUS_PATH = data_path("update_status.json")
SETTINGS_PATH = data_path("settings.json")

# Halo Infinite rank ladder (50-CSR sub-tiers within each tier; Onyx is flat).
_CSR_TIER_BANDS = [("Bronze", 0), ("Silver", 300), ("Gold", 600),
                   ("Platinum", 900), ("Diamond", 1200)]


def csr_to_tier(csr):
    """Map a numeric CSR to (tier_name, sub_tier). Onyx (>=1500) has sub_tier None."""
    try:
        val = int(float(csr))
    except (TypeError, ValueError):
        return (None, None)
    if val <= 0:
        return (None, None)
    if val >= 1500:
        return ("Onyx", None)
    for name, base in reversed(_CSR_TIER_BANDS):
        if val >= base:
            return (name, min((val - base) // 50 + 1, 6))
    return (None, None)
DB_NAME = os.getenv("HALO_DB_NAME", "halostatsapi")
DB_USER = os.getenv("HALO_DB_USER", "postgres")
DB_PASSWORD = os.getenv("HALO_DB_PASSWORD")
DB_HOST = os.getenv("HALO_DB_HOST", "halostatsapi")
DB_PORT = os.getenv("HALO_DB_PORT", "5432")

_RUNTIME_SETTINGS_CACHE: dict = {"mtime": None, "settings": {}}
_LAST_LOGGED_MATCH_LIMIT: int | None = None


def load_runtime_settings() -> dict:
    """Load settings.json with a lightweight mtime cache.

    This is used by the scraper so Settings page changes can apply mid-run
    without restarting the container.
    """

    if not SETTINGS_PATH.exists():
        _RUNTIME_SETTINGS_CACHE["mtime"] = None
        _RUNTIME_SETTINGS_CACHE["settings"] = {}
        return {}

    try:
        mtime = SETTINGS_PATH.stat().st_mtime
    except Exception:
        mtime = None

    if _RUNTIME_SETTINGS_CACHE.get("mtime") == mtime and isinstance(
        _RUNTIME_SETTINGS_CACHE.get("settings"), dict
    ):
        return _RUNTIME_SETTINGS_CACHE["settings"]

    try:
        with open(SETTINGS_PATH, "r") as f:
            settings = json.load(f)
        if not isinstance(settings, dict):
            settings = {}
    except Exception:
        settings = {}

    _RUNTIME_SETTINGS_CACHE["mtime"] = mtime
    _RUNTIME_SETTINGS_CACHE["settings"] = settings
    return settings

def get_match_limit():
    """Load match limit from settings file or fall back to environment variable.
    
    This controls how many matches to scan per API call, but the system will
    automatically fetch all historical matches until the database is complete.
    """
    default_limit = int(os.getenv("HALO_MATCH_LIMIT", "500"))

    settings = load_runtime_settings()
    chosen = settings.get("match_limit", default_limit)
    try:
        chosen_int = int(chosen)
    except Exception:
        chosen_int = default_limit

    # Interpret 0 or negative as unlimited
    unlimited = False
    if chosen_int <= 0:
        unlimited = True
        chosen_int = None

    global _LAST_LOGGED_MATCH_LIMIT
    if _LAST_LOGGED_MATCH_LIMIT != chosen_int:
        source = "settings.json" if "match_limit" in settings else "env HALO_MATCH_LIMIT"
        if unlimited:
            logger.info("match_limit source=%s value=unlimited", source)
        else:
            logger.info("match_limit source=%s value=%s", source, chosen_int)
        _LAST_LOGGED_MATCH_LIMIT = chosen_int

    return chosen_int

def get_update_interval():
    """Load update interval from settings or env (seconds)."""
    default_interval = int(os.getenv("HALO_UPDATE_INTERVAL", "120"))

    settings = load_runtime_settings()
    chosen = settings.get("update_interval", default_interval)
    try:
        chosen_int = int(chosen)
    except Exception:
        chosen_int = default_interval

    return chosen_int


TEXT_COLUMNS = {
    "player_gamertag",
    "player_xuid",
    "match_id",
    "playlist",
    "playlist_id",
    "map",
    "game_type",
    "outcome",
    "raw_json",
    # CSR tier *names* are strings ("Diamond", "Onyx", ...). They were defaulting
    # to DOUBLE and silently coercing to NULL on insert — keep them TEXT.
    "current_csr_tier",
    "current_csr_next_tier",
    "season_max_csr_tier",
    "season_max_csr_next_tier",
    "all_time_max_csr_tier",
    "all_time_max_csr_next_tier",
    # Season the match was played in (matchmade only); string id like "Csr/Seasons/...".
    "season_id",
}

# Tier-name columns that may already exist as DOUBLE in older DBs and need a
# one-time ALTER ... TYPE TEXT (see ensure_schema).
_TIER_NAME_COLUMNS = [
    "current_csr_tier", "current_csr_next_tier",
    "season_max_csr_tier", "season_max_csr_next_tier",
    "all_time_max_csr_tier", "all_time_max_csr_next_tier",
]

EXTRA_COLUMNS = [
    # Stores the un-normalized match payload (API-derived fields) as JSON text
    "raw_json",
    # Best-effort timestamp for when the row was scraped
    "scraped_at",
]


def all_db_columns() -> list[str]:
    # Keep stable ordering: FINAL_COLUMNS first, then any extras.
    cols = list(FINAL_COLUMNS)
    for col in EXTRA_COLUMNS:
        if col not in cols:
            cols.append(col)
    return cols


def _quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _column_sql_type(col: str) -> str:
    # Preserve the current data model behavior:
    # - TEXT_COLUMNS stay as text
    # - date/scraped_at are timestamps
    # - everything else is stored as DOUBLE PRECISION (pandas will coerce)
    if col in ("date", "scraped_at"):
        return "TIMESTAMPTZ"
    if col in TEXT_COLUMNS:
        return "TEXT"
    return "DOUBLE PRECISION"


def ensure_schema(engine) -> None:
    """Create the halo_match_stats table with a stable, explicit schema.

    This avoids schema drift from pandas' inferred types and also ensures
    that columns with special characters (e.g. dmg/ka) are consistently
    present across deployments.
    """

    cols = all_db_columns()
    col_defs = [f"{_quote_ident(col)} {_column_sql_type(col)}" for col in cols]

    ddl = "CREATE TABLE IF NOT EXISTS halo_match_stats (\n  " + ",\n  ".join(col_defs) + "\n);"
    with engine.begin() as conn:
        conn.execute(text(ddl))

    # If the table already existed (e.g. older deployments), add any missing columns.
    try:
        existing_cols = {c.get("name") for c in inspect(engine).get_columns("halo_match_stats")}
        desired_cols = set(cols)
        missing = [c for c in cols if c not in existing_cols]
        if missing:
            with engine.begin() as conn:
                for col in missing:
                    conn.execute(
                        text(
                            f"ALTER TABLE halo_match_stats ADD COLUMN IF NOT EXISTS {_quote_ident(col)} {_column_sql_type(col)}"
                        )
                    )
    except Exception as exc:
        logger.warning("schema_reconcile_failed error=%s", exc)

    # One-time fix: tier-NAME columns created as DOUBLE in older deployments can
    # never hold "Diamond"/"Onyx" (the API value coerces to NULL). Convert any
    # such column to TEXT. Idempotent — skips columns already TEXT.
    try:
        col_types = {c.get("name"): str(c.get("type")).upper()
                     for c in inspect(engine).get_columns("halo_match_stats")}
        with engine.begin() as conn:
            for col in _TIER_NAME_COLUMNS:
                t = col_types.get(col)
                if t and "TEXT" not in t and "CHAR" not in t:
                    conn.execute(text(
                        f"ALTER TABLE halo_match_stats "
                        f"ALTER COLUMN {_quote_ident(col)} TYPE TEXT "
                        f"USING {_quote_ident(col)}::text"
                    ))
                    logger.info("csr_tier_column_retyped col=%s from=%s", col, t)
    except Exception as exc:
        logger.warning("tier_column_retype_failed error=%s", exc)

    # Ensure indexes + KV schema after table exists.
    ensure_indexes(engine)
    ensure_kv_schema(engine)
    ensure_match_players_schema(engine)


def ensure_match_players_schema(engine) -> None:
    """Per-(match, player) lightweight scoreboard rows for ALL players in
    matches a tracked player appeared in — powers the opponent/nemesis views
    and the full match scoreboard. Populated going forward by the scraper."""
    with engine.begin() as conn:
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS halo_match_players (
                match_id     TEXT NOT NULL,
                player_xuid  TEXT NOT NULL,
                gamertag     TEXT,
                team_id      INTEGER,
                outcome      TEXT,
                is_tracked   BOOLEAN DEFAULT FALSE,
                kills        DOUBLE PRECISION,
                deaths       DOUBLE PRECISION,
                assists      DOUBLE PRECISION,
                kda          DOUBLE PRECISION,
                accuracy     DOUBLE PRECISION,
                damage_dealt DOUBLE PRECISION,
                csr          DOUBLE PRECISION,
                match_date   TIMESTAMPTZ,
                playlist     TEXT,
                map          TEXT,
                scraped_at   TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (match_id, player_xuid)
            );
            """
        ))
        # CSR per player per match (post-match rank recap; -1 = ranked but no
        # recap available, NULL = not yet captured / non-ranked). Powers the
        # average-opponent-CSR / strength-of-schedule views.
        conn.execute(text("ALTER TABLE halo_match_players ADD COLUMN IF NOT EXISTS csr DOUBLE PRECISION"))
        # Bot flag (#5) — ranked arena is human-only, but capture every lobby
        # honestly so bots never pollute opponent/nemesis/CSR averages.
        conn.execute(text("ALTER TABLE halo_match_players ADD COLUMN IF NOT EXISTS is_bot BOOLEAN DEFAULT FALSE"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_hmp_xuid ON halo_match_players (player_xuid)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_hmp_match ON halo_match_players (match_id)"))


async def resolve_gamertags(client, xuids):
    """Resolve xuid -> gamertag for unknown opponents via the profile API,
    caching results. Best-effort: unknowns fall back to a Spartan-NNNN label."""
    tracked = tracked_by_xuid()
    unknown = [x for x in xuids if x not in _XUID_NAME_CACHE and x not in tracked]
    if unknown:
        try:
            resp = await client.profile.get_users_by_id(unknown)
            users = await resp.parse()
            for u in (users or []):
                xid = clean_xuid(get_or_default(u, 'xuid', default='') or '')
                gt = get_or_default(u, 'gamertag', default='') or ''
                if xid and gt:
                    _XUID_NAME_CACHE[xid] = gt
        except Exception as exc:
            logger.warning("gamertag_resolve_failed count=%s error=%s", len(unknown), exc)
    out = {}
    for x in xuids:
        out[x] = (tracked.get(x) or _XUID_NAME_CACHE.get(x)
                  or f"Spartan-{str(x)[-4:]}")
    return out


async def resolve_xuids_for_gamertags(gamertags):
    """Resolve gamertag -> xuid via the profile API (used by the /setup page).

    Returns {gamertag: xuid} for every tag that resolved; missing keys mean the
    lookup failed (bad tag, no tokens yet, API hiccup) and the caller should ask
    for a manual XUID. Requires a valid tokens.json (run the OAuth flow first).
    """
    tokens = load_tokens()
    if not tokens or not tokens.get("spartan_token"):
        logger.warning("xuid_resolve_skipped reason=no_tokens")
        return {}
    out = {}
    async with ClientSession() as session:
        client = HaloInfiniteClient(
            session=session,
            spartan_token=tokens["spartan_token"],
            clearance_token=tokens.get("clearance_token") or "",
        )
        for gt in gamertags:
            try:
                resp = await client.profile.get_user_by_gamertag(gt)
                user = await resp.parse()
                xid = clean_xuid(get_or_default(user, "xuid", default="") or "")
                if xid:
                    out[gt] = xid
            except Exception as exc:
                logger.warning("xuid_resolve_failed gamertag=%s error=%s", gt, exc)
    return out


async def capture_match_players(client, engine, match_id, match_stats,
                                match_date, playlist, map_name):
    """Upsert a lightweight scoreboard row for every player in the match."""
    if not match_id or match_id in _CAPTURED_MATCH_PLAYERS:
        return
    rows = []
    xuids = []
    for player in get_or_default(match_stats, 'players', default=[]) or []:
        xuid = clean_xuid(get_or_default(player, 'player_id', default='') or '')
        if not xuid:
            continue
        outcome = get_or_default(player, 'outcome', default='')
        outcome = OUTCOMES.get(outcome, str(outcome)) if str(outcome).isdigit() else str(outcome)
        team_id = get_or_default(player, 'last_team_id', default=0)
        k = d = a = acc = dmg = 0.0
        pts = get_or_default(player, 'player_team_stats', default=[]) or []
        if pts:
            stats = get_or_default(pts[0], 'stats')
            core = get_or_default(stats, 'core_stats') if stats else None
            if core:
                k = float(get_or_default(core, 'kills', default=0) or 0)
                d = float(get_or_default(core, 'deaths', default=0) or 0)
                a = float(get_or_default(core, 'assists', default=0) or 0)
                acc = float(get_or_default(core, 'accuracy', default=0) or 0)
                dmg = float(get_or_default(core, 'damage_dealt', default=0) or 0)
        kda = (k + a / 3.0 - d)
        xuids.append(xuid)
        rows.append({
            'match_id': match_id, 'player_xuid': xuid, 'team_id': int(team_id or 0),
            'outcome': str(outcome).lower(), 'is_tracked': xuid in tracked_by_xuid(),
            'is_bot': not bool(get_or_default(player, 'is_human', default=True)),
            'kills': k, 'deaths': d, 'assists': a, 'kda': kda,
            'accuracy': acc, 'damage_dealt': dmg,
            'match_date': match_date, 'playlist': playlist, 'map': map_name,
        })
    if not rows:
        return
    names = await resolve_gamertags(client, xuids)
    for r in rows:
        r['gamertag'] = names.get(r['player_xuid'], r['player_xuid'])
    # Capture per-player CSR for ranked matches (one skill call covers everyone).
    # -1.0 marks "ranked but no recap for this player" so the backfill won't keep
    # re-querying it; NULL stays for non-ranked matches (no CSR concept).
    is_ranked = bool(playlist) and 'ranked' in str(playlist).lower()
    if is_ranked:
        csr_map = await get_match_skill_csr(client, match_id, xuids)
        for r in rows:
            r['csr'] = csr_map.get(r['player_xuid'], -1.0)
    else:
        for r in rows:
            r['csr'] = None
    with engine.begin() as conn:
        for r in rows:
            conn.execute(text(
                """
                INSERT INTO halo_match_players
                    (match_id, player_xuid, gamertag, team_id, outcome, is_tracked,
                     is_bot, kills, deaths, assists, kda, accuracy, damage_dealt, csr,
                     match_date, playlist, map)
                VALUES
                    (:match_id, :player_xuid, :gamertag, :team_id, :outcome, :is_tracked,
                     :is_bot, :kills, :deaths, :assists, :kda, :accuracy, :damage_dealt, :csr,
                     :match_date, :playlist, :map)
                ON CONFLICT (match_id, player_xuid) DO UPDATE SET
                    gamertag = CASE WHEN EXCLUDED.gamertag LIKE 'Spartan-%%'
                                     AND halo_match_players.gamertag NOT LIKE 'Spartan-%%'
                               THEN halo_match_players.gamertag
                               ELSE EXCLUDED.gamertag END,
                    team_id = EXCLUDED.team_id,
                    outcome = EXCLUDED.outcome, is_tracked = EXCLUDED.is_tracked,
                    is_bot = EXCLUDED.is_bot,
                    kills = EXCLUDED.kills, deaths = EXCLUDED.deaths,
                    assists = EXCLUDED.assists, kda = EXCLUDED.kda,
                    accuracy = EXCLUDED.accuracy, damage_dealt = EXCLUDED.damage_dealt,
                    csr = COALESCE(EXCLUDED.csr, halo_match_players.csr),
                    match_date = EXCLUDED.match_date, playlist = EXCLUDED.playlist,
                    map = EXCLUDED.map
                """
            ), r)
    _CAPTURED_MATCH_PLAYERS.add(match_id)


async def backfill_opponent_csr(client, engine, limit=400):
    """Fill ``csr`` for historical ``halo_match_players`` rows captured before
    CSR tracking existed. Works newest-first, one skill call per match (covers
    all players), ranked playlists only. Marks players with no recap as -1 so
    they aren't re-queried. Runs a bounded batch per scraper cycle, so the whole
    backlog fills over a handful of cycles without one giant blocking job."""
    try:
        with engine.connect() as conn:
            match_ids = [r[0] for r in conn.execute(text(
                """
                SELECT match_id
                FROM halo_match_players
                WHERE csr IS NULL AND playlist ILIKE '%%Ranked%%'
                GROUP BY match_id
                ORDER BY MAX(match_date) DESC NULLS LAST
                LIMIT :lim
                """
            ), {"lim": int(limit)})]
    except Exception as exc:
        logger.warning("opp_csr_backfill_query_failed error=%s", exc)
        return

    if not match_ids:
        return

    filled = 0
    for mid in match_ids:
        try:
            with engine.connect() as conn:
                xuids = [r[0] for r in conn.execute(text(
                    "SELECT player_xuid FROM halo_match_players WHERE match_id = :m"
                ), {"m": mid})]
            csr_map = await get_match_skill_csr(client, mid, xuids)
            with engine.begin() as conn:
                for x in xuids:
                    conn.execute(text(
                        "UPDATE halo_match_players SET csr = :c "
                        "WHERE match_id = :m AND player_xuid = :x AND csr IS NULL"
                    ), {"c": csr_map.get(x, -1.0), "m": mid, "x": x})
            if csr_map:
                filled += 1
            await asyncio.sleep(0.15)
        except Exception as exc:
            logger.warning("opp_csr_backfill_match_failed match_id=%s error=%s", mid, exc)
    logger.info("opp_csr_backfill processed=%s matches_with_csr=%s", len(match_ids), filled)


# xuids we've already tried to re-resolve this process run — a name that
# still fails (banned / deleted account) shouldn't cost an API call every
# cycle forever.
_NAME_BACKFILL_TRIED: set = set()


async def backfill_opponent_names(client, engine, limit=300):
    """Re-resolve historical ``halo_match_players`` rows stuck with the
    ``Spartan-NNNN`` placeholder (captured while the profile API was
    failing), so nemesis/opponent views show real gamertags. Bounded per
    scraper cycle; a xuid that still can't be resolved is skipped for the
    rest of the process run."""
    try:
        with engine.connect() as conn:
            xuids = [r[0] for r in conn.execute(text(
                """
                SELECT DISTINCT player_xuid
                FROM halo_match_players
                WHERE gamertag LIKE 'Spartan-%%' AND NOT is_tracked
                  AND NOT COALESCE(is_bot, FALSE)  -- bots have bid(...) pseudo-xuids, never resolvable
                ORDER BY player_xuid
                LIMIT :lim
                """
            ), {"lim": int(limit)})]
    except Exception as exc:
        logger.warning("opp_name_backfill_query_failed error=%s", exc)
        return
    todo = [x for x in xuids if x not in _NAME_BACKFILL_TRIED]
    if not todo:
        return
    fixed = 0
    for i in range(0, len(todo), 30):
        chunk = todo[i:i + 30]
        names = await resolve_gamertags(client, chunk)
        with engine.begin() as conn:
            for x in chunk:
                _NAME_BACKFILL_TRIED.add(x)
                gt = names.get(x) or ""
                if gt and not gt.startswith("Spartan-"):
                    conn.execute(text(
                        "UPDATE halo_match_players SET gamertag = :g "
                        "WHERE player_xuid = :x"
                    ), {"g": gt, "x": x})
                    fixed += 1
        await asyncio.sleep(0.15)
    logger.info("opp_name_backfill tried=%s fixed=%s", len(todo), fixed)


# match_ids we've already attempted this process run — aged-out matches get a
# -1 sentinel in the DB, but transient failures shouldn't retry every cycle.
_PERFECTS_TRIED: set = set()


async def backfill_perfects(client, engine, limit=120, time_budget_s=20):
    """Fill team_perfects / enemy_team_perfects ('got perfected' tracker) for
    historical rows. One match-stats call covers every player in the match,
    newest-first, time-budgeted per cycle. Matches whose stats no longer
    resolve get a -1 sentinel so they aren't re-queried forever."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                """
                SELECT match_id, MAX(date) AS d
                FROM halo_match_stats
                WHERE enemy_team_perfects IS NULL
                GROUP BY match_id
                ORDER BY MAX(date) DESC NULLS LAST
                LIMIT :lim
                """), {"lim": int(limit)}).fetchall()
    except Exception as exc:
        logger.warning("perfects_backfill_query_failed error=%s", exc)
        return
    todo = [str(r[0]) for r in rows if str(r[0]) not in _PERFECTS_TRIED]
    if not todo:
        return
    medal_names = await get_medal_metadata(client)
    start = time.monotonic()
    filled = 0
    for mid in todo:
        if time.monotonic() - start > time_budget_s:
            break
        _PERFECTS_TRIED.add(mid)
        per_team = {}
        try:
            resp = await client.stats.get_match_stats(mid)
            ms = await resp.parse()
            for team in (get_or_default(ms, 'teams', default=[]) or []):
                tid = get_or_default(team, 'team_id')
                stats_obj = getattr(team, 'stats', None)
                core = getattr(stats_obj, 'core_stats', None) if stats_obj else None
                perf = 0
                for m in (getattr(core, 'medals', None) or []):
                    _mid = get_or_default(m, 'name_id')
                    if _mid is not None and medal_names.get(str(_mid)) == 'Perfect':
                        perf += int(get_or_default(m, 'count', default=0) or 0)
                if tid is not None:
                    per_team[int(tid)] = perf
        except Exception as exc:
            logger.warning("perfects_backfill_match_failed match_id=%s error=%s", mid, exc)
        try:
            with engine.begin() as conn:
                if per_team:
                    prows = conn.execute(text(
                        "SELECT DISTINCT player_gamertag, team_id "
                        "FROM halo_match_stats WHERE match_id = :m"), {"m": mid}).fetchall()
                    for gt, tid in prows:
                        own = per_team.get(int(tid or 0), 0)
                        enemy = sum(v for k, v in per_team.items() if k != int(tid or 0))
                        conn.execute(text(
                            "UPDATE halo_match_stats SET team_perfects = :tp, "
                            "enemy_team_perfects = :ep "
                            "WHERE match_id = :m AND player_gamertag = :g"
                        ), {"tp": own, "ep": enemy, "m": mid, "g": gt})
                    filled += 1
                else:
                    conn.execute(text(
                        "UPDATE halo_match_stats SET team_perfects = -1, "
                        "enemy_team_perfects = -1 WHERE match_id = :m"), {"m": mid})
        except Exception as exc:
            logger.warning("perfects_backfill_update_failed match_id=%s error=%s", mid, exc)
        await asyncio.sleep(0.15)
    logger.info("perfects_backfill attempted=%s filled=%s", len(todo), filled)


# match_ids whose lobby we've already attempted this process run — avoids
# re-querying the API every cycle for matches that have aged out / return no
# players (a 429 puts the match back so it's retried later).
_BACKFILL_TRIED: set = set()


async def backfill_match_players(client, engine, candidate_limit=600, time_budget_s=25):
    """Capture the full per-match lobby (opponents) for HISTORICAL matches that
    predate the halo_match_players feature.

    We already have every match_id + its date/playlist/map in halo_match_stats,
    so this does NOT re-discover match history — it only re-fetches per-match
    stats from the API (which returns all 8 players) and runs
    capture_match_players on each. Newest-first, bounded by a wall-clock budget
    per scraper cycle so it never balloons a cycle, and fully resumable: a match
    drops out of the candidate set the moment its lobby is captured.
    """
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                """
                SELECT s.match_id, MAX(s.date) AS d,
                       MAX(s.playlist) AS playlist, MAX(s.map) AS map
                FROM halo_match_stats s
                LEFT JOIN halo_match_players p ON p.match_id = s.match_id
                WHERE p.match_id IS NULL
                  AND s.playlist ILIKE '%%Ranked%%'
                GROUP BY s.match_id
                ORDER BY MAX(s.date) DESC NULLS LAST
                LIMIT :lim
                """
            ), {"lim": int(candidate_limit)}).fetchall()
    except Exception as exc:
        logger.warning("match_players_backfill_query_failed error=%s", exc)
        return

    rows = [r for r in rows if str(r[0]) not in _BACKFILL_TRIED]
    if not rows:
        return

    start = time.monotonic()
    attempted = captured = 0
    for mid, mdate, playlist, map_name in rows:
        if time.monotonic() - start > time_budget_s:
            break
        attempted += 1
        _BACKFILL_TRIED.add(str(mid))
        try:
            resp = await client.stats.get_match_stats(mid)
            match_stats = await resp.parse()
            if not match_stats or not getattr(match_stats, 'players', None):
                continue  # aged out / unavailable — leave it tried so we don't re-hit it
            await capture_match_players(client, engine, str(mid), match_stats,
                                        mdate, playlist or '', map_name or '')
            captured += 1
            await asyncio.sleep(0.3)  # gentle pacing vs API rate limits
        except ClientResponseError as e:
            if getattr(e, 'status', None) == 429:
                logger.warning("match_players_backfill_rate_limited match_id=%s", mid)
                _BACKFILL_TRIED.discard(str(mid))  # retry it next cycle
                await asyncio.sleep(5)
                break  # back off for the rest of this cycle
            logger.warning("match_players_backfill_failed match_id=%s error=%s", mid, e)
        except Exception as exc:
            logger.warning("match_players_backfill_failed match_id=%s error=%s", mid, exc)
    if attempted:
        logger.info("match_players_backfill attempted=%s captured=%s", attempted, captured)


def ensure_kv_schema(engine) -> None:
    """Create tables for auto-discovered stats.

    - halo_match_stats_kv: one row per (player_xuid, match_id, key)
    - halo_stat_keys: registry of all keys ever observed

    This lets the scraper store new/unknown stats without code changes.
    """

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS halo_match_stats_kv (
                    player_xuid TEXT NOT NULL,
                    match_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value_json JSONB,
                    value_text TEXT,
                    value_num DOUBLE PRECISION,
                    value_type TEXT,
                    scraped_at TIMESTAMPTZ,
                    PRIMARY KEY (player_xuid, match_id, key)
                );
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS halo_stat_keys (
                    key TEXT PRIMARY KEY,
                    first_seen TIMESTAMPTZ,
                    last_seen TIMESTAMPTZ,
                    inferred_type TEXT,
                    example_json JSONB
                );
                """
            )
        )

    ensure_kv_indexes(engine)


def ensure_kv_indexes(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_halo_match_stats_kv_key ON halo_match_stats_kv (key)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_halo_match_stats_kv_match ON halo_match_stats_kv (match_id)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_halo_match_stats_kv_player ON halo_match_stats_kv (player_xuid)"
            )
        )


def _infer_value_type(value) -> tuple[str, float | None, str | None, str | None]:
    """Return (value_type, value_num, value_text, value_json_str)."""

    if value is None:
        return "null", None, None, "null"

    # Normalize bool before int/float checks.
    if isinstance(value, bool):
        return "bool", 1.0 if value else 0.0, "true" if value else "false", json.dumps(value)

    if isinstance(value, (int, float)):
        # Keep as numeric + json
        return "number", float(value), None, json.dumps(value)

    if isinstance(value, str):
        # Some API fields might be huge; store text and json.
        return "text", None, value, json.dumps(value)

    # Lists/dicts/objects: store as json only.
    try:
        return "json", None, None, json.dumps(value, default=str)
    except Exception:
        # Last resort
        return "text", None, str(value), json.dumps(str(value))


def write_extra_stats_to_kv(
    engine,
    player_xuid: str,
    match_id: str,
    payload: dict,
    scraped_at_iso: str | None = None,
) -> None:
    """Persist any fields not present in the stable wide schema into KV tables."""

    if not payload:
        return

    known_cols = set(all_db_columns())
    extras = {k: v for k, v in payload.items() if k not in known_cols}
    if not extras:
        return

    scraped_at = scraped_at_iso or datetime.now(dt_timezone.utc).isoformat()

    rows = []
    key_rows = []
    for key, value in extras.items():
        value_type, value_num, value_text, value_json_str = _infer_value_type(value)
        rows.append(
            {
                "player_xuid": str(player_xuid),
                "match_id": str(match_id),
                "key": str(key),
                "value_json": value_json_str,
                "value_text": value_text,
                "value_num": value_num,
                "value_type": value_type,
                "scraped_at": scraped_at,
            }
        )
        key_rows.append(
            {
                "key": str(key),
                "ts": scraped_at,
                "inferred_type": value_type,
                "example_json": value_json_str,
            }
        )

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO halo_match_stats_kv (
                    player_xuid, match_id, key,
                    value_json, value_text, value_num, value_type, scraped_at
                )
                VALUES (
                    :player_xuid, :match_id, :key,
                    (:value_json)::jsonb, :value_text, :value_num, :value_type, (:scraped_at)::timestamptz
                )
                ON CONFLICT (player_xuid, match_id, key)
                DO UPDATE SET
                    value_json = EXCLUDED.value_json,
                    value_text = EXCLUDED.value_text,
                    value_num = EXCLUDED.value_num,
                    value_type = EXCLUDED.value_type,
                    scraped_at = EXCLUDED.scraped_at
                """
            ),
            rows,
        )

        conn.execute(
            text(
                """
                INSERT INTO halo_stat_keys (key, first_seen, last_seen, inferred_type, example_json)
                VALUES (:key, (:ts)::timestamptz, (:ts)::timestamptz, :inferred_type, (:example_json)::jsonb)
                ON CONFLICT (key)
                DO UPDATE SET
                    last_seen = EXCLUDED.last_seen
                """
            ),
            key_rows,
        )

# Tracked players — shared helper reads players.json (from /setup) first, then
# the HALO_TRACKED_PLAYERS env var, else []. stats.py runs as a fresh process
# each scrape cycle (see entrypoint.py), so roster changes made in /setup are
# picked up automatically on the next cycle without a restart.
from players import load_players as _load_tracked_players

PLAYERS = _load_tracked_players()
logger.info("tracked_players count=%s", len(PLAYERS))

# Resolved opponent gamertags (xuid -> gamertag), filled lazily via the profile
# API and reused across matches within a run.
_XUID_NAME_CACHE: dict = {}
# match_ids whose full player list we've already captured this run (process_match
# is called once per tracked player per match, so this avoids redundant writes).
_CAPTURED_MATCH_PLAYERS: set = set()
# xuid -> gamertag for tracked players is built lazily (see tracked_by_xuid())
# because clean_xuid() is defined further down this module.
_TRACKED_BY_XUID_CACHE: dict | None = None


def tracked_by_xuid() -> dict:
    global _TRACKED_BY_XUID_CACHE
    if _TRACKED_BY_XUID_CACHE is None:
        _TRACKED_BY_XUID_CACHE = {clean_xuid(p["xuid"]): p["gamertag"] for p in PLAYERS}
    return _TRACKED_BY_XUID_CACHE

FINAL_COLUMNS = [
    'player_gamertag', 'player_xuid', 'match_id', 'date', 'duration', 'game_type', 'map', 'playlist','playlist_id', 'outcome', 'team_id', 'team_rank', 'kills', 'deaths', 'assists', 'kda', 'accuracy','score', 'medal_count', 'dmg/ka', 'dmg/death', 'dmg/min', 'dmg_difference', 'pre_match_csr', 'post_match_csr', 'medal_360','medal_Achilles_Spine','medal_Always_Rotating','medal_Back_Smack','medal_Ballista','medal_Bank_Shot','medal_Blind_Fire','medal_Bodyguard','medal_Bomber','medal_Boom_Block','medal_Boxer','medal_Breacher','medal_Bulltrue','medal_Call_Blocked','medal_Chain_Reaction','medal_Clear_Reception','medal_Clock_Stop','medal_Cluster_Luck','medal_Combat_Evolved','medal_Counter_snipe','medal_Deadly_Catch','medal_Double_Kill','medal_Extermination','medal_Fastball','medal_Flag_Joust','medal_Flawless_Victory','medal_From_the_Grave','medal_Fumble','medal_Goal_Line_Stand','medal_Grenadier','medal_Guardian_Angel','medal_Gunslinger','medal_Hail_Mary','medal_Hang_Up','medal_Harpoon','medal_Hill_Guardian','medal_Hold_This','medal_Interlinked','medal_Killing_Frenzy','medal_Killing_Spree','medal_Killjoy','medal_Killtacular','medal_Killtrocity','medal_Last_Shot','medal_Marksman','medal_Mind_the_Gap','medal_Nade_Shot','medal_Ninja','medal_No_Scope','medal_Odin_s_Raven','medal_Off_the_Rack','medal_Overkill','medal_Pancake','medal_Perfect','medal_Pull','medal_Quick_Draw','medal_Quigley','medal_Remote_Detonation','medal_Return_to_Sender','medal_Reversal','medal_Rifleman','medal_Scattergunner','medal_Secure_Line','medal_Sharpshooter','medal_Shot_Caller','medal_Signal_Block','medal_Sneak_King','medal_Snipe','medal_Special_Delivery','medal_Spotter','medal_Steaktacular','medal_Stick','medal_Stopped_Short','medal_Straight_Balling','medal_Treasure_Hunter','medal_Triple_Kill','medal_Warrior','medal_Whiplash','medal_Wingman','medal_Yard_Sale','medal_id_1024030246','medal_id_1032565232','medal_id_1117301492','medal_id_1267013266','medal_id_152718958','medal_id_1552628741','medal_id_1825517751','medal_id_204144695','medal_id_22113181','medal_id_2387185397','medal_id_2408971842','medal_id_249491819','medal_id_3002710045','medal_id_316828380','medal_id_340198991','medal_id_3507884073','medal_id_4130011565','medal_id_4247243561','medal_id_454168309','medal_id_555570945','medal_id_601966503','medal_id_638246808','medal_id_709346128','medal_id_746397417','medal_id_911992497','all_time_max_csr_initial_measurement_matches','all_time_max_csr_measurement_matches_remaining','all_time_max_csr_next_sub_tier','all_time_max_csr_next_tier','all_time_max_csr_next_tier_start','all_time_max_csr_sub_tier','all_time_max_csr_tier','all_time_max_csr_tier_start','all_time_max_csr_value','current_csr_initial_measurement_matches','current_csr_measurement_matches_remaining','current_csr_next_sub_tier','current_csr_next_tier','current_csr_next_tier_start','current_csr_sub_tier','current_csr_tier','current_csr_tier_start','current_csr_value','season_max_csr_initial_measurement_matches','season_max_csr_measurement_matches_remaining','season_max_csr_next_sub_tier','season_max_csr_next_tier','season_max_csr_next_tier_start','season_max_csr_sub_tier','season_max_csr_tier','season_max_csr_tier_start','season_max_csr_value','average_life_duration','betrayals','callout_assists','capture_the_flag_stats_flag_capture_assists','capture_the_flag_stats_flag_captures','capture_the_flag_stats_flag_carriers_killed','capture_the_flag_stats_flag_grabs','capture_the_flag_stats_flag_returners_killed','capture_the_flag_stats_flag_returns','capture_the_flag_stats_flag_secures','capture_the_flag_stats_flag_steals','capture_the_flag_stats_kills_as_flag_carrier','capture_the_flag_stats_kills_as_flag_returner','capture_the_flag_stats_time_as_flag_carrier','damage_dealt','damage_taken','driver_assists','emp_assists','extraction_stats_extraction_conversions_completed','extraction_stats_extraction_conversions_denied','extraction_stats_extraction_initiations_completed','extraction_stats_extraction_initiations_denied','extraction_stats_successful_extractions','grenade_kills','headshot_kills','hijacks','max_killing_spree','melee_kills','objectives_completed','oddball_stats_kills_as_skull_carrier','oddball_stats_longest_time_as_skull_carrier','oddball_stats_skull_carriers_killed','oddball_stats_skull_grabs','oddball_stats_skull_scoring_ticks','oddball_stats_time_as_skull_carrier','personal_score','power_weapon_kills','pvp_stats_assists','pvp_stats_deaths','pvp_stats_kda','pvp_stats_kills','rounds_lost','rounds_tied','rounds_won','shots_fired','shots_hit','spawns','suicides','vehicle_destroys','zones_stats_stronghold_captures','zones_stats_stronghold_defensive_kills','zones_stats_stronghold_occupation_time','zones_stats_stronghold_offensive_kills','zones_stats_stronghold_scoring_ticks','zones_stats_stronghold_secures','team_accuracy','team_assists','team_average_life_duration','team_betrayals','team_callout_assists','team_capture_the_flag_stats_flag_capture_assists','team_capture_the_flag_stats_flag_captures','team_capture_the_flag_stats_flag_carriers_killed','team_capture_the_flag_stats_flag_grabs','team_capture_the_flag_stats_flag_returners_killed','team_capture_the_flag_stats_flag_returns','team_capture_the_flag_stats_flag_secures','team_capture_the_flag_stats_flag_steals','team_capture_the_flag_stats_kills_as_flag_carrier','team_capture_the_flag_stats_kills_as_flag_returner','team_capture_the_flag_stats_time_as_flag_carrier','team_damage_dealt','team_damage_taken','team_deaths','team_driver_assists','team_emp_assists','team_extraction_stats_extraction_conversions_completed','team_extraction_stats_extraction_conversions_denied','team_extraction_stats_extraction_initiations_completed','team_extraction_stats_extraction_initiations_denied','team_extraction_stats_successful_extractions','team_grenade_kills','team_headshot_kills','team_hijacks','team_id','team_kda','team_kills','team_max_killing_spree','team_medal_count','team_medals','team_perfects','team_melee_kills','team_objectives_completed','team_oddball_stats_kills_as_skull_carrier','team_oddball_stats_longest_time_as_skull_carrier','team_oddball_stats_skull_carriers_killed','team_oddball_stats_skull_grabs','team_oddball_stats_skull_scoring_ticks','team_oddball_stats_time_as_skull_carrier','team_personal_score','team_power_weapon_kills','team_pvp_stats_assists','team_pvp_stats_deaths','team_pvp_stats_kda','team_pvp_stats_kills','team_rank','team_rounds_lost','team_rounds_tied','team_rounds_won','team_score','team_shots_fired','team_shots_hit','team_spawns','team_suicides','team_vehicle_destroys','team_zones_stats_stronghold_captures','team_zones_stats_stronghold_defensive_kills','team_zones_stats_stronghold_occupation_time','team_zones_stats_stronghold_offensive_kills','team_zones_stats_stronghold_scoring_ticks','team_zones_stats_stronghold_secures','enemy_team_accuracy','enemy_team_assists','enemy_team_average_life_duration','enemy_team_betrayals','enemy_team_callout_assists','enemy_team_capture_the_flag_stats_flag_capture_assists','enemy_team_capture_the_flag_stats_flag_captures','enemy_team_capture_the_flag_stats_flag_carriers_killed','enemy_team_capture_the_flag_stats_flag_grabs','enemy_team_capture_the_flag_stats_flag_returners_killed','enemy_team_capture_the_flag_stats_flag_returns','enemy_team_capture_the_flag_stats_flag_secures','enemy_team_capture_the_flag_stats_flag_steals','enemy_team_capture_the_flag_stats_kills_as_flag_carrier','enemy_team_capture_the_flag_stats_kills_as_flag_returner','enemy_team_capture_the_flag_stats_time_as_flag_carrier','enemy_team_damage_dealt','enemy_team_damage_taken','enemy_team_deaths','enemy_team_driver_assists','enemy_team_emp_assists','enemy_team_extraction_stats_extraction_conversions_completed','enemy_team_extraction_stats_extraction_conversions_denied','enemy_team_extraction_stats_extraction_initiations_completed','enemy_team_extraction_stats_extraction_initiations_denied','enemy_team_extraction_stats_successful_extractions','enemy_team_grenade_kills','enemy_team_headshot_kills','enemy_team_hijacks','enemy_team_kda','enemy_team_kills','enemy_team_max_killing_spree','enemy_team_medal_count','enemy_team_medals','enemy_team_perfects','enemy_team_melee_kills','enemy_team_objectives_completed','enemy_team_oddball_stats_kills_as_skull_carrier','enemy_team_oddball_stats_longest_time_as_skull_carrier','enemy_team_oddball_stats_skull_carriers_killed','enemy_team_oddball_stats_skull_grabs','enemy_team_oddball_stats_skull_scoring_ticks','enemy_team_oddball_stats_time_as_skull_carrier','enemy_team_personal_score','enemy_team_power_weapon_kills','enemy_team_pvp_stats_assists','enemy_team_pvp_stats_deaths','enemy_team_pvp_stats_kda','enemy_team_pvp_stats_kills','enemy_team_rounds_lost','enemy_team_rounds_tied','enemy_team_rounds_won','enemy_team_score','enemy_team_shots_fired','enemy_team_shots_hit','enemy_team_spawns','enemy_team_suicides','enemy_team_vehicle_destroys','enemy_team_zones_stats_stronghold_captures','enemy_team_zones_stats_stronghold_defensive_kills','enemy_team_zones_stats_stronghold_occupation_time','enemy_team_zones_stats_stronghold_offensive_kills','enemy_team_zones_stats_stronghold_scoring_ticks','enemy_team_zones_stats_stronghold_secures','player_rank','season_id','team_mmr','enemy_team_mmr','expected_kills','expected_deaths','kills_std_dev','deaths_std_dev','cf_expected_kills','cf_expected_deaths'
]

TIME_COLUMNS = [
    'duration',
    'average_life_duration',
    'capture_the_flag_stats_time_as_flag_carrier',
    'oddball_stats_time_as_skull_carrier',
    'oddball_stats_longest_time_as_skull_carrier',
    'team_average_life_duration',
    'team_capture_the_flag_stats_time_as_flag_carrier',
    'team_oddball_stats_time_as_skull_carrier',
    'team_oddball_stats_longest_time_as_skull_carrier',
    'enemy_team_average_life_duration',
    'enemy_team_capture_the_flag_stats_time_as_flag_carrier',
    'enemy_team_oddball_stats_time_as_skull_carrier',
    'enemy_team_oddball_stats_longest_time_as_skull_carrier',
    'zones_stats_stronghold_occupation_time',
    'team_zones_stats_stronghold_occupation_time',
    'enemy_team_zones_stats_stronghold_occupation_time'
]

# Outcome mapping
OUTCOMES = {0: "Left", 1: "Tie", 2: "Win", 3: "Loss", 4: "Dnf"}

# Cache for medal metadata
medal_cache = {}
        
def clean_xuid(xuid):
    """Clean XUID format"""
    if isinstance(xuid, str) and "xuid(" in xuid:
        return xuid.replace("xuid(", "").replace(")", "")
    return str(xuid)

def load_tokens():
    """Load authentication tokens from file"""
    with open(TOKENS_PATH, 'r') as f:
        return json.load(f)

def get_or_default(obj, *attrs, default=None):
    """Safely navigate nested objects"""
    for attr in attrs:
        if hasattr(obj, attr):
            obj = getattr(obj, attr)
            if obj is None:
                return default
        else:
            return default
    return obj

async def get_match_skill_full(client, match_id, xuid):
    """Pre/post CSR + skill performance for one player in a match, from a SINGLE
    get_match_skill call (no extra cost vs. the CSR-only fetch). Returns a dict;
    any field the API doesn't provide stays None. Captures the data we used to
    discard: team MMR (yours + enemy), expected-vs-actual kills/deaths + std dev,
    and self counterfactuals (expected K/D for a player of your skill)."""
    out = {'pre_match_csr': None, 'post_match_csr': None,
           'team_mmr': None, 'enemy_team_mmr': None,
           'expected_kills': None, 'expected_deaths': None,
           'kills_std_dev': None, 'deaths_std_dev': None,
           'cf_expected_kills': None, 'cf_expected_deaths': None}
    try:
        response = await client.skill.get_match_skill(match_id=match_id, xuids=[xuid])
        data = await response.parse()
        for entry in getattr(data, 'value', None) or []:
            if clean_xuid(getattr(entry, 'id', '') or '') != xuid:
                continue
            result = getattr(entry, 'result', None)
            if not result:
                break
            recap = getattr(result, 'rank_recap', None)
            if recap:
                pre = getattr(recap, 'pre_match_csr', None)
                post = getattr(recap, 'post_match_csr', None)
                out['pre_match_csr'] = getattr(pre, 'value', None) if pre else None
                out['post_match_csr'] = getattr(post, 'value', None) if post else None
            tm = getattr(result, 'team_mmr', None)
            out['team_mmr'] = float(tm) if tm is not None else None
            my_team = getattr(result, 'team_id', None)
            try:
                mmrs = dict(getattr(result, 'team_mmrs', None) or {})
                enemy = [float(v) for k, v in mmrs.items() if k != my_team]
                out['enemy_team_mmr'] = (sum(enemy) / len(enemy)) if enemy else None
            except Exception:
                pass
            sp = getattr(result, 'stat_performances', None)
            if sp:
                k = getattr(sp, 'kills', None)
                d = getattr(sp, 'deaths', None)
                if k:
                    out['expected_kills'] = getattr(k, 'expected', None)
                    out['kills_std_dev'] = getattr(k, 'std_dev', None)
                if d:
                    out['expected_deaths'] = getattr(d, 'expected', None)
                    out['deaths_std_dev'] = getattr(d, 'std_dev', None)
            cf = getattr(result, 'counterfactuals', None)
            self_cf = getattr(cf, 'self_counterfactuals', None) if cf else None
            if self_cf:
                out['cf_expected_kills'] = getattr(self_cf, 'kills', None)
                out['cf_expected_deaths'] = getattr(self_cf, 'deaths', None)
            break
    except Exception as e:
        logger.warning("match_skill_full_fetch_failed match_id=%s xuid=%s error=%s", match_id, xuid, e)
    return out


async def get_match_skill_all(client, match_id, xuids):
    """Like get_match_skill_full but for MANY players in one call → {clean_xuid:
    skill_dict}. Used by the historical skill backfill (one API call per match
    covers every tracked player in it)."""
    out = {}
    xuids = [x for x in dict.fromkeys(xuids) if x]
    if not xuids:
        return out
    try:
        response = await client.skill.get_match_skill(match_id=match_id, xuids=xuids)
        data = await response.parse()
        for entry in getattr(data, 'value', None) or []:
            xid = clean_xuid(getattr(entry, 'id', '') or '')
            if not xid:
                continue
            result = getattr(entry, 'result', None)
            if not result:
                continue
            rec = {'team_mmr': None, 'enemy_team_mmr': None,
                   'expected_kills': None, 'expected_deaths': None,
                   'kills_std_dev': None, 'deaths_std_dev': None,
                   'cf_expected_kills': None, 'cf_expected_deaths': None,
                   'pre_match_csr': None, 'post_match_csr': None}
            recap = getattr(result, 'rank_recap', None)
            if recap:
                pre = getattr(recap, 'pre_match_csr', None)
                post = getattr(recap, 'post_match_csr', None)
                rec['pre_match_csr'] = getattr(pre, 'value', None) if pre else None
                rec['post_match_csr'] = getattr(post, 'value', None) if post else None
            tm = getattr(result, 'team_mmr', None)
            rec['team_mmr'] = float(tm) if tm is not None else None
            my_team = getattr(result, 'team_id', None)
            try:
                mmrs = dict(getattr(result, 'team_mmrs', None) or {})
                enemy = [float(v) for k, v in mmrs.items() if k != my_team]
                rec['enemy_team_mmr'] = (sum(enemy) / len(enemy)) if enemy else None
            except Exception:
                pass
            sp = getattr(result, 'stat_performances', None)
            if sp:
                k = getattr(sp, 'kills', None)
                d = getattr(sp, 'deaths', None)
                if k:
                    rec['expected_kills'] = getattr(k, 'expected', None)
                    rec['kills_std_dev'] = getattr(k, 'std_dev', None)
                if d:
                    rec['expected_deaths'] = getattr(d, 'expected', None)
                    rec['deaths_std_dev'] = getattr(d, 'std_dev', None)
            cf = getattr(result, 'counterfactuals', None)
            self_cf = getattr(cf, 'self_counterfactuals', None) if cf else None
            if self_cf:
                rec['cf_expected_kills'] = getattr(self_cf, 'kills', None)
                rec['cf_expected_deaths'] = getattr(self_cf, 'deaths', None)
            out[xid] = rec
    except Exception as e:
        logger.warning("match_skill_all_fetch_failed match_id=%s error=%s", match_id, e)
    return out


_SKILL_BACKFILL_TRIED: set = set()


async def backfill_skill_stats(client, engine, candidate_limit=400, time_budget_s=20):
    """Backfill team MMR + expected-vs-actual K/D + counterfactuals onto historical
    halo_match_stats rows (tracked players). One get_match_skill call per match
    covers all tracked players in it. team_mmr IS NULL marks a row as needing it.
    Time-budgeted + resumable, newest-first, ranked only."""
    try:
        with engine.connect() as conn:
            match_ids = [r[0] for r in conn.execute(text(
                """
                SELECT match_id
                FROM halo_match_stats
                WHERE team_mmr IS NULL AND playlist ILIKE '%%Ranked%%'
                GROUP BY match_id
                ORDER BY MAX(date) DESC NULLS LAST
                LIMIT :lim
                """
            ), {"lim": int(candidate_limit)})]
    except Exception as exc:
        logger.warning("skill_backfill_query_failed error=%s", exc)
        return

    match_ids = [m for m in match_ids if str(m) not in _SKILL_BACKFILL_TRIED]
    if not match_ids:
        return

    cols = ('team_mmr', 'enemy_team_mmr', 'expected_kills', 'expected_deaths',
            'kills_std_dev', 'deaths_std_dev', 'cf_expected_kills', 'cf_expected_deaths')
    set_sql = ', '.join(f"{c} = :{c}" for c in cols)
    start = time.monotonic()
    attempted = filled = 0
    for mid in match_ids:
        if time.monotonic() - start > time_budget_s:
            break
        attempted += 1
        _SKILL_BACKFILL_TRIED.add(str(mid))
        try:
            with engine.connect() as conn:
                xuids = [r[0] for r in conn.execute(text(
                    "SELECT player_xuid FROM halo_match_stats WHERE match_id = :m"
                ), {"m": mid})]
            skill = await get_match_skill_all(client, mid, xuids)
            if not skill:
                continue
            with engine.begin() as conn:
                for xid, rec in skill.items():
                    params = {c: rec.get(c) for c in cols}
                    params['m'] = mid
                    params['x'] = xid
                    conn.execute(text(
                        f"UPDATE halo_match_stats SET {set_sql} "
                        "WHERE match_id = :m AND player_xuid = :x"
                    ), params)
            filled += 1
            await asyncio.sleep(0.3)
        except ClientResponseError as e:
            if getattr(e, 'status', None) == 429:
                _SKILL_BACKFILL_TRIED.discard(str(mid))
                await asyncio.sleep(5)
                break
            logger.warning("skill_backfill_match_failed match_id=%s error=%s", mid, e)
        except Exception as exc:
            logger.warning("skill_backfill_match_failed match_id=%s error=%s", mid, exc)
    if attempted:
        logger.info("skill_backfill attempted=%s filled=%s", attempted, filled)


async def get_rank_recap_csr_change(client, match_id, xuid):
    """Fetch pre-match and post-match CSR (if available) for a specific player and match."""
    try:
        response = await client.skill.get_match_skill(match_id=match_id, xuids=[xuid])
        data = await response.parse()

        for entry in data.value:
            if clean_xuid(entry.id) != xuid:
                continue

            result = getattr(entry, 'result', None)
            if not result or not hasattr(result, 'rank_recap'):
                # 🟡 No rank recap available (normal in many matches)
                return None, None

            recap = result.rank_recap
            pre = getattr(recap, 'pre_match_csr', None)
            post = getattr(recap, 'post_match_csr', None)

            pre_value = getattr(pre, 'value', None) if pre else None
            post_value = getattr(post, 'value', None) if post else None

            return pre_value, post_value

    except Exception as e:
        logger.warning("csr_rank_recap_fetch_failed match_id=%s xuid=%s error=%s", match_id, xuid, e)

    return None, None


async def get_match_skill_csr(client, match_id, xuids):
    """Return {clean_xuid: post_match_csr} for every player in a ranked match.

    One ``get_match_skill`` call returns the rank recap for all requested xuids,
    so we can grab opponent CSR cheaply (one call per match). Returns an empty
    dict for matches with no rank recap (social / unrated)."""
    out = {}
    xuids = [x for x in dict.fromkeys(xuids) if x]  # de-dup, drop blanks
    if not xuids:
        return out
    try:
        response = await client.skill.get_match_skill(match_id=match_id, xuids=xuids)
        data = await response.parse()
        for entry in getattr(data, 'value', None) or []:
            xid = clean_xuid(getattr(entry, 'id', '') or '')
            if not xid:
                continue
            result = getattr(entry, 'result', None)
            recap = getattr(result, 'rank_recap', None) if result else None
            if not recap:
                continue
            post = getattr(recap, 'post_match_csr', None)
            pre = getattr(recap, 'pre_match_csr', None)
            val = getattr(post, 'value', None) if post else None
            if val is None or val <= 0:
                val = getattr(pre, 'value', None) if pre else None
            if val is not None and val > 0:
                out[xid] = float(val)
    except Exception as e:
        logger.warning("match_skill_csr_fetch_failed match_id=%s error=%s", match_id, e)
    return out

def normalize_row(row, all_fields):
    """
    Ensure all expected fields exist in the row with appropriate default values.
    Numeric fields get 0, text fields get ''.
    """
    normalized = {}
    
    # If row is None, create an empty dictionary
    if row is None:
        row = {}
    
    for field in all_fields:
        clean_field = field.strip()  # Remove any whitespace
        if clean_field in row:
            # Convert empty strings to appropriate defaults
            if row[clean_field] == '':
                if (
                    clean_field.startswith(('medal_', 'time_', 'damage_', 'team_', 'enemy_team_', 'capture_', 'extraction_', 'oddball_', 'zones_')) or
                    any(clean_field.endswith(suffix) for suffix in (
                        '_count', '_kills', '_score', '_value', '_ticks', '_deaths', '_assists',
                        '_starts', '_captures', '_grabs', '_returns', '_carrier', '_denied', '_completed', '_steals', '_spawns',
                        '_shots', '_suicides', '_rank', '_duration'
                    )) or
                    clean_field in ('kills', 'deaths', 'assists', 'kd', 'kda', 'accuracy', 'score', 'team_rank', 'betrayals')
                ):
                    normalized[clean_field] = 0
                else:
                    normalized[clean_field] = ''
            else:
                normalized[clean_field] = row[clean_field]
        else:
            # Determine if this should be a numeric field based on prefix/suffix
            if (
                clean_field.startswith(('medal_', 'time_', 'damage_', 'team_', 'enemy_team_', 'capture_', 'extraction_', 'oddball_', 'zones_')) or
                any(clean_field.endswith(suffix) for suffix in (
                    '_count', '_kills', '_score', '_value', '_ticks', '_deaths', '_assists',
                    '_starts', '_captures', '_grabs', '_returns', '_carrier', '_denied', '_completed', '_steals', '_spawns',
                    '_shots', '_suicides', '_rank', '_duration'
                )) or
                clean_field in ('kills', 'deaths', 'assists', 'kd', 'kda', 'accuracy', 'score', 'team_rank', 'betrayals')
            ):
                normalized[clean_field] = 0
            else:
                normalized[clean_field] = ''
    return normalized

def parse_duration_to_seconds(duration_str):
    try:
        if not isinstance(duration_str, str):
            duration_str = str(duration_str)

        if duration_str.count(':') == 2:
            h, m, s = duration_str.split(':')
            return int(h) * 3600 + int(m) * 60 + float(s)
        elif duration_str.count(':') == 1:
            m, s = duration_str.split(':')
            return int(m) * 60 + float(s)
    except Exception as e:
        logger.warning("duration_parse_failed value=%s error=%s", duration_str, e)
    return 0

def get_engine():
    db_url = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    return create_engine(db_url, pool_pre_ping=True)

def table_exists(engine) -> bool:
    return inspect(engine).has_table("halo_match_stats")

def dedupe_columns(cols):
    counter = {}
    new_cols = []
    for col in cols:
        count = counter.get(col, 0)
        new_col = f"{col}.{count}" if count > 0 else col
        new_cols.append(new_col)
        counter[col] = count + 1
    return new_cols

def normalize_columns_for_db(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [col.strip().lower().replace(" ", "_") for col in df.columns]
    df.columns = dedupe_columns(df.columns)
    return df

def prepare_results_dataframe(results: list) -> pd.DataFrame:
    df = pd.DataFrame(results)
    if df.empty:
        return df

    df = convert_time_columns_to_seconds(df)
    df = normalize_columns_for_db(df)

    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")

    for col in df.columns:
        if col in TEXT_COLUMNS:
            df[col] = df[col].astype(str)
        elif col != "date":
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df

def delete_existing_matches(engine, df: pd.DataFrame) -> None:
    if df.empty or "player_xuid" not in df.columns or "match_id" not in df.columns:
        return

    pairs = df[["player_xuid", "match_id"]].dropna().drop_duplicates()
    if pairs.empty:
        return

    with engine.begin() as conn:
        for player_xuid, group in pairs.groupby("player_xuid"):
            match_ids = group["match_id"].tolist()
            conn.execute(
                text(
                    "DELETE FROM halo_match_stats "
                    "WHERE player_xuid = :xuid AND match_id = ANY(:match_ids)"
                ),
                {"xuid": player_xuid, "match_ids": match_ids},
            )

def ensure_indexes(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_halo_match_stats_player "
                "ON halo_match_stats (player_xuid)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_halo_match_stats_date "
                "ON halo_match_stats (date)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_halo_match_stats_player_date "
                "ON halo_match_stats (player_xuid, date)"
            )
        )
        try:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_halo_match_stats_unique "
                    "ON halo_match_stats (player_xuid, match_id)"
                )
            )
        except Exception as exc:
            logger.warning("unique_index_create_failed error=%s", exc)


def write_single_result_to_db(match_row: dict, engine) -> bool:
    """Write a single normalized match row to the DB immediately.

    This keeps the website up-to-date during long scrape runs.
    We delete any existing (player_xuid, match_id) row first to avoid
    unique-index conflicts and allow refresh/re-scrapes.
    """

    if not match_row:
        return False

    df = prepare_results_dataframe([match_row])
    if df.empty:
        return False

    try:
        if not table_exists(engine):
            ensure_schema(engine)
        created = False
        # Table should already exist via ensure_schema; keep 'created' for compatibility.
        created = False

        delete_existing_matches(engine, df)
        df.to_sql(
            "halo_match_stats",
            engine,
            if_exists="append",
            index=False,
            chunksize=1,
            method="multi",
        )

        if created:
            ensure_indexes(engine)

        return True
    except Exception as exc:
        logger.warning("single_match_write_failed error=%s", exc)
        return False

def write_results_to_db(results: list, engine) -> int:
    df = prepare_results_dataframe(results)
    if df.empty:
        return 0

    try:
        if not table_exists(engine):
            ensure_schema(engine)

        delete_existing_matches(engine, df)
        df.to_sql(
            "halo_match_stats",
            engine,
            if_exists="append",
            index=False,
            chunksize=1000,
            method="multi",
        )
        ensure_indexes(engine)
        return len(df)
    except Exception as exc:
        logger.warning("batch_write_failed error=%s", exc)
        return 0

def get_existing_match_ids(engine, player_xuid: str) -> set:
    try:
        if not table_exists(engine):
            # Make sure the table exists with the stable schema so future
            # calls don't repeatedly attempt to auto-create it.
            ensure_schema(engine)
        if not table_exists(engine):
            return set()
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT match_id FROM halo_match_stats WHERE player_xuid = :xuid"
                ),
                {"xuid": player_xuid},
            ).fetchall()
        # Ensure match IDs are strings for comparison
        match_ids = {str(row[0]) for row in rows}
        logger.info("existing_matches_loaded xuid=%s count=%s", player_xuid, len(match_ids))
        return match_ids
    except Exception as exc:
        logger.warning("existing_matches_load_failed xuid=%s error=%s", player_xuid, exc)
        return set()

def trim_player_history(engine, player_xuid: str, limit: int) -> None:
    """DISABLED - This function was incorrectly deleting historical data.
    
    match_limit should only control how many matches to FETCH per run,
    not how many to KEEP in the database. Keeping all historical data.
    """
    # DO NOT DELETE HISTORICAL DATA
    pass


def get_latest_match_at(engine) -> str | None:
    """ISO timestamp of the most recent match across all tracked players, or None.
    Used by the entrypoint to poll faster while a session is live."""
    try:
        with engine.connect() as conn:
            val = conn.execute(text("SELECT MAX(date) FROM halo_match_stats")).scalar()
        if val is None:
            return None
        return val.isoformat() if hasattr(val, "isoformat") else str(val)
    except Exception as e:
        logger.warning("latest_match_at_query_failed error=%s", e)
        return None


def write_update_status(inserted_rows: int, engine=None) -> None:
    update_status["new_rows_added"] = inserted_rows > 0
    update_status["new_row_count"] = inserted_rows
    update_status["last_update"] = datetime.now(dt_timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if engine is not None:
        latest = get_latest_match_at(engine)
        if latest:
            update_status["latest_match_at"] = latest
    try:
        with open(UPDATE_STATUS_PATH, "w") as f:
            json.dump(update_status, f, indent=2)
            logger.info("update_status_written inserted_rows=%s", inserted_rows)
    except Exception as e:
        logger.error("update_status_write_failed error=%s", e)


async def get_medal_metadata(client):
    """Fetch medal metadata to get proper names"""
    global medal_cache
    if medal_cache:
        return medal_cache
    
    try:
        metadata_response = await client.gamecms_hacs.get_medal_metadata()
        metadata = await metadata_response.parse()
        
        if hasattr(metadata, 'medals'):
            for medal in metadata.medals:
                if hasattr(medal, 'name_id') and hasattr(medal, 'name'):
                    medal_id = medal.name_id
                    medal_name = medal.name.value if hasattr(medal.name, 'value') else str(medal.name)
                    medal_cache[medal_id] = medal_name
                    medal_cache[str(medal_id)] = medal_name
    except Exception as e:
        logger.warning("medal_metadata_fetch_failed error=%s", e)
    
    return medal_cache

async def fetch_metadata(client, match_id, asset_id, version_id, cache, fetch_func):
    """Generic function to fetch and cache metadata"""
    key = f"{asset_id}:{version_id}"
    if key in cache:
        return cache[key]
    
    try:
        response = await fetch_func(asset_id, version_id)
        data = await response.parse()
        
        # Try common name attributes
        for attr in ['name', 'asset_name', 'internal_name', 'display_name', 'public_name', 'title']:
            if hasattr(data, attr):
                name = getattr(data, attr)
                cache[key] = name
                return name
                
        # If properties exist, try those too
        properties = get_or_default(data, 'properties')
        if properties:
            for attr in ['name', 'display_name', 'game_mode', 'variant_name']:
                if hasattr(properties, attr):
                    name = getattr(properties, attr)
                    cache[key] = name
                    return name
    except Exception as e:
        logger.warning("metadata_fetch_failed match_id=%s asset_id=%s error=%s", match_id, asset_id, e)
    
    return f"ID: {asset_id}"

def process_medals(medals_data, match_data, medal_names):
    """Process medal data into the match record with defensive coding"""
    if not medals_data:
        return
    
    try:
        # Ensure medals_data is iterable
        if not hasattr(medals_data, '__iter__'):
            logger.warning("medals_not_iterable type=%s", type(medals_data))
            return
            
        for medal in medals_data:
            # Defensive check for medal object
            if medal is None:
                continue
                
            medal_id = get_or_default(medal, 'name_id')
            medal_count = get_or_default(medal, 'count', default=0)
            
            if not medal_id:
                continue
                
            # Use medal name if available, otherwise use ID
            if medal_names and str(medal_id) in medal_names:
                medal_name = medal_names[str(medal_id)]
                # Clean the name for use as a column name
                clean_name = ''.join(c if c.isalnum() else '_' for c in medal_name)
                column_name = f"medal_{clean_name}"
            else:
                column_name = f"medal_id_{medal_id}"
                
            match_data[column_name] = medal_count
    except Exception as e:
        logger.exception("medal_processing_failed error=%s", e)

async def process_match(
    client,
    player_info,
    match_id,
    match_number,
    results,
    medal_names,
    engine=None,
    inserted_counter: dict | None = None,
):
    """Process a single match for a player"""
    player_gamertag = player_info["gamertag"]
    player_xuid = clean_xuid(player_info["xuid"])

    logger.info("process_match_start match_id=%s gamertag=%s match_number=%s", match_id, player_gamertag, match_number)

    try:
        # Fetch match stats
        match_stats_response = await client.stats.get_match_stats(match_id)
        match_stats = await match_stats_response.parse()

        if not match_stats or not getattr(match_stats, "players", None):
            logger.warning("match_stats_missing match_id=%s gamertag=%s", match_id, player_gamertag)
            return False

        match_date = match_stats.match_info.start_time
        match_duration = match_stats.match_info.duration

        # Metadata
        game_variant = get_or_default(match_stats.match_info, 'ugc_game_variant')
        game_type = await fetch_metadata(client, match_id, game_variant.asset_id, game_variant.version_id, {}, lambda a, v: client.discovery_ugc.get_ugc_game_variant(a, v)) if game_variant else "Unknown"

        map_variant = get_or_default(match_stats.match_info, 'map_variant')
        map_name = await fetch_metadata(client, match_id, map_variant.asset_id, map_variant.version_id, {}, lambda a, v: client.discovery_ugc.get_map(a, v)) if map_variant else "Unknown"

        playlist_obj = get_or_default(match_stats.match_info, 'playlist')
        playlist_id = get_or_default(playlist_obj, 'asset_id')
        version_id = get_or_default(playlist_obj, 'version_id')
        playlist = await fetch_metadata(client, match_id, playlist_id, version_id, {}, lambda a, v: client.discovery_ugc.get_playlist(a, v)) if playlist_id and version_id else "Unknown"

        # Find player in match
        for player in match_stats.players:
            current_xuid = clean_xuid(get_or_default(player, 'player_id'))
            if current_xuid != player_xuid:
                continue

            player_team_id = get_or_default(player, 'last_team_id', default=0)
            outcome = get_or_default(player, 'outcome', default="Unknown")
            readable_outcome = OUTCOMES.get(outcome, f"Unknown ({outcome})") if str(outcome).isdigit() else str(outcome)

            team_rank = 0
            # Real per-player placement rank in the match (#4 — was hard-coded 0).
            player_rank = get_or_default(player, 'rank', default=0)
            friendly_team_stats = {}
            enemy_team_stats = {}

            for team in get_or_default(match_stats, 'teams', default=[]):
                team_id = get_or_default(team, 'team_id')
                is_player_team = (team_id == player_team_id)
                if is_player_team:
                    # The player's team's actual finishing rank (was always 0).
                    team_rank = get_or_default(team, 'rank', default=0) or 0
                team_stats_dict = friendly_team_stats if is_player_team else enemy_team_stats
                team_prefix = 'team_' if is_player_team else 'enemy_team_'

                if hasattr(team, 'stats') and team.stats:
                    if hasattr(team.stats, 'core_stats'):
                        for stat_name, stat_value in vars(team.stats.core_stats).items():
                            if not stat_name.startswith('_'):
                                if stat_name == 'medals' and stat_value:
                                    team_stats_dict[f'{team_prefix}medal_count'] = len(stat_value)
                                    # 'Got perfected' tracker: per-side Perfect-medal count.
                                    # (The API records the EARNER of a medal, never the victim,
                                    # so this is the closest truth that exists.)
                                    _perf = 0
                                    for _m in stat_value:
                                        _mid = get_or_default(_m, 'name_id')
                                        if _mid is not None and medal_names and medal_names.get(str(_mid)) == 'Perfect':
                                            _perf += int(get_or_default(_m, 'count', default=0) or 0)
                                    team_stats_dict[f'{team_prefix}perfects'] = _perf
                                else:
                                    team_stats_dict[f'{team_prefix}{stat_name}'] = stat_value
                    for category_name in vars(team.stats):
                        if category_name != 'core_stats':
                            cat_stats = getattr(team.stats, category_name, None)
                            if cat_stats:
                                for stat_name, stat_value in vars(cat_stats).items():
                                    team_stats_dict[f'{team_prefix}{category_name}_{stat_name}'] = stat_value or 0

            match_ts = pd.Timestamp(match_date)
            if match_ts.tzinfo is None:
                match_ts = match_ts.tz_localize(dt_timezone.utc)
            match_ts = match_ts.tz_convert(dt_timezone.utc)

            match_data = {
                'player_gamertag': player_gamertag,
                'player_xuid': player_xuid,
                'match_id': match_id,
                'date': match_ts.replace(microsecond=0).isoformat(),
                'duration': str(match_duration),
                'game_type': game_type,
                'map': map_name,
                'playlist': playlist,
                'playlist_id': playlist_id or 'Unknown',
                'outcome': readable_outcome,
                'team_id': player_team_id,
                'team_rank': team_rank,
                'player_rank': player_rank,
                'season_id': get_or_default(match_stats.match_info, 'season_id'),
            }

            match_data.update(friendly_team_stats)
            match_data.update(enemy_team_stats)

            # 🆕 Fetch pre/post CSR + skill performance (team MMR, expected-vs-actual
            # K/D, counterfactuals) from the rank recap — one call, was CSR-only.
            skill = await get_match_skill_full(client, match_id, player_xuid)
            pre_csr, post_csr = skill['pre_match_csr'], skill['post_match_csr']
            match_data['pre_match_csr'] = pre_csr  # None stored as NULL; 0 is a valid CSR value
            match_data['post_match_csr'] = post_csr
            for _sk in ('team_mmr', 'enemy_team_mmr', 'expected_kills', 'expected_deaths',
                        'kills_std_dev', 'deaths_std_dev', 'cf_expected_kills', 'cf_expected_deaths'):
                match_data[_sk] = skill.get(_sk)
            logger.debug(
                "csr_recap gamertag=%s match_id=%s pre_match_csr=%s post_match_csr=%s",
                player_gamertag,
                match_id,
                pre_csr,
                post_csr,
            )

            # Fetch full playlist CSR data (current/season_max/all_time_max)
            try:
                default_ranked_playlist = "edfef3ac-9cbe-4fa2-b949-8f29deafd483"  # Default Ranked Arena playlist
                playlist_to_check = playlist_id if playlist_id else default_ranked_playlist

                playlist_csr_response = await client.skill.get_playlist_csr(
                    playlist_id=playlist_to_check,
                    xuids=[player_xuid]
                )

                if playlist_csr_response:
                    playlist_csr_data = await playlist_csr_response.parse()
                    if hasattr(playlist_csr_data, 'value') and playlist_csr_data.value:
                        for player_data in playlist_csr_data.value:
                            if clean_xuid(get_or_default(player_data, 'id')) == player_xuid:
                                result = get_or_default(player_data, 'result')
                                if result:
                                    for csr_type in ['current', 'season_max', 'all_time_max']:
                                        csr_obj = get_or_default(result, csr_type)
                                        if csr_obj and hasattr(csr_obj, '__dict__'):
                                            for k, v in vars(csr_obj).items():
                                                if not k.startswith('_'):
                                                    match_data[f'{csr_type}_csr_{k}'] = v

            except Exception as e:
                logger.warning("playlist_csr_fetch_failed gamertag=%s match_id=%s error=%s", player_gamertag, match_id, e)

            # Fallback: if the playlist-CSR call didn't populate the rank tier
            # (common — the API often returns it empty), derive it from the
            # post-match CSR so rank is first-class everywhere going forward.
            try:
                if not match_data.get('current_csr_tier') and post_csr:
                    tier, sub = csr_to_tier(post_csr)
                    if tier:
                        match_data['current_csr_tier'] = tier
                        if sub is not None:
                            match_data['current_csr_sub_tier'] = sub
                        if not match_data.get('current_csr_value'):
                            match_data['current_csr_value'] = post_csr
            except Exception as e:
                logger.warning("csr_tier_derive_failed gamertag=%s error=%s", player_gamertag, e)

            # 🧮 Player Stats
            player_team_stats = get_or_default(player, 'player_team_stats', default=[])
            if player_team_stats:
                stats = get_or_default(player_team_stats[0], 'stats')
                if stats:
                    core = get_or_default(stats, 'core_stats')
                    if core:
                        for stat_name, stat_value in vars(core).items():
                            if stat_name == 'medals':
                                match_data['medal_count'] = len(stat_value)
                                process_medals(stat_value, match_data, medal_names)
                            elif stat_name == 'accuracy':
                                match_data['accuracy'] = stat_value
                            else:
                                match_data[stat_name] = stat_value

                    for category in vars(stats):
                        if category != 'core_stats':
                            cat_stats = getattr(stats, category, None)
                            if cat_stats:
                                for stat_name, stat_value in vars(cat_stats).items():
                                    match_data[f"{category}_{stat_name}"] = stat_value or 0

            # Store the full API-derived payload so we never lose fields.
            # (Calculated fields are added after this snapshot.)
            raw_payload = match_data.copy()
            match_data["raw_json"] = json.dumps(raw_payload, default=str)
            match_data["scraped_at"] = datetime.now(dt_timezone.utc).isoformat()

            match_data = add_calculated_fields(match_data)
            normalized_match_data = normalize_row(match_data, all_db_columns())
            if results is not None:
                results.append(normalized_match_data)

            if engine is not None:
                if write_single_result_to_db(normalized_match_data, engine):
                    try:
                        write_extra_stats_to_kv(
                            engine,
                            player_xuid=player_xuid,
                            match_id=match_id,
                            payload=raw_payload,
                            scraped_at_iso=match_data.get("scraped_at"),
                        )
                    except Exception as exc:
                        logger.warning("extra_stats_persist_failed gamertag=%s match_id=%s error=%s", player_gamertag, match_id, exc)
                    if inserted_counter is not None:
                        inserted_counter["count"] = int(inserted_counter.get("count", 0)) + 1
                        # Keep update_status.json fresh during long runs
                        write_update_status(int(inserted_counter["count"]))

            # Capture the full per-match scoreboard (all players, incl. opponents)
            # once per match — powers the nemesis view + full match scoreboard.
            if engine is not None:
                try:
                    await capture_match_players(client, engine, match_id, match_stats,
                                                match_date, playlist, map_name)
                except Exception as exc:
                    logger.warning("match_players_capture_failed match_id=%s error=%s", match_id, exc)
            return True

        logger.warning("player_not_found_in_match gamertag=%s match_id=%s", player_gamertag, match_id)
        return False

    except Exception as e:
        logger.exception("process_match_failed match_id=%s gamertag=%s error=%s", match_id, player_gamertag, e)
        return False

async def process_player(
    client,
    player_info,
    results,
    medal_names,
    max_matches=None,
    force_refresh=False,
    existing_match_ids=None,
    engine=None,
    inserted_counter: dict | None = None,
):
    player_gamertag = player_info["gamertag"]
    player_xuid = clean_xuid(player_info["xuid"])
    existing_match_ids = existing_match_ids or set()

    total_seen = 0
    start = 0
    page_size = 25
    force_refresh_latched = bool(force_refresh)

    logger.info("process_player_start gamertag=%s xuid=%s max_matches=%s force_refresh=%s", player_gamertag, player_xuid, max_matches or 'all', force_refresh_latched)

    while True:
        # --- RETRY LOGIC FOR MATCH HISTORY ---
        retries = 0
        max_retries = 5
        match_history = None

        while retries < max_retries:
            try:
                history_response = await client.stats.get_match_history(
                    player=player_xuid,
                    start=start,
                    count=page_size,
                    match_type='all'
                )
                match_history = await history_response.parse()
                break # Success, exit retry loop
            except ClientResponseError as e:
                if e.status == 429:
                    wait_time = (2 ** retries) + random.uniform(0.5, 2.0)
                    # Rate limit hit - sleeping silently
                    await asyncio.sleep(wait_time)
                    retries += 1
                else:
                    logger.error("match_history_fetch_failed gamertag=%s start=%s error=%s", player_gamertag, start, e)
                    raise e # Raise non-429 errors
        
        if match_history is None:
            logger.error("match_history_exhausted gamertag=%s retries=%s", player_gamertag, max_retries)
            return
        # -------------------------------------

        results_batch = match_history.results
        logger.info("match_history_batch gamertag=%s start=%s batch_size=%s", player_gamertag, start, len(results_batch))

        if not results_batch:
            break

        skipped_count = 0
        processed_count = 0
        
        for match_result in results_batch:
            effective_limit = max_matches if max_matches is not None else get_match_limit()
            if effective_limit is not None and total_seen >= effective_limit:
                logger.info("match_limit_reached gamertag=%s limit=%s", player_gamertag, effective_limit)
                return

            match_id = match_result.match_id
            total_seen += 1
            
            # Ensure match_id is a string for comparison
            match_id_str = str(match_id)

            if not force_refresh_latched and match_id_str in existing_match_ids:
                skipped_count += 1
                continue

            # PACING: Sleep slightly to prevent hammering the API during individual match processing
            await asyncio.sleep(0.5)

            try:
                await process_match(
                    client, player_info, match_id_str, total_seen,
                    results, medal_names, engine=engine, inserted_counter=inserted_counter
                )
                processed_count += 1
                # Avoid re-processing duplicates within this run.
                existing_match_ids.add(match_id_str)
            except ClientResponseError as e:
                if e.status == 429:
                    logger.warning("process_match_rate_limited gamertag=%s match_id=%s wait_seconds=5", player_gamertag, match_id_str)
                    await asyncio.sleep(5)
                else:
                    logger.warning("process_match_client_error gamertag=%s match_id=%s error=%s", player_gamertag, match_id_str, e)
        
        if skipped_count > 0:
            logger.info("player_batch_complete gamertag=%s skipped=%s processed=%s", player_gamertag, skipped_count, processed_count)

        # SCRAPE-SPEED: stop paginating once a page yields NO new matches. Halo
        # match history is newest-first, so if nothing on this page was new,
        # everything older is already stored — no reason to keep fetching pages.
        # (Was scanning ~150 matches/player every cycle even when 0 were new,
        # making each cycle ~60s. Now a quiet cycle fetches ~1 page/player.)
        # Skipped only on a force-refresh (which deliberately re-scans deep) and
        # for a brand-new player (every match is new, so it scans full history).
        if processed_count == 0 and not force_refresh_latched:
            break

        if len(results_batch) < page_size:
            break

        start += page_size


def reorder_columns(columns):
    # Core identifiers
    core_fields = [
        'player_gamertag', 'player_xuid', 'match_id', 'date', 'duration',
        'game_type', 'map', 'playlist', 'playlist_id',
        'outcome', 'team_id', 'team_rank'
    ]

    # Performance
    perf_fields = ['kills', 'deaths', 'assists', 'kda', 'accuracy', 'score', 'medal_count']

    # Custom calculated columns — always include them
    calc_fields = ['dmg/ka', 'dmg/death', 'dmg/min', 'dmg_difference']

    # Medals
    named_medals = sorted([c for c in columns if c.startswith('medal_') and not c.startswith('medal_id_') and c != 'medal_count'])
    id_medals = sorted([c for c in columns if c.startswith('medal_id_')])

    # CSR fields
    csr_fields = sorted([c for c in columns if c.startswith(('current_csr_', 'season_max_csr_', 'all_time_max_csr_'))])

    # Team stats
    team_fields = sorted([c for c in columns if c.startswith('team_')])
    enemy_team_fields = sorted([c for c in columns if c.startswith('enemy_team_')])

    # Remaining = player stats and misc
    used = set(core_fields + perf_fields + calc_fields + named_medals + id_medals + csr_fields + team_fields + enemy_team_fields)
    misc_fields = sorted([c for c in columns if c not in used])

    # Final order
    ordered = (
        core_fields +
        perf_fields +
        calc_fields +  # Always include them here
        named_medals +
        id_medals +
        csr_fields +
        misc_fields +
        team_fields +
        enemy_team_fields
    )
    return ordered

def add_calculated_fields(row):
    """Add calculated fields to a match data row with robust error handling"""
    # Initialize calculated fields with default values
    calculated_fields = {
        "dmg/ka": 0,
        "dmg/death": 0,
        "dmg/min": 0,
        "dmg_difference": 0
    }
    
    # If row is None, return a dictionary with default calculated fields
    if row is None:
        return calculated_fields
        
    # Create a copy of the row to avoid modifying the original
    result = row.copy() if isinstance(row, dict) else {}
    
    try:
        # Use defensive conversion with fallbacks
        kills = float(row.get("kills", 0) or 0)
        assists = float(row.get("assists", 0) or 0)
        deaths = float(row.get("deaths", 0) or 0)
        damage_dealt = float(row.get("damage_dealt", 0) or 0)
        damage_taken = float(row.get("damage_taken", 0) or 0)
        duration_str = row.get("duration", "0:00.0")

        duration_seconds = parse_duration_to_seconds(duration_str)

        # Calculate
        ka = kills + assists
        if ka > 0:
            result["dmg/ka"] = round(damage_dealt / ka, 2)
        else:
            result["dmg/ka"] = 0
            
        if deaths > 0:
            result["dmg/death"] = round(damage_dealt / deaths, 2)
        else:
            result["dmg/death"] = 0
            
        if duration_seconds > 0:
            result["dmg/min"] = round(damage_dealt / (duration_seconds / 60), 2)
        else:
            result["dmg/min"] = 0
            
        result["dmg_difference"] = round(damage_dealt - damage_taken, 2)

    except Exception as e:
        logger.warning("derived_fields_calculation_failed error=%s", e)
        # Add the default calculated fields if there was an error
        result.update(calculated_fields)

    # Ensure all calculated fields exist in the result
    for field in calculated_fields:
        if field not in result:
            result[field] = calculated_fields[field]
            
    return result

def backfill_csr_tiers(engine) -> None:
    """One-time idempotent backfill: derive current_csr_tier/sub_tier from
    post_match_csr for historical rows the playlist-CSR API left empty. Only
    touches NULL rows, so it's a cheap no-op once everything is populated."""
    # Done in Python rather than SQL: post_match_csr is stored loosely (some rows
    # hold ''/non-numeric), and Postgres evaluates casts unpredictably vs. WHERE
    # filters, so a pure-SQL UPDATE kept choking on '' → double. float() in Python
    # parses safely; an executemany UPDATE keyed on (player_xuid, match_id) is fast
    # and idempotent (after the first pass there are no NULL-tier rows left).
    try:
        with engine.begin() as conn:
            rows = conn.execute(text(
                "SELECT player_xuid, match_id, post_match_csr FROM halo_match_stats "
                "WHERE current_csr_tier IS NULL OR current_csr_tier = ''"
            )).fetchall()
            params = []
            for player_xuid, match_id, pcsr in rows:
                try:
                    csr = float(str(pcsr).strip())
                except (TypeError, ValueError):
                    continue
                if csr <= 0:
                    continue
                tier, sub = csr_to_tier(csr)
                if not tier:
                    continue
                params.append({"t": tier, "s": sub,
                               "x": player_xuid, "m": str(match_id)})
            if params:
                conn.execute(text(
                    "UPDATE halo_match_stats SET current_csr_tier = :t, "
                    "current_csr_sub_tier = :s "
                    "WHERE player_xuid = :x AND match_id = :m"
                ), params)
                logger.info("csr_tier_backfill rows=%s", len(params))
    except Exception as e:
        logger.warning("csr_tier_backfill_failed error=%s", e)


async def run_stats(max_matches=None, force_refresh=False):
    if not PLAYERS:
        # First-run: nothing to scrape yet. Idle gracefully — entrypoint.py
        # sleep-loops and re-runs this module, so the roster saved via the
        # webapp's /setup page is picked up on the next cycle. Still refresh
        # update_status.json so the container healthcheck stays green while idle.
        logger.info("no players configured — finish setup at /setup (or set HALO_TRACKED_PLAYERS)")
        write_update_status(0)
        return
    tokens = load_tokens()
    # We still keep a small in-memory list for debugging, but DB writes happen per match.
    results = []
    engine = get_engine()
    ensure_schema(engine)
    backfill_csr_tiers(engine)

    # Clear stale collation version (Alpine musl doesn't provide one)
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(
                "UPDATE pg_database SET datcollversion = NULL "
                "WHERE datname = current_database() AND datcollversion IS NOT NULL"
            ))
    except Exception as exc:
        logger.warning("collation_version_clear_failed error=%s", exc)

    # If caller passes explicit overrides, they stay fixed for the whole run.
    # Otherwise, per-player/per-page logic will pick up Settings changes mid-run.
    force_refresh_override = bool(force_refresh)
    match_limit_override = max_matches if max_matches is not None else None

    # Refresh can only be enabled explicitly by caller.
    force_refresh_latched = force_refresh_override

    effective_limit_for_log = match_limit_override if match_limit_override is not None else get_match_limit()
    logger.info(
        "stats_run_start match_limit=%s force_refresh=%s players=%s",
        effective_limit_for_log,
        force_refresh_latched,
        len(PLAYERS),
    )

    inserted_counter = {"count": 0}

    clearance_token = tokens.get("clearance_token") or ""
    if not clearance_token:
        logger.warning("clearance_token_missing scraper will attempt to run but some API calls may fail")

    async with ClientSession() as session:
        client = HaloInfiniteClient(
            session=session,
            spartan_token=tokens["spartan_token"],
            clearance_token=clearance_token,
        )

        medal_names = await get_medal_metadata(client)

        pre_backfill_count = int(inserted_counter.get("count", 0))
        for player in PLAYERS:
            xuid = clean_xuid(player["xuid"])
            existing_match_ids = set()
            existing_match_ids = get_existing_match_ids(engine, xuid)
            await process_player(
                client,
                player,
                results,
                medal_names,
                max_matches=match_limit_override,
                force_refresh=force_refresh_latched,
                existing_match_ids=existing_match_ids,
                engine=engine,
                inserted_counter=inserted_counter,
            )

        # Pause the heavy historical backfills for the WHOLE active session, not
        # just cycles that happened to capture a game — so every cycle during a
        # session stays fast (~10s) and a new game is detected promptly instead
        # of waiting behind a ~60-90s backfill. A session is "active" while the
        # latest tracked match is within HALO_ACTIVE_WINDOW_MIN (45m); backfills
        # resume once truly idle. HALO_BACKFILL_ALWAYS=1 forces them on.
        new_this_cycle = int(inserted_counter.get("count", 0)) - pre_backfill_count
        _session_active = False
        try:
            _latest = get_latest_match_at(engine)
            if _latest:
                _ts = pd.Timestamp(_latest)
                if _ts.tzinfo is None:
                    _ts = _ts.tz_localize(dt_timezone.utc)
                _age_min = (pd.Timestamp.now(tz=dt_timezone.utc) - _ts).total_seconds() / 60.0
                _session_active = _age_min <= int(os.getenv("HALO_ACTIVE_WINDOW_MIN", "45"))
        except Exception:
            _session_active = False
        # Xbox presence: anyone IN Halo means a session could start any minute —
        # hold API-hungry backfills even before the first match lands.
        if not _session_active:
            try:
                with open(data_path("presence.json")) as _pf:
                    _snap = json.load(_pf)
                if (time.time() - float(_snap.get("updated") or 0) < 300
                        and any(v.get("in_halo") for v in (_snap.get("players") or {}).values())):
                    _session_active = True
            except Exception:
                pass
        run_backfills = (new_this_cycle == 0 and not _session_active) \
            or os.getenv("HALO_BACKFILL_ALWAYS", "0") == "1"

        # Theater film events (exact kill/death/medal times) — EVERY cycle so
        # tonight's games get precise highlight timing while the squad plays;
        # small budget when active, bigger when idle to chew through history.
        if os.getenv("HALO_FILM_EVENTS", "1") == "1":
            try:
                from film_events import backfill_film_events
                await backfill_film_events(
                    client, engine,
                    limit=8 if _session_active else int(os.getenv("HALO_FILM_EVENTS_LIMIT", "50")),
                    time_budget_s=12 if _session_active else int(os.getenv("HALO_FILM_EVENTS_SECONDS", "25")),
                )
            except Exception as exc:
                logger.warning("film_events_failed error=%s", exc)

        if run_backfills:
            # Backfill the full lobby (opponents) for historical matches that predate
            # the halo_match_players capture — time-budgeted per cycle, resumable.
            if os.getenv("HALO_MATCH_PLAYERS_BACKFILL", "1") == "1":
                try:
                    await backfill_match_players(
                        client, engine,
                        candidate_limit=int(os.getenv("HALO_MATCH_PLAYERS_BACKFILL_LIMIT", "600")),
                        time_budget_s=int(os.getenv("HALO_MATCH_PLAYERS_BACKFILL_SECONDS", "25")),
                    )
                except Exception as exc:
                    logger.warning("match_players_backfill_failed error=%s", exc)

            # Backfill team MMR + expected-vs-actual K/D onto historical tracked rows.
            if os.getenv("HALO_SKILL_BACKFILL", "1") == "1":
                try:
                    await backfill_skill_stats(
                        client, engine,
                        candidate_limit=int(os.getenv("HALO_SKILL_BACKFILL_LIMIT", "400")),
                        time_budget_s=int(os.getenv("HALO_SKILL_BACKFILL_SECONDS", "20")),
                    )
                except Exception as exc:
                    logger.warning("skill_backfill_failed error=%s", exc)

            # Backfill opponent CSR for older matches (bounded per cycle).
            try:
                await backfill_opponent_csr(
                    client, engine,
                    limit=int(os.getenv("HALO_OPP_CSR_BACKFILL", "400")),
                )
            except Exception as exc:
                logger.warning("opp_csr_backfill_failed error=%s", exc)

            # Re-resolve opponents stuck as Spartan-NNNN placeholders.
            try:
                await backfill_opponent_names(
                    client, engine,
                    limit=int(os.getenv("HALO_OPP_NAME_BACKFILL", "300")),
                )
            except Exception as exc:
                logger.warning("opp_name_backfill_failed error=%s", exc)

            # Fill historical 'got perfected' counts (bounded per cycle).
            try:
                await backfill_perfects(
                    client, engine,
                    limit=int(os.getenv("HALO_PERFECTS_BACKFILL", "120")),
                    time_budget_s=int(os.getenv("HALO_PERFECTS_BACKFILL_SECONDS", "20")),
                )
            except Exception as exc:
                logger.warning("perfects_backfill_failed error=%s", exc)

    inserted_rows = int(inserted_counter.get("count", 0))
    # Final index ensure (cheap if already exists)
    try:
        if table_exists(engine):
            ensure_indexes(engine)
    except Exception:
        pass
    for player in PLAYERS:
        effective_trim_limit = match_limit_override if match_limit_override is not None else get_match_limit()
        trim_player_history(engine, clean_xuid(player["xuid"]), effective_trim_limit)
    write_update_status(inserted_rows, engine=engine)

    # Best-effort push notifications (rank/streak/PB/session). Never blocks.
    try:
        from notify import process_notifications
        process_notifications(engine, PLAYERS)
    except Exception as exc:
        logger.warning("notifications_failed error=%s", exc)

def convert_time_columns_to_seconds(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convert all time-based columns in TIME_COLUMNS to seconds.
    Handles formats like:
    - MM:SS.s
    - H:MM:SS
    - 0 days 00:00:25.400000 (pandas timedelta string)

    Returns the updated DataFrame.
    """

    def parse_time_to_seconds(time_str):
        if not isinstance(time_str, str):
            return 0.0
        try:
            time_str = time_str.strip()

            # Handle '0 days HH:MM:SS.microseconds'
            if "days" in time_str:
                time_part = time_str.split("days")[-1].strip()
                h, m, s = time_part.split(":")
                return float(h) * 3600 + float(m) * 60 + float(s)

            # Handle HH:MM:SS
            if time_str.count(':') == 2:
                h, m, s = time_str.split(':')
                return float(h) * 3600 + float(m) * 60 + float(s)

            # Handle MM:SS
            elif time_str.count(':') == 1:
                m, s = time_str.split(':')
                return float(m) * 60 + float(s)

            # Handle pure seconds
            elif time_str.replace('.', '', 1).isdigit():
                return float(time_str)

        except Exception as e:
            logger.warning("time_value_conversion_failed value=%s error=%s", time_str, e)
        return 0.0

    for col in TIME_COLUMNS:
        if col in df.columns:
            try:
                df[col] = df[col].astype(str).apply(parse_time_to_seconds)
                logger.debug("time_column_converted column=%s", col)
            except Exception as e:
                logger.warning("time_column_conversion_failed column=%s error=%s", col, e)
        else:
            logger.debug("time_column_missing column=%s", col)

    return df

if __name__ == "__main__":
    # Run with no max_matches limit to get all matches
    # asyncio.run(run_stats(max_matches=None))
    
    # Or run with a specific limit (e.g., 10000 matches per player)
    asyncio.run(run_stats(max_matches=None))
    
