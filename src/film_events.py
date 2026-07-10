"""Halo Theater film events — the ground truth for WHEN things happened.

Match stats only say a player earned e.g. two Double Kills; the THEATER film
for the match records every kill, death and medal with a millisecond offset
from match start. Combined with the match's wall-clock start time this gives
exact real-world timestamps for every moment — which is what downstream
consumers (MultiTwitch highlight reels / clip cutting) need so clips land on
the actual play instead of "somewhere in that match".

Fetched per match via SPNKr's film API (one film-metadata call + one chunk
download), newest-first over a recent window, time-budgeted per scraper
cycle. Films publish a little after the match ends, so a failed fetch for a
match younger than 2 hours is retried next cycle; older failures are marked
permanently so they aren't re-queried forever.

Requires spnkr>=0.10.2 (film module). If the installed spnkr lacks it, the
backfill logs once and no-ops.
"""
import asyncio
import logging
import time as _time

from sqlalchemy import text

logger = logging.getLogger(__name__)

FILM_WINDOW_DAYS = 14          # only fetch films for matches this recent
FILM_RETRY_YOUNG_SECS = 2 * 3600   # films lag the match end — retry young failures

_film_schema_done = False
_film_import_warned = False


def _ensure_film_schema(engine) -> None:
    global _film_schema_done
    if _film_schema_done:
        return
    with engine.begin() as conn:
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS halo_film_events (
                match_id   TEXT NOT NULL,
                xuid       TEXT,
                gamertag   TEXT,
                event_type TEXT NOT NULL,
                time_ms    BIGINT NOT NULL,
                medal_name TEXT
            )
            """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_hfe_match ON halo_film_events (match_id)"))
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS halo_film_status (
                match_id TEXT PRIMARY KEY,
                status   TEXT NOT NULL,
                tried_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """))
    _film_schema_done = True


async def backfill_film_events(client, engine, limit=50, time_budget_s=25):
    """Fetch + store theater highlight events for recent matches missing them."""
    global _film_import_warned
    try:
        from spnkr.film.api import read_highlight_events
    except Exception as exc:
        if not _film_import_warned:
            logger.warning("film_events unavailable (spnkr too old?): %s", exc)
            _film_import_warned = True
        return

    try:
        _ensure_film_schema(engine)
        with engine.connect() as conn:
            rows = conn.execute(text(
                """
                SELECT m.match_id, EXTRACT(EPOCH FROM MAX(m.date)) AS start_epoch
                FROM halo_match_stats m
                LEFT JOIN halo_film_status s ON s.match_id = m.match_id
                WHERE m.date > NOW() - INTERVAL ':d days'
                  AND m.playlist ILIKE 'Ranked Arena'
                  AND s.match_id IS NULL
                GROUP BY m.match_id
                ORDER BY MAX(m.date) DESC
                LIMIT :lim
                """.replace(':d', str(int(FILM_WINDOW_DAYS)))), {"lim": int(limit)}).fetchall()
    except Exception as exc:
        logger.warning("film_events_query_failed error=%s", exc)
        return
    if not rows:
        return

    start = _time.monotonic()
    fetched = failed = 0
    now = _time.time()
    for mid, start_epoch in rows:
        if _time.monotonic() - start > time_budget_s:
            break
        mid = str(mid)
        try:
            events = await read_highlight_events(client, mid)
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM halo_film_events WHERE match_id = :m"),
                             {"m": mid})
                for e in events:
                    if e.event_type not in ("kill", "death", "medal"):
                        continue
                    conn.execute(text(
                        "INSERT INTO halo_film_events "
                        "(match_id, xuid, gamertag, event_type, time_ms, medal_name) "
                        "VALUES (:m, :x, :g, :t, :ms, :md)"),
                        {"m": mid, "x": str(e.xuid), "g": e.gamertag,
                         "t": e.event_type, "ms": int(e.time_ms),
                         "md": e.medal_name})
                conn.execute(text(
                    "INSERT INTO halo_film_status (match_id, status) VALUES (:m, 'ok') "
                    "ON CONFLICT (match_id) DO UPDATE SET status = 'ok', tried_at = NOW()"),
                    {"m": mid})
            fetched += 1
        except Exception as exc:
            failed += 1
            # Films publish AFTER the match — leave young matches unmarked so
            # the next cycle retries; only permanently skip old ones.
            age = now - float(start_epoch or 0)
            if age > FILM_RETRY_YOUNG_SECS:
                try:
                    with engine.begin() as conn:
                        conn.execute(text(
                            "INSERT INTO halo_film_status (match_id, status) VALUES (:m, :s) "
                            "ON CONFLICT (match_id) DO UPDATE SET status = EXCLUDED.status, tried_at = NOW()"),
                            {"m": mid, "s": f"failed: {str(exc)[:120]}"})
                except Exception:
                    pass
            logger.info("film_events_match_skipped match_id=%s age_min=%.0f error=%s",
                        mid[:8], age / 60, str(exc)[:120])
        await asyncio.sleep(0.2)
    if fetched or failed:
        logger.info("film_events fetched=%s failed=%s candidates=%s", fetched, failed, len(rows))
