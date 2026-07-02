"""Shared tracked-player roster.

Single source of truth for which players the scraper and webapp track.

Loading priority (highest first):
  1. players.json in the data dir (written by the /setup page)
  2. HALO_TRACKED_PLAYERS env var (JSON array of {"gamertag", "xuid"})
  3. empty list — the app runs but shows the first-run /setup flow

Both sources use the same shape: a JSON array of objects with "gamertag" and
"xuid" keys. Callers should re-read (call load_players()) rather than caching
long-term, so roster changes made in /setup apply without a restart.
"""
import json
import logging
import os

from halo_paths import data_path

logger = logging.getLogger(__name__)

PLAYERS_PATH = data_path("players.json")


def _normalize(raw) -> list | None:
    """Validate a decoded players payload → [{'gamertag','xuid'}] or None."""
    if not isinstance(raw, list):
        return None
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            return None
        gamertag = str(entry.get("gamertag") or "").strip()
        xuid = str(entry.get("xuid") or "").strip()
        if not gamertag or not xuid:
            return None
        out.append({"gamertag": gamertag, "xuid": xuid})
    return out


def _from_file() -> list | None:
    """Roster from players.json, or None when absent/invalid/empty."""
    try:
        if not PLAYERS_PATH.exists():
            return None
        raw = json.loads(PLAYERS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("players_json_read_failed path=%s error=%s", PLAYERS_PATH, exc)
        return None
    players = _normalize(raw)
    if not players:
        return None
    return players


def _from_env() -> list | None:
    raw = os.getenv("HALO_TRACKED_PLAYERS", "").strip()
    if not raw:
        return None
    try:
        players = _normalize(json.loads(raw))
    except ValueError as exc:
        logger.warning("tracked_players_env_parse_failed error=%s", exc)
        return None
    if players is None:
        logger.warning("tracked_players_env_invalid reason=bad_shape")
        return None
    return players or None


def load_players() -> list:
    """Tracked players as [{'gamertag': ..., 'xuid': ...}], possibly empty."""
    players = _from_file()
    if players is not None:
        return players
    return _from_env() or []


def save_players(players: list) -> None:
    """Persist the roster to players.json (atomic-ish write)."""
    normalized = _normalize(players)
    if normalized is None:
        raise ValueError("players must be a list of {gamertag, xuid} objects")
    tmp = PLAYERS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(normalized, indent=2), encoding="utf-8")
    tmp.replace(PLAYERS_PATH)
    logger.info("players_json_saved count=%s", len(normalized))
