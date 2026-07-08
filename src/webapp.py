import html
import json
import logging
import os
import re
import hmac
import secrets
import threading
import time
from itertools import combinations
import pandas as pd
import requests
from pathlib import Path
from pytz import UTC, UnknownTimeZoneError, timezone as pytz_timezone
from flask import Flask, render_template, request, redirect, url_for, Response, session, jsonify
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import text, bindparam
from halo_paths import data_path
from util import *  # leaf helpers (extracted to util.py)
import push  # web-push (VAPID) helpers — lazy heavy imports, safe at boot
import twitch_live  # direct Helix live-status (drops the MultiTwitch dependency)

# Setup Flask with correct template and static paths for Docker
APP_ROOT = Path(__file__).parent.parent  # Go up from /app/src to /app
TEMPLATE_DIR = APP_ROOT / 'templates'
STATIC_DIR = APP_ROOT / 'static'

APP_TITLE = os.getenv('HALO_SITE_TITLE', 'Halo Stats')
TIMEZONE = os.getenv('HALO_TZ', 'UTC')
SESSION_GAP_MINUTES = int(os.getenv('HALO_SESSION_GAP_MINUTES', '120'))
# A player whose most recent match landed within this many minutes is treated as
# "live now" on the dashboard (likely still in a session).
LIVE_WINDOW_MINUTES = int(os.getenv('HALO_LIVE_WINDOW_MINUTES', '45'))
SESSION_LIMIT_DEFAULT = int(os.getenv('HALO_SESSION_LIMIT', '50'))
LIFETIME_LIMIT_DEFAULT = int(os.getenv('HALO_LIFETIME_LIMIT_DEFAULT', '200'))
LIFETIME_LIMIT_MAX = int(os.getenv('HALO_LIFETIME_LIMIT_MAX', '2000'))
STATUS_PATH = data_path(os.getenv('HALO_STATUS_NAME', 'update_status.json'))
SETTINGS_PATH = data_path('settings.json')
INSIGHTS_CACHE_PATH = data_path(os.getenv('HALO_INSIGHTS_CACHE_NAME', 'insights_cache.json'))
STATIC_VERSION_OVERRIDE = os.getenv('HALO_STATIC_VERSION')
CACHE_TTL = int(os.getenv('HALO_CACHE_TTL', '120'))
DB_COUNT_TTL = float(os.getenv('HALO_DB_COUNT_TTL', '3'))
DATA_CACHE_CHECK_INTERVAL = float(os.getenv('HALO_DATA_CACHE_CHECK_INTERVAL', '3'))
SYNC_RELOAD_ON_CHANGE = os.getenv('HALO_SYNC_RELOAD_ON_CHANGE', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
SITE_VERSION_POLL_SECONDS = float(os.getenv('HALO_SITE_VERSION_POLL_SECONDS', '2.5'))
LIVE_STREAM_POLL_SECONDS = float(os.getenv('HALO_LIVE_STREAM_POLL_SECONDS', '3'))
TWITCH_LIVE_TTL_SECONDS = float(os.getenv('HALO_TWITCH_LIVE_TTL_SECONDS', '5'))
INSIGHTS_CACHE_TTL = int(os.getenv('HALO_INSIGHTS_CACHE_TTL', '300'))
INSIGHTS_CACHE_DISK_TTL = int(os.getenv('HALO_INSIGHTS_CACHE_DISK_TTL', '21600'))
INSIGHTS_CACHE_VERSION = 2
LINEUP_MATCH_LIMIT = int(os.getenv('HALO_LINEUP_MATCH_LIMIT', '0'))
MAP_VETO_MIN_GAMES = int(os.getenv('HALO_MAP_VETO_MIN_GAMES', '50'))
# Maps with fewer than this many squad-wide ranked games are treated as retired /
# out-of-rotation and hidden from every map-specific view (overall performance
# totals are unaffected — only map-keyed breakdowns/veto/best-maps drop them).
MAP_MIN_GAMES = int(os.getenv('HALO_MAP_MIN_GAMES', '100'))
NOTABLE_GAMES_LIMIT = int(os.getenv('HALO_NOTABLE_GAMES_LIMIT', '100'))
PLAYER_HOVER_CACHE_TTL = int(os.getenv('HALO_PLAYER_HOVER_TTL', '300'))
DB_NAME = os.getenv('HALO_DB_NAME', 'halostatsapi')
DB_USER = os.getenv('HALO_DB_USER', 'postgres')
DB_PASSWORD = os.getenv('HALO_DB_PASSWORD')
DB_HOST = os.getenv('HALO_DB_HOST', 'halostatsapi')
DB_PORT = os.getenv('HALO_DB_PORT', '5432')

NUMERIC_COLUMNS = ['kills', 'deaths', 'assists', 'kda', 'accuracy', 'score', 'dmg/ka', 'dmg/death', 'dmg/min', 'dmg_difference']
MATCH_COLUMNS = [
    # Core match info
    'match_id', 'date', 'player_gamertag', 'playlist', 'game_type', 'map', 'outcome',
    # Player core stats
    'kills', 'deaths', 'assists', 'kda', 'accuracy', 'score', 'personal_score',
    'duration', 'medal_count', 'average_life_duration',
    # Weapon/Damage stats
    'damage_dealt', 'damage_taken', 'shots_fired', 'shots_hit',
    'headshot_kills', 'melee_kills', 'grenade_kills', 'power_weapon_kills',
    'vehicle_destroys', 'hijacks',
    # Objective stats
    'objectives_completed', 'callout_assists', 'betrayals', 'suicides',
    'rounds_won', 'rounds_lost', 'rounds_tied',
    # Calculated stats
    'dmg/ka', 'dmg/death', 'dmg/min', 'dmg_difference',
    # CSR tracking
    'pre_match_csr', 'post_match_csr',
    # Game type specific
    'capture_the_flag_stats_flag_captures', 'capture_the_flag_stats_flag_returns',
    'oddball_stats_time_as_skull_carrier', 'zones_stats_stronghold_captures',
    'extraction_stats_successful_extractions',
    # Team stats
    'team_id', 'team_rank', 'team_damage_dealt', 'team_score', 'team_personal_score',
    'enemy_team_damage_dealt', 'enemy_team_score'
]
MAJOR_STAT_COLUMNS = [
    ('kills', 'Kills'), ('deaths', 'Deaths'), ('assists', 'Assists'), 
    ('kda', 'KDA'), ('accuracy', 'Accuracy'),
    ('damage_dealt', 'Damage Dealt'), ('damage_taken', 'Damage Taken'),
    ('dmg/ka', 'DMG/KA'), ('dmg/death', 'DMG/Death'), ('dmg/min', 'DMG/Min'),
    ('dmg_difference', 'Damage Diff'),
    ('shots_fired', 'Shots Fired'), ('shots_hit', 'Shots Hit'),
    ('medal_count', 'Medals'), ('personal_score', 'Personal Score'),
    ('callout_assists', 'Callouts'), 
    ('headshot_kills', 'Headshots'), ('melee_kills', 'Melee'), ('grenade_kills', 'Grenades'),
    ('power_weapon_kills', 'Power Weapons'),
    ('average_life_duration', 'Avg Life'), ('objectives_completed', 'Objectives'),
    ('betrayals', 'Betrayals'), ('suicides', 'Suicides'),
    ('pre_match_csr', 'Pre-CSR'), ('post_match_csr', 'Post-CSR'),
    ('vehicle_destroys', 'Vehicle Kills'), ('hijacks', 'Hijacks'),
    ('rounds_won', 'Rounds Won'), ('rounds_lost', 'Rounds Lost')
]
INDEX_DEFINITIONS = [('idx_halo_match_stats_playlist', 'playlist'), ('idx_halo_match_stats_outcome', 'outcome'), ('idx_halo_match_stats_date', 'date'), ('idx_halo_match_stats_player', 'player_gamertag'), ('idx_halo_match_stats_match', 'match_id')]
# Composite / extra indexes that speed the row-count WHERE filter, the
# match_datetime ORDER BY used when loading, and the common player+playlist
# slice. Each is created only when all of its columns exist.
COMPOSITE_INDEX_DEFINITIONS = [
    ('idx_halo_match_stats_match_datetime', ('match_datetime',)),
    ('idx_halo_match_stats_player_playlist', ('player_gamertag', 'playlist')),
]
OBJECTIVE_PREFIXES = ('capture_the_flag_stats_', 'oddball_stats_', 'zones_stats_', 'extraction_stats_')
EXTRA_MATCH_COLUMNS = ['objectives_completed', 'betrayals', 'suicides']
# Named per-medal columns the dashboards reference *by name* (not via the
# generic ``medal_*`` iteration). These must stay in the lean cache so the
# pages that use the lean cache (e.g. /weapons, /highlights) keep producing
# identical numbers. Every *other* ``medal_*`` / ``medal_id_*`` column is only
# ever consumed by generic iteration in the four medal routes, so those columns
# live exclusively in the medal-inclusive cache to keep the lean cache small.
KEEP_MEDAL_COLUMNS = {'medal_count', 'medal_snipe', 'medal_no_scope', 'medal_perfect'}

app = Flask(__name__, template_folder=str(TEMPLATE_DIR), static_folder=str(STATIC_DIR))
# Static assets are cache-busted by a ?v=<mtime> query param, so it's safe to
# cache them hard in the browser — cuts repeat-visit requests to near zero.
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 604800  # 7 days
app.secret_key = os.getenv('HALO_SECRET_KEY') or os.getenv('SECRET_KEY') or secrets.token_hex(32)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Strict'
app.config['SESSION_COOKIE_SECURE'] = os.getenv('HALO_SESSION_COOKIE_SECURE', 'false').strip().lower() in ('1', 'true', 'yes', 'on')
logger = logging.getLogger(__name__)

ADMIN_USER = os.getenv('HALO_ADMIN_USER', 'admin')
ADMIN_PASSWORD = os.getenv('HALO_ADMIN_PASSWORD') or os.getenv('HALO_ADMIN_TOKEN') or ''
CSRF_SESSION_KEY = '_csrf_token'
ADMIN_ENDPOINTS = {
    'settings', 'suggestions', 'rosters', 'snapshots', 'goals',
}


def auth_enabled() -> bool:
    return bool(ADMIN_PASSWORD)


def is_admin() -> bool:
    return (not auth_enabled()) or bool(session.get('halo_admin'))


def csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def _wants_json() -> bool:
    return request.path.startswith('/api/') or 'application/json' in (request.headers.get('Accept') or '')


@app.after_request
def _gzip_response(resp):
    """Gzip text responses (HTML pages are ~300KB uncompressed) — the single
    biggest transfer win, especially on mobile. Cache-busted static already sets
    long cache headers so it only pays this once."""
    try:
        if resp.direct_passthrough or resp.status_code >= 300 or resp.headers.get('Content-Encoding'):
            return resp
        if 'gzip' not in (request.headers.get('Accept-Encoding') or '').lower():
            return resp
        ct = (resp.content_type or '')
        if not any(t in ct for t in ('text/', 'json', 'javascript', 'css', 'html', 'xml', 'svg', 'manifest')):
            return resp
        data = resp.get_data()
        if len(data) < 1024:
            return resp
        import gzip as _gz
        comp = _gz.compress(data, 5)
        resp.set_data(comp)
        resp.headers['Content-Encoding'] = 'gzip'
        resp.headers['Content-Length'] = str(len(comp))
        resp.headers.add('Vary', 'Accept-Encoding')
    except Exception:
        return resp
    return resp


# ── Response-level HTML cache ───────────────────────────────────────────────
# Full rendered pages for anonymous GETs — covers template render + any inline
# per-request builds the payload caches can't reach. Count-aware + short TTL;
# the cache warmer keeps entries refreshed so repeat views are ~5ms.
RESP_CACHE: dict = {}
RESP_CACHE_TTL = int(os.getenv('HALO_RESP_CACHE_TTL', '45'))
RESP_CACHE_TTLS = {'/live': 15}          # live page reacts to streams, not just games
RESP_CACHE_MAX = int(os.getenv('HALO_RESP_CACHE_MAX', '120'))
RESP_CACHE_SKIP_PATHS = {'/overlay'}     # meta-refreshing OBS source — keep it live


@app.before_request
def _resp_cache_serve():
    if request.method != 'GET' or request.path in RESP_CACHE_SKIP_PATHS:
        return None
    if auth_enabled() and is_admin():
        return None
    # The streaming→/live redirect must still win over a cached dashboard.
    if request.path == '/' and not request.args:
        try:
            if (os.getenv('HALO_LIVE_REDIRECT', 'true').strip().lower() not in ('0', 'false', 'no', 'off')
                    and _live_streaming_gamertags()):
                return redirect(url_for('live'))
        except Exception:
            pass
    e = RESP_CACHE.get(request.full_path)
    if not e:
        return None
    ttl = RESP_CACHE_TTLS.get(request.path, RESP_CACHE_TTL)
    if e['count'] != count_cache.get() or time.time() - e['ts'] > ttl:
        return None  # let the request rebuild it (payload SWR keeps that fast)
    resp = Response(e['body'], mimetype='text/html')
    resp.headers['X-Page-Cache'] = 'hit'
    return resp


@app.after_request
def _resp_cache_store(resp):
    """Registered after _gzip_response → runs BEFORE it (reverse order), so the
    stored body is the un-gzipped HTML."""
    try:
        if (request.method == 'GET' and resp.status_code == 200
                and request.path not in RESP_CACHE_SKIP_PATHS
                and resp.headers.get('X-Page-Cache') != 'hit'
                and not resp.direct_passthrough
                and 'text/html' in (resp.content_type or '')
                and not (auth_enabled() and is_admin())):
            body = resp.get_data()
            # Never cache a page carrying a session-bound CSRF token — it would
            # break form posts for every other visitor it got served to.
            if b'_csrf' not in body and len(body) < 3_000_000:
                cnt = count_cache.get()
                if cnt > 0:  # same DB-blip poisoning guard as the payload caches
                    RESP_CACHE[request.full_path] = {'ts': time.time(), 'count': cnt, 'body': body}
                    while len(RESP_CACHE) > RESP_CACHE_MAX:
                        RESP_CACHE.pop(min(RESP_CACHE, key=lambda k: RESP_CACHE[k]['ts']), None)
    except Exception:
        pass
    return resp


@app.before_request
def require_admin_and_csrf():
    endpoint = request.endpoint or ''
    if endpoint in ('static', 'login', 'health', 'manifest', 'service_worker', 'app_icon'):
        return None
    # Push (un)subscription is open to any visitor — subscribing only registers a
    # browser push endpoint, so it's exempt from the admin+CSRF gate on POSTs.
    if endpoint in ('push_key', 'push_subscribe', 'push_unsubscribe', 'push_test'):
        return None
    # Twitch chat sign-in relays are open to any visitor — they only proxy
    # Twitch's public device-code flow and store nothing server-side.
    if endpoint in ('twitch_chat_config', 'twitch_device_start', 'twitch_device_poll',
                    'twitch_refresh', 'twitch_validate'):
        return None
    if auth_enabled() and endpoint in ADMIN_ENDPOINTS and not is_admin():
        if _wants_json():
            return jsonify({'error': 'unauthorized'}), 401
        return redirect(url_for('login', next=request.full_path if request.query_string else request.path))
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        if auth_enabled() and not is_admin():
            if _wants_json():
                return jsonify({'error': 'unauthorized'}), 401
            return redirect(url_for('login', next=request.path))
        sent = request.headers.get('X-CSRF-Token') or request.form.get('_csrf')
        if request.is_json and not sent:
            body = request.get_json(silent=True) or {}
            sent = body.get('_csrf') if isinstance(body, dict) else None
        expected = session.get(CSRF_SESSION_KEY)
        if not expected or not sent or not hmac.compare_digest(str(expected), str(sent)):
            logger.warning('CSRF reject path=%s have_expected=%s have_sent=%s',
                           request.path, bool(expected), bool(sent))
            if _wants_json():
                return jsonify({'error': 'csrf'}), 403
            return Response('CSRF validation failed', status=403)
    return None


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    next_url = request.args.get('next') or request.form.get('next') or url_for('index')
    if not str(next_url).startswith('/'):
        next_url = url_for('index')
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if auth_enabled() and username == ADMIN_USER and hmac.compare_digest(password, ADMIN_PASSWORD):
            session['halo_admin'] = True
            csrf_token()
            return redirect(next_url)
        error = 'Invalid login.'
    return render_template('login.html', app_title=APP_TITLE, error=error, next_url=next_url,
                           db_row_count=count_cache.get() if 'count_cache' in globals() else 0)


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('login'))


# ── First-run setup (/setup) ────────────────────────────────────────────────
# Players are configured in the app (saved to players.json in the data dir)
# instead of env files. See src/players.py for the load priority.

_ROSTER_CONFIGURED_CACHE = {'ts': 0.0, 'val': False}


def _roster_configured() -> bool:
    """Cheap cached check for 'is at least one player configured?'."""
    now = time.time()
    if now - _ROSTER_CONFIGURED_CACHE['ts'] < 5:
        return _ROSTER_CONFIGURED_CACHE['val']
    try:
        import players as players_mod
        val = bool(players_mod.load_players())
    except Exception:
        val = True  # fail open — never trap the whole site behind /setup on an error
    _ROSTER_CONFIGURED_CACHE['ts'] = now
    _ROSTER_CONFIGURED_CACHE['val'] = val
    return val


@app.before_request
def _first_run_gate():
    """Until a roster exists, steer page views to /setup instead of rendering
    empty dashboards."""
    if request.method != 'GET':
        return None
    endpoint = request.endpoint or ''
    if (endpoint in ('static', 'login', 'logout', 'setup', 'health', 'manifest',
                     'service_worker', 'app_icon', 'push_key')
            or request.path.startswith('/api/')
            or request.path.startswith('/icon-')):
        return None
    if _roster_configured():
        return None
    return redirect(url_for('setup'))


def _resolve_xuids(gamertags: list) -> dict:
    """gamertag → xuid via the Halo profile API (best-effort; {} on failure).
    Imported lazily — stats.py pulls in the spnkr client, which the webapp
    doesn't otherwise need."""
    if not gamertags:
        return {}
    try:
        import asyncio
        import stats as stats_mod
        return asyncio.run(stats_mod.resolve_xuids_for_gamertags(gamertags)) or {}
    except Exception as exc:
        logger.warning('setup_xuid_resolve_failed error=%s', exc)
        return {}


@app.route('/setup', methods=['GET', 'POST'])
def setup():
    """First-run setup: (1) Azure API credentials → api_config.json,
    (2) in-app Xbox Live OAuth → tokens.json, (3) tracked players →
    players.json. GET is public (it's the landing page before any players
    exist); every POST goes through the global admin+CSRF gate like any other
    mutating route (see require_admin_and_csrf). The client secret is never
    rendered back into the page — only a "configured" state."""
    import api_config
    import players as players_mod
    current = players_mod.load_players()
    error = None
    message = None
    rows = []  # entries needing/showing manual XUIDs after a partial resolve
    form_kind = request.form.get('form') or 'players'

    if request.method == 'POST' and form_kind == 'credentials':
        client_id = (request.form.get('client_id') or '').strip()
        client_secret = (request.form.get('client_secret') or '').strip()
        stored = api_config.stored_credentials()
        if not client_secret and stored and stored['client_id'] == client_id:
            client_secret = stored['client_secret']  # unchanged id → keep saved secret
        if not client_id:
            error = 'Enter the Application (client) ID from your Azure app registration.'
        elif not client_secret:
            error = 'Enter the client secret VALUE from your Azure app registration.'
        else:
            try:
                api_config.save_credentials(client_id, client_secret)
                message = 'API credentials saved. Next: authorize with Xbox Live (step 2).'
            except (OSError, ValueError) as exc:
                logger.warning('setup_credentials_save_failed error=%s', exc)
                error = f'Could not save credentials: {exc}'

    if request.method == 'POST' and form_kind == 'oauth':
        try:
            tokens = api_config.mint_tokens_from_code(request.form.get('auth_code') or '')
            message = 'Xbox authorization complete — tokens.json created'
            if not tokens.get('clearance_token'):
                message += ' (clearance token missing; some API calls may fail)'
            message += '. Next: add your players (step 3).'
        except Exception as exc:
            logger.warning('setup_oauth_exchange_failed error=%s', exc)
            error = f'Authorization failed: {exc}'

    if request.method == 'POST' and form_kind == 'players':
        entries = []
        # Manual-fix table rows (gamertag[i] + xuid[i] pairs).
        for gt, xu in zip(request.form.getlist('gamertag'), request.form.getlist('xuid')):
            gt = gt.strip()
            xu = re.sub(r'\D', '', xu or '')  # XUIDs are numeric
            if gt:
                entries.append({'gamertag': gt, 'xuid': xu})
        # Free-form textarea: one player per line, "Gamertag" or "Gamertag, XUID".
        for line in (request.form.get('roster') or '').splitlines():
            line = line.strip()
            if not line:
                continue
            if ',' in line:
                gt, xu = line.split(',', 1)
                entries.append({'gamertag': gt.strip(), 'xuid': re.sub(r'\D', '', xu)})
            else:
                entries.append({'gamertag': line, 'xuid': ''})
        # De-dupe by gamertag; an entry that carries an XUID wins.
        merged = {}
        for e in entries:
            key = e['gamertag'].lower()
            if key not in merged or e['xuid']:
                merged[key] = e
        entries = list(merged.values())

        if not entries:
            error = 'Enter at least one gamertag.'
        else:
            need = [e for e in entries if not e['xuid']]
            if need:
                found = _resolve_xuids([e['gamertag'] for e in need])
                for e in need:
                    if found.get(e['gamertag']):
                        e['xuid'] = found[e['gamertag']]
            unresolved = [e for e in entries if not e['xuid']]
            if not unresolved:
                players_mod.save_players(entries)
                _ROSTER_CONFIGURED_CACHE['ts'] = 0.0
                _PLAYER_CLASS_CACHE['ts'] = 0.0
                logger.info('setup_roster_saved count=%s', len(entries))
                return redirect(url_for('index', stay=1))
            rows = entries
            error = ("Couldn't auto-resolve an XUID for every gamertag — fill the "
                     'missing ones in below. (Auto-resolve needs a valid tokens.json; '
                     'you can also look XUIDs up on any gamertag→XUID site, or grab '
                     'them from a Halo tracker profile URL.)')

    # Step-status context (recomputed after any POST so the page reflects the
    # action that just happened). The secret itself is never passed to the
    # template — only the client id + a configured flag.
    try:
        creds_client_id = api_config.get_credentials()[0]
        creds_configured = api_config.credentials_configured()
        creds_source = api_config.credentials_source()
        authorize_url = api_config.build_authorize_url() if creds_configured else None
        token_state = api_config.tokens_status()
        redirect_uri = api_config.get_redirect_uri()
    except Exception as exc:  # never let a status probe take down /setup
        logger.warning('setup_status_failed error=%s', exc)
        creds_client_id, creds_configured, creds_source = None, False, None
        authorize_url, token_state, redirect_uri = None, {}, 'http://localhost'

    return render_template('setup.html', app_title=APP_TITLE, current=current,
                           rows=rows, error=error, message=message,
                           creds_client_id=creds_client_id,
                           creds_configured=creds_configured,
                           creds_source=creds_source,
                           authorize_url=authorize_url,
                           token_state=token_state,
                           redirect_uri=redirect_uri,
                           db_row_count=count_cache.get() if 'count_cache' in globals() else 0)


try:
    APP_TIMEZONE = pytz_timezone(TIMEZONE)
except UnknownTimeZoneError:
    logger.warning('Unknown timezone %s, falling back to UTC', TIMEZONE)
    APP_TIMEZONE = UTC

STATIC_VERSION_PATHS = [
    STATIC_DIR / 'styles.css',
    STATIC_DIR / 'app.js'
]

# ── Dynamic per-player palette ───────────────────────────────────────────────
# 20 distinct colors paired with generic CSS classes .player-c0 … .player-c19
# (see static/styles.css — the .player-cN rule families are generated from this
# list; keep the two in sync). Configured roster players get stable
# sorted-index colors (always distinct up to 20 players); anyone else gets a
# deterministic hash-based color so the same name is always tinted the same
# way. Adjacent indices are deliberately far apart on the hue wheel so an
# alphabetically-sorted roster never puts two near-identical colors side by
# side.
PLAYER_PALETTE = ['#3dbfb8', '#e0a800', '#4f7cf0', '#e0566b',
                  '#4ade80', '#a78bfa', '#ff7a3d', '#f472b6',
                  '#38bdf8', '#facc15', '#818cf8', '#a3e635',
                  '#f87171', '#22d3ee', '#fb923c', '#34d399',
                  '#c084fc', '#d4a373', '#94a3b8', '#e879f9']

_PLAYER_CLASS_CACHE = {'ts': 0.0, 'map': {}}


def _player_class_map() -> dict:
    """lowercase gamertag → 'player-cN' for the configured roster (30s TTL)."""
    now = time.time()
    if now - _PLAYER_CLASS_CACHE['ts'] < 30:
        return _PLAYER_CLASS_CACHE['map']
    m = {}
    try:
        import players as players_mod
        roster = sorted({p['gamertag'] for p in players_mod.load_players()},
                        key=str.lower)
        m = {gt.strip().lower(): f'player-c{i % len(PLAYER_PALETTE)}'
             for i, gt in enumerate(roster)}
    except Exception:
        logger.debug('player_class_map_failed', exc_info=True)
    _PLAYER_CLASS_CACHE['ts'] = now
    _PLAYER_CLASS_CACHE['map'] = m
    return m


def get_player_class(player_name: str) -> str:
    """Return the generic palette CSS class for a player (player-c0…c19)."""
    if not player_name:
        return ''
    name_lower = str(player_name).strip().lower()
    mapped = _player_class_map().get(name_lower)
    if mapped:
        return mapped
    import zlib
    return f'player-c{zlib.crc32(name_lower.encode("utf-8")) % len(PLAYER_PALETTE)}'


def get_static_version() -> str:
    if STATIC_VERSION_OVERRIDE:
        return STATIC_VERSION_OVERRIDE
    try:
        mtimes = []
        for path in STATIC_VERSION_PATHS:
            if path.exists():
                mtimes.append(path.stat().st_mtime)
        if mtimes:
            return str(int(max(mtimes)))
    except OSError:
        return '1'
    return '1'


@app.template_filter('player_class')
def player_class_filter(player_name):
    return get_player_class(player_name)


_CLASS_HEX = {f'player-c{i}': c for i, c in enumerate(PLAYER_PALETTE)}


@app.template_filter('player_hex')
def player_hex_filter(player_name):
    """Hex colour for a player (for places that can't use a CSS class, e.g. an
    <option> text color)."""
    return _CLASS_HEX.get(get_player_class(player_name), '#e8edf6')


@app.template_filter('player_href')
def player_href_filter(player_name):
    return url_for('player_profile', player_name=str(player_name or ''))


@app.template_filter('player_card_href')
def player_card_href_filter(player_name):
    return url_for('stat_card', player_name=str(player_name or ''))


@app.template_filter('mmss')
def mmss_filter(value):
    """Seconds → m:ss (e.g. 1238 → '20:38', 9 → '0:09'). Blank for 0/None."""
    try:
        s = int(round(float(value)))
    except (TypeError, ValueError):
        return value
    if s <= 0:
        return '–'
    return f'{s // 60}:{s % 60:02d}'


# Optional MultiTwitch (synced stream-rewatch) integration. Set MULTITWITCH_URL
# to the public base URL of a MultiTwitch instance to enable "watch the rewatch"
# links; leave unset to hide the feature entirely.
MULTITWITCH_URL = os.environ.get('MULTITWITCH_URL', '').rstrip('/')

# match_id → unix-seconds map, so rewatch links can point DIRECTLY at MultiTwitch
# (<MULTITWITCH_URL>/api/watch-at?ts=…) instead of hopping through a local
# /watch/<id> redirect.
_MATCH_TS_CACHE = {'ts': 0.0, 'map': {}}


def _match_ts_map() -> dict:
    now = time.time()
    if _MATCH_TS_CACHE['map'] and (now - _MATCH_TS_CACHE['ts'] < 120):
        return _MATCH_TS_CACHE['map']
    m = _MATCH_TS_CACHE['map']
    try:
        df = cache.get()
        if not df.empty and 'match_id' in df.columns and 'date' in df.columns:
            d = pd.to_datetime(df['date'], utc=True, errors='coerce')
            tmp = pd.DataFrame({'mid': df['match_id'].astype(str), 'ts': d}).dropna(subset=['ts'])
            grouped = tmp.groupby('mid')['ts'].max()
            m = {k: int(v.timestamp()) for k, v in grouped.items()}
    except Exception:
        pass  # keep the previous (possibly stale) map on error
    _MATCH_TS_CACHE['ts'] = now
    _MATCH_TS_CACHE['map'] = m
    return m


def watch_url_for(match_id) -> str:
    """Direct MultiTwitch synced-rewatch URL for a match (empty if unknown or
    the MultiTwitch integration isn't configured)."""
    if not match_id or not MULTITWITCH_URL:
        return ''
    ts = _match_ts_map().get(str(match_id))
    return f"{MULTITWITCH_URL}/api/watch-at?ts={ts}" if ts else ''


# Register as a true Jinja global (NOT just a context-processor var) so it's
# reachable inside imported macros like _combat_macros.html — context-processor
# variables are not visible to imported macros.
app.jinja_env.globals['watch_url'] = watch_url_for


def player_color_map() -> dict:
    """lowercase gamertag → hex for the configured roster. Injected into
    templates so chart JS colors players consistently with the CSS classes."""
    return {name: _CLASS_HEX.get(css, PLAYER_PALETTE[0])
            for name, css in _player_class_map().items()}


@app.context_processor
def utility_processor():
    return dict(get_player_class=get_player_class, static_version=get_static_version(),
                csrf_token=csrf_token, is_admin=is_admin, auth_enabled=auth_enabled,
                multitwitch_base=MULTITWITCH_URL,
                player_colors=player_color_map(),
                player_palette=PLAYER_PALETTE,
                realtime_poll_seconds=SITE_VERSION_POLL_SECONDS)


@app.context_processor
def nav_players_processor():
    """Tracked-player list for the global nav drawer. Many routes don't pass
    `players`, which left the nav's Players section showing "No players yet".
    Inject it globally so every page's nav is populated; routes that pass their
    own `players` (e.g. for a filter dropdown) still override this."""
    try:
        df = cache.get()
        players = unique_sorted(df['player_gamertag']) if (
            not df.empty and 'player_gamertag' in df.columns) else []
    except Exception:
        players = []
    return dict(players=players)


@app.context_processor
def hover_data_processor():
    # build_player_hover_data has its own 300s TTL cache internally,
    # so the per-request cost is just a timestamp check + cache.get().
    try:
        data = build_player_hover_data(cache.get())
        # Ensure the value is a plain dict (guards against lazy wrappers)
        if not isinstance(data, dict):
            data = {}
    except (TypeError, ValueError, KeyError):
        data = {}
    return dict(player_hover_data=data)


def get_engine():
    db_url = f'postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}'
    # pool_pre_ping: re-validate (and re-resolve DNS for) a pooled connection
    # before handing it out, so a transient Docker-DNS / DB blip recycles the
    # connection instead of bubbling up an error.
    # pool_recycle: drop connections older than 30 min so stale sockets after a
    # network hiccup don't linger.
    # connect_args timeout: a connect attempt fails fast (10s) rather than
    # hanging a waitress thread.
    return create_engine(
        db_url,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={'connect_timeout': 10},
    )


def to_local_timestamp(value):
    ts = pd.to_datetime(value, errors='coerce', utc=True)
    if pd.isna(ts):
        return ts
    try:
        return ts.tz_convert(APP_TIMEZONE)
    except Exception:
        return ts


def quote_identifier(name: str) -> str:
    safe_name = str(name).replace('"', '""')
    return f'"{safe_name}"'


def select_match_columns(available: set[str]) -> list[str]:
    medal_cols = [col for col in available if str(col).startswith('medal_') or str(col).startswith('medal_id_')]
    objective_cols = [col for col in available if any(str(col).startswith(prefix) for prefix in OBJECTIVE_PREFIXES)]
    extra_cols = [col for col in EXTRA_MATCH_COLUMNS if col in available]
    ordered = []
    seen = set()
    for col in MATCH_COLUMNS + sorted(objective_cols) + sorted(medal_cols) + extra_cols:
        if col in available and col not in seen:
            ordered.append(col)
            seen.add(col)
    return ordered or sorted(available)


def ensure_indexes(engine) -> None:
    try:
        inspector = inspect(engine)
        if not inspector.has_table('halo_match_stats'):
            return
        columns = {c.get('name') for c in inspector.get_columns('halo_match_stats')}
        columns.discard(None)
        if not columns:
            return
        with engine.begin() as conn:
            for index_name, column_name in INDEX_DEFINITIONS:
                if column_name not in columns:
                    continue
                col_sql = quote_identifier(column_name)
                conn.execute(text(f'CREATE INDEX IF NOT EXISTS {index_name} ON halo_match_stats ({col_sql})'))
            for index_name, column_names in COMPOSITE_INDEX_DEFINITIONS:
                if any(col not in columns for col in column_names):
                    continue
                cols_sql = ', '.join(quote_identifier(col) for col in column_names)
                conn.execute(text(f'CREATE INDEX IF NOT EXISTS {index_name} ON halo_match_stats ({cols_sql})'))
    except SQLAlchemyError as exc:
        logger.warning('Failed to ensure indexes: %s', exc)
        return None


def load_dataframe(engine, include_medals: bool = False) -> pd.DataFrame:
    try:
        inspector = inspect(engine)
        if not inspector.has_table('halo_match_stats'):
            return pd.DataFrame()

        columns = {c.get('name') for c in inspector.get_columns('halo_match_stats')}
        columns.discard(None)

        # Exclude raw_json and bulk team/enemy columns to save memory.
        # Only load columns the webapp actually needs for dashboards.
        exclude_prefixes = ('raw_json', 'scraped_at',
                            'all_time_max_csr_', 'current_csr_', 'season_max_csr_',
                            'pvp_stats_')
        exclude_exact = {'raw_json', 'scraped_at'}
        select_cols = []
        # Iterate columns in a deterministic (sorted) order so the loaded column
        # order — and therefore the medal-row display order on /medals — is
        # stable across restarts. (``columns`` is a set, whose iteration order
        # otherwise varies per process.)
        for c in sorted(columns):
            if c in exclude_exact:
                continue
            if any(c.startswith(p) for p in exclude_prefixes):
                continue
            # The ~185 per-medal columns (medal_* / medal_id_*) are only used by
            # the four "medal" routes via generic ``medal_*`` iteration. Skip
            # them in the lean cache to cut memory ~5x; the medal-inclusive
            # cache (include_medals=True) loads them. A small named keep-set
            # stays in the lean cache because some pages read those by name.
            if not include_medals and c not in KEEP_MEDAL_COLUMNS and (
                    c.startswith('medal_') or c.startswith('medal_id_')):
                continue
            select_cols.append(c)
        select_sql = ', '.join(quote_identifier(col) for col in select_cols) if select_cols else '*'
        
        where = []
        if 'playlist' in columns:
            # Stats are Ranked Arena ONLY — exclude Ranked Doubles/FFA/Slayer/
            # Snipers/Survivors/Tactical and all social/BTB/customs.
            where.append("playlist ILIKE 'Ranked Arena'")
        if 'outcome' in columns:
            where.append("LOWER(outcome) <> 'dnf'")
        
        tie_conditions = []
        if 'kills' in columns:
            tie_conditions.append('COALESCE(kills, 0) <= 1')
        if 'duration' in columns:
            tie_conditions.append('COALESCE(duration, 0) < 120')
        if tie_conditions:
            tie_clause = ' AND '.join(tie_conditions)
            where.append(f"NOT (LOWER(outcome) = 'tie' AND ({tie_clause}))")
        
        query = f'SELECT {select_sql} FROM halo_match_stats'
        if where:
            query = f"{query} WHERE {' AND '.join(where)}"
        # Memory cap: pull at most HALO_DF_MAX_ROWS (default 200k) most-recent
        # matches.  Without this, a growing match DB will eventually OOM the
        # webapp on cache rebuild.
        try:
            max_rows = max(1000, int(os.getenv('HALO_DF_MAX_ROWS', '200000')))
        except (TypeError, ValueError):
            max_rows = 200000
        if 'match_datetime' in columns:
            query = f"{query} ORDER BY match_datetime DESC NULLS LAST LIMIT {max_rows}"
        elif 'match_id' in columns:
            query = f"{query} ORDER BY match_id DESC LIMIT {max_rows}"
        else:
            query = f"{query} LIMIT {max_rows}"

        df = pd.read_sql_query(text(query), engine)
        return df
    except (SQLAlchemyError, ValueError) as exc:
        logger.warning('Failed to load dataframe: %s', exc)
        return pd.DataFrame()


def load_db_row_count(engine) -> int:
    try:
        if not inspect(engine).has_table('halo_match_stats'):
            return 0
        
        columns = {c.get('name') for c in inspect(engine).get_columns('halo_match_stats')}
        where = ["playlist ILIKE 'Ranked Arena'"] if 'playlist' in columns else []
        
        if 'outcome' in columns:
            where.append("LOWER(outcome) <> 'dnf'")
        
        tie_conditions = []
        if 'kills' in columns:
            tie_conditions.append('COALESCE(kills, 0) <= 1')
        if 'duration' in columns:
            tie_conditions.append('COALESCE(duration, 0) < 120')
        if tie_conditions:
            tie_clause = ' AND '.join(tie_conditions)
            where.append(f"NOT (LOWER(outcome) = 'tie' AND ({tie_clause}))")
        
        where_sql = ' AND '.join(where)
        
        with engine.connect() as conn:
            if where_sql:
                query = f'SELECT COUNT(*) FROM halo_match_stats WHERE {where_sql}'
            else:
                query = 'SELECT COUNT(*) FROM halo_match_stats'
            return int(conn.execute(text(query)).scalar() or 0)
    except (SQLAlchemyError, ValueError, TypeError) as exc:
        logger.warning('Failed to load DB row count: %s', exc)
        return 0


class DataCache:
    """Smart cache that only reloads when new matches are added.

    Two flavours, distinguished by ``include_medals``:
      * lean (default) — drops the ~185 generic ``medal_*`` columns, used by the
        bulk of the dashboard routes. Much smaller in memory.
      * medal-inclusive — also loads every ``medal_*`` column, used only by the
        handful of routes that iterate medals generically.
    Both flavours load the *same rows* (identical WHERE/ORDER/LIMIT), so a
    per-player or per-match aggregate computed from either is identical; only
    the set of columns differs.
    """

    def __init__(self, engine, include_medals: bool = False) -> None:
        self.engine = engine
        self.include_medals = include_medals
        self.df = pd.DataFrame()
        self.last_count = 0
        self.last_check = 0.0
        self._lock = threading.Lock()
        self._reloading = False

    def _reload_now(self):
        df = normalize_df(load_dataframe(self.engine, self.include_medals))
        if not df.empty:
            self.df = df            # atomic swap (GIL) — readers never see a partial df
            self.last_count = load_db_row_count(self.engine)
        return df

    def get(self) -> pd.DataFrame:
        # First ever load (or recovering from an empty df after a DB blip) must be
        # synchronous — there's nothing stale to serve yet.
        if self.df.empty:
            with self._lock:
                if self.df.empty:
                    self._reload_now()
            return self.df
        # Otherwise, check the lightweight row count frequently. When a new game
        # lands, reload synchronously by default so the request that noticed the
        # change returns fresh stats instead of making the browser wait for a
        # later timer tick.
        now = time.time()
        if now - self.last_check >= DATA_CACHE_CHECK_INTERVAL and not self._reloading:
            self.last_check = now
            try:
                current_count = load_db_row_count(self.engine)
            except Exception:
                current_count = self.last_count
            if current_count != self.last_count:
                if SYNC_RELOAD_ON_CHANGE:
                    with self._lock:
                        if current_count != self.last_count:
                            self._reload_now()
                    return self.df
                self._reloading = True

                def _bg():
                    try:
                        self._reload_now()
                    except Exception as exc:
                        logger.warning('bg reload failed: %s', exc)
                    finally:
                        self._reloading = False

                threading.Thread(target=_bg, daemon=True, name='datacache-reload').start()
        return self.df

    def force_reload(self) -> pd.DataFrame:
        with self._lock:
            self.df = normalize_df(load_dataframe(self.engine, self.include_medals))
            self.last_count = load_db_row_count(self.engine)
            self.last_check = time.time()
        return self.df


class DbCountCache:
    def __init__(self, engine) -> None:
        self.engine = engine
        self.count = 0
        self.last_load = 0.0
    
    def get(self) -> int:
        now = time.time()
        if now - self.last_load >= DB_COUNT_TTL:
            self.count = load_db_row_count(self.engine)
            self.last_load = now
        return self.count

    def set(self, count: int) -> None:
        self.count = int(count or 0)
        self.last_load = time.time()


def ensure_datetime(df: pd.DataFrame, col: str = 'date') -> pd.DataFrame:
    """Parse df[col] to tz-aware datetimes exactly once (in place). Frames that
    came through normalize_df (cache.get()/medal_df()) are already parsed, so
    this is a cheap no-op for them — use it instead of re-running
    pd.to_datetime at every call site."""
    if col in getattr(df, 'columns', ()) and not pd.api.types.is_datetime64_any_dtype(df[col]):
        df[col] = pd.to_datetime(df[col], errors='coerce', utc=True)
    return df


def normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()

    if 'date' in df.columns:
        ensure_datetime(df)
        try:
            df['date_local'] = df['date'].dt.tz_convert(APP_TIMEZONE)
        except Exception:
            df['date_local'] = df['date']
    else:
        df['date_local'] = pd.NaT
    
    for col in NUMERIC_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    for col in ['player_gamertag', 'playlist', 'game_type', 'map', 'outcome']:
        if col not in df.columns:
            df[col] = ''

    # Derived per-minute rates — tracked site-wide like dmg/min so every table can
    # show them. `duration` is numeric seconds; short/zero games → NaN (blank).
    if 'duration' in df.columns:
        _mins = (pd.to_numeric(df['duration'], errors='coerce') / 60.0)
        _mins = _mins.where(_mins > 0)
        if 'kda' in df.columns:
            df['kda/min'] = pd.to_numeric(df['kda'], errors='coerce') / _mins
        try:
            df['obj/min'] = objective_score_series(df) / _mins
        except Exception:
            pass

    return df


def load_status() -> dict:
    if not STATUS_PATH.exists():
        return {}
    try:
        with open(STATUS_PATH, 'r') as file:
            status = json.load(file)
        if isinstance(status, dict) and status.get('last_update'):
            status['last_update'] = format_last_update(status.get('last_update'))
        return status
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning('Failed to load status file: %s', exc)
        return {}


def load_settings() -> dict:
    defaults = {
        'match_limit': int(os.getenv('HALO_MATCH_LIMIT', '500')),
        'update_interval': int(os.getenv('HALO_UPDATE_INTERVAL', '60')),
        'force_refresh': os.getenv('HALO_FORCE_REFRESH', 'false').strip().lower() in ['1', 'true', 'yes', 'on']
    }
    
    if not SETTINGS_PATH.exists():
        return defaults
    
    try:
        with open(SETTINGS_PATH, 'r') as file:
            settings = json.load(file)
        return {**defaults, **settings}
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning('Failed to load settings: %s', exc)
        return defaults


import hashlib  # noqa: E402  (kept local — used by the helpers just below)


# ---------------------------------------------------------------------------
# App-level DB schema helpers
# ---------------------------------------------------------------------------

def ensure_app_tables(engine) -> None:
    """Create all webapp-owned tables in one place."""
    ensure_suggestions_table(engine)
    ensure_roster_tables(engine)
    ensure_snapshot_tables(engine)
    ensure_goals_table(engine)
    ensure_match_players_table(engine)


def ensure_match_players_table(engine) -> None:
    """Mirror of the scraper's halo_match_players DDL so the webapp can read it
    (and not error) even before the first opponent capture has run."""
    with engine.begin() as conn:
        conn.execute(text('''
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
        )
        '''))
        conn.execute(text("ALTER TABLE halo_match_players ADD COLUMN IF NOT EXISTS csr DOUBLE PRECISION"))
        conn.execute(text("ALTER TABLE halo_match_players ADD COLUMN IF NOT EXISTS is_bot BOOLEAN DEFAULT FALSE"))


def ensure_goals_table(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text('''
        CREATE TABLE IF NOT EXISTS halo_goals (
            id BIGSERIAL PRIMARY KEY,
            player TEXT NOT NULL,
            metric TEXT NOT NULL,
            target DOUBLE PRECISION NOT NULL,
            window_games INTEGER NOT NULL DEFAULT 20,
            note TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        '''))


def ensure_roster_tables(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text('''
        CREATE TABLE IF NOT EXISTS halo_rosters (
            id BIGSERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            description TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        '''))
        conn.execute(text('''
        CREATE TABLE IF NOT EXISTS halo_roster_players (
            roster_id BIGINT NOT NULL REFERENCES halo_rosters(id) ON DELETE CASCADE,
            gamertag TEXT NOT NULL,
            added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (roster_id, gamertag)
        )
        '''))


def ensure_snapshot_tables(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text('''
        CREATE TABLE IF NOT EXISTS halo_snapshots (
            id BIGSERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            players JSONB,
            date_from TEXT,
            date_to TEXT,
            notes TEXT,
            share_token TEXT UNIQUE,
            payload JSONB
        )
        '''))


# ---------------------------------------------------------------------------
# Roster helpers
# ---------------------------------------------------------------------------

def fetch_rosters(engine) -> list[dict]:
    ensure_roster_tables(engine)
    with engine.begin() as conn:
        rows = conn.execute(text('''
            SELECT r.id, r.name, r.description, r.created_at,
                   COALESCE(json_agg(rp.gamertag ORDER BY rp.gamertag)
                            FILTER (WHERE rp.gamertag IS NOT NULL), '[]') AS players
            FROM halo_rosters r
            LEFT JOIN halo_roster_players rp ON rp.roster_id = r.id
            GROUP BY r.id ORDER BY r.name
        ''')).fetchall()
    return [
        {'id': r[0], 'name': r[1], 'description': r[2] or '',
         'created_at': format_date(r[3]), 'players': r[4] or []}
        for r in rows
    ]


def save_roster(engine, name: str, description: str, gamertags: list[str]) -> int:
    ensure_roster_tables(engine)
    with engine.begin() as conn:
        row = conn.execute(text('''
            INSERT INTO halo_rosters (name, description)
            VALUES (:name, :desc)
            ON CONFLICT (name) DO UPDATE SET description=EXCLUDED.description, updated_at=NOW()
            RETURNING id
        '''), {'name': name.strip(), 'desc': description.strip()}).fetchone()
        roster_id = row[0]
        conn.execute(text('DELETE FROM halo_roster_players WHERE roster_id=:id'), {'id': roster_id})
        for gt in gamertags:
            gt = gt.strip()
            if gt:
                conn.execute(text(
                    'INSERT INTO halo_roster_players (roster_id, gamertag) VALUES (:rid, :gt) ON CONFLICT DO NOTHING'
                ), {'rid': roster_id, 'gt': gt})
    return roster_id


def delete_roster(engine, roster_id: int) -> None:
    ensure_roster_tables(engine)
    with engine.begin() as conn:
        conn.execute(text('DELETE FROM halo_rosters WHERE id=:id'), {'id': roster_id})


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------

def save_snapshot(engine, name: str, players: list, date_from: str, date_to: str,
                  notes: str, payload: dict) -> dict:
    ensure_snapshot_tables(engine)
    token = secrets.token_urlsafe(16)
    with engine.begin() as conn:
        row = conn.execute(text('''
            INSERT INTO halo_snapshots (name, players, date_from, date_to, notes, share_token, payload)
            VALUES (:name, :players, :date_from, :date_to, :notes, :token, :payload)
            RETURNING id, share_token
        '''), {
            'name': name.strip(),
            'players': json.dumps(players),
            'date_from': date_from or None,
            'date_to': date_to or None,
            'notes': notes.strip() if notes else None,
            'token': token,
            'payload': json.dumps(payload),
        }).fetchone()
    return {'id': row[0], 'share_token': row[1]}


def fetch_snapshots(engine, limit: int = 50) -> list[dict]:
    ensure_snapshot_tables(engine)
    with engine.begin() as conn:
        rows = conn.execute(text('''
            SELECT id, name, created_at, players, date_from, date_to, notes, share_token
            FROM halo_snapshots ORDER BY created_at DESC LIMIT :limit
        '''), {'limit': limit}).fetchall()
    return [
        {'id': r[0], 'name': r[1], 'created_at': format_date(r[2]),
         'players': r[3] or [], 'date_from': r[4] or '', 'date_to': r[5] or '',
         'notes': r[6] or '', 'share_token': r[7]}
        for r in rows
    ]


def fetch_snapshot_by_token(engine, token: str) -> dict | None:
    ensure_snapshot_tables(engine)
    # Validate token is safe (URL-safe base64 only)
    if not token or not all(c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_' for c in token):
        return None
    with engine.begin() as conn:
        row = conn.execute(text('''
            SELECT id, name, created_at, players, date_from, date_to, notes, share_token, payload
            FROM halo_snapshots WHERE share_token=:token
        '''), {'token': token}).fetchone()
    if not row:
        return None
    return {
        'id': row[0], 'name': row[1], 'created_at': format_date(row[2]),
        'players': row[3] or [], 'date_from': row[4] or '', 'date_to': row[5] or '',
        'notes': row[6] or '', 'share_token': row[7], 'payload': row[8] or {}
    }


def delete_snapshot(engine, snapshot_id: int) -> None:
    ensure_snapshot_tables(engine)
    with engine.begin() as conn:
        conn.execute(text('DELETE FROM halo_snapshots WHERE id=:id'), {'id': snapshot_id})


# ---------------------------------------------------------------------------
# Weekly recap builder
# ---------------------------------------------------------------------------

def build_weekly_recap(df: pd.DataFrame, week_offset: int = 0) -> dict:
    """Compute a weekly recap for the N-th most recent week (0 = current/last)."""
    if df.empty or 'date' not in df.columns:
        return {}

    working = df.copy()
    ensure_datetime(working)
    working = working.dropna(subset=['date'])
    if working.empty:
        return {}

    # Determine week window (Mon–Sun in local time)
    now = pd.Timestamp.now(tz='UTC').tz_convert(APP_TIMEZONE)
    # Most recent Monday
    days_since_monday = now.dayofweek
    week_start_local = (now - pd.Timedelta(days=days_since_monday + 7 * week_offset)).normalize()
    week_end_local = week_start_local + pd.Timedelta(days=7)

    week_start_utc = week_start_local.tz_convert('UTC')
    week_end_utc = week_end_local.tz_convert('UTC')

    week_df = working[(working['date'] >= week_start_utc) & (working['date'] < week_end_utc)]

    if week_df.empty:
        return {
            'week_label': week_start_local.strftime('%b %d') + ' – ' + (week_end_local - pd.Timedelta(days=1)).strftime('%b %d, %Y'),
            'total_games': 0, 'players': [], 'best_player': None, 'worst_map': None,
            'best_map': None, 'best_duo': None, 'notable_games': [],
        }

    players_present = unique_sorted(week_df['player_gamertag']) if 'player_gamertag' in week_df.columns else []

    # Per-player summaries
    player_rows = []
    for player in players_present:
        p_df = week_df[week_df['player_gamertag'] == player]
        games = len(p_df)
        if games == 0:
            continue
        outcomes = p_df['outcome'].astype(str).str.lower() if 'outcome' in p_df.columns else pd.Series()
        wins = (outcomes == 'win').sum()
        win_pct = wins / games * 100 if games else 0
        kills = numeric_series(p_df, 'kills').sum()
        deaths = numeric_series(p_df, 'deaths').sum()
        assists = numeric_series(p_df, 'assists').sum()
        kda = safe_kda(kills / games, assists / games, deaths / games)
        dmg = numeric_series(p_df, 'damage_dealt').sum()
        dur = numeric_series(p_df, 'duration').sum()
        dpm = dmg / (dur / 60) if dur > 0 else 0
        pre_csr = pd.to_numeric(p_df['pre_match_csr'], errors='coerce').replace(0, pd.NA).dropna().min() if 'pre_match_csr' in p_df.columns else None
        post_csr = pd.to_numeric(p_df['post_match_csr'], errors='coerce').replace(0, pd.NA).dropna().max() if 'post_match_csr' in p_df.columns else None
        csr_delta = (float(post_csr) - float(pre_csr)) if pre_csr and post_csr else None
        player_rows.append({
            'player': player, 'games': games,
            'win_pct': format_float(win_pct, 1),
            'kda': format_float(kda, 2),
            'kda_min': format_float(kda_per_min(p_df), 2),
            'obj_min': _obj_dash(obj_per_min(p_df), 1),
            'dpm': format_float(dpm, 0),
            'csr_delta': format_signed(csr_delta, 0) if csr_delta is not None else '-',
            'csr_delta_raw': csr_delta or 0,
            'win_pct_raw': win_pct,
        })

    # Best player by CSR delta, then win%
    best_player = max(player_rows, key=lambda r: (r['csr_delta_raw'], r['win_pct_raw']), default=None)

    # Map win rates
    map_stats = {}
    if 'map' in week_df.columns and 'outcome' in week_df.columns:
        for map_name, map_df in week_df.groupby(week_df['map'].map(normalize_map_name)):
            if not map_name:
                continue
            g = len(map_df)
            if g < 2:
                continue
            w = (map_df['outcome'].astype(str).str.lower() == 'win').sum()
            map_stats[map_name] = {'games': g, 'win_pct': w / g * 100}
    worst_map = min(map_stats.items(), key=lambda x: x[1]['win_pct'], default=None)
    best_map = max(map_stats.items(), key=lambda x: x[1]['win_pct'], default=None)
    worst_map_row = {'name': worst_map[0], **worst_map[1], 'win_pct': format_float(worst_map[1]['win_pct'], 1)} if worst_map else None
    best_map_row = {'name': best_map[0], **best_map[1], 'win_pct': format_float(best_map[1]['win_pct'], 1)} if best_map else None

    # Best duo (pair with most wins together, same team only)
    best_duo = None
    if 'match_id' in week_df.columns and len(players_present) >= 2:
        duo_wins = {}
        for match_id, match_df in week_df.groupby('match_id'):
            outcomes_in_match = match_df['outcome'].astype(str).str.lower().tolist() if 'outcome' in match_df.columns else []
            is_win = any(o == 'win' for o in outcomes_in_match)
            team_col = 'team_id' if 'team_id' in match_df.columns else None
            if team_col:
                groups = match_df.groupby(team_col)['player_gamertag'].apply(list)
            else:
                groups = [match_df['player_gamertag'].tolist()]
            for team_players in groups:
                for combo in combinations(sorted(set(team_players)), 2):
                    key = combo
                    if key not in duo_wins:
                        duo_wins[key] = {'wins': 0, 'games': 0}
                    duo_wins[key]['games'] += 1
                    if is_win:
                        duo_wins[key]['wins'] += 1
        if duo_wins:
            top_duo = max(duo_wins.items(), key=lambda x: (x[1]['wins'], x[1]['games']))
            best_duo = {
                'players': list(top_duo[0]),
                'wins': top_duo[1]['wins'],
                'games': top_duo[1]['games'],
                'win_pct': format_float(top_duo[1]['wins'] / top_duo[1]['games'] * 100, 1) if top_duo[1]['games'] else '0.0',
            }

    # Notable games (biggest KDA or damage games)
    notable = []
    if 'kills' in week_df.columns and 'deaths' in week_df.columns and 'assists' in week_df.columns:
        week_df = week_df.copy()
        week_df['_kda'] = (
            numeric_series(week_df, 'kills') +
            numeric_series(week_df, 'assists') / 3 -
            numeric_series(week_df, 'deaths')
        )
        top = week_df.nlargest(3, '_kda')
        for _, row in top.iterrows():
            acc_val = safe_float(row.get('accuracy'))
            gg = compute_match_grade(
                kda=row['_kda'], accuracy=acc_val,
                dmg_dealt=safe_float(row.get('damage_dealt')),
                dmg_taken=safe_float(row.get('damage_taken')),
                outcome=row.get('outcome'),
            ) or {}
            notable.append({
                'player': row.get('player_gamertag', ''),
                'map': normalize_map_name(row.get('map', '')),
                'game_type': row.get('game_type', ''),
                'kda': format_float(row['_kda'], 2),
                'kills': format_int(row.get('kills', 0)),
                'deaths': format_int(row.get('deaths', 0)),
                'assists': format_int(row.get('assists', 0)),
                'outcome': str(row.get('outcome', '')).title(),
                'outcome_class': outcome_class(row.get('outcome', '')),
                'date': format_date(row.get('date')),
                'match_id': row.get('match_id', ''),
                'grade': gg.get('grade', ''),
                'grade_class': gg.get('grade_class', ''),
                'grade_tip': gg.get('grade_tip', ''),
            })

    # Week MVP (highest average Game Grade) + Game of the Week (best single grade)
    week_mvp = None
    game_of_week = None
    pp_scores = {}
    for _, row in week_df.iterrows():
        g = _match_grade_for_row(row)
        sc = g.get('grade_score')
        if sc is None:
            continue
        p = row.get('player_gamertag', '')
        pp_scores.setdefault(p, []).append(sc)
        if game_of_week is None or sc > game_of_week['score']:
            kills = safe_float(row.get('kills', 0))
            deaths = safe_float(row.get('deaths', 0))
            assists = safe_float(row.get('assists', 0))
            game_of_week = {
                'player': p, 'grade': g.get('grade'), 'grade_class': g.get('grade_class'),
                'score': sc, 'kills': format_int(kills), 'deaths': format_int(deaths),
                'assists': format_int(assists), 'kda': format_float(safe_kda(kills, assists, deaths), 2),
                'map': normalize_map_name(row.get('map')), 'match_id': row.get('match_id', ''),
                'date': format_date(row.get('date')),
            }
    for p, scores in pp_scores.items():
        avg = sum(scores) / len(scores)
        if week_mvp is None or avg > week_mvp['avg']:
            gg = grade_from_percentile(avg)
            week_mvp = {'player': p, 'avg': round(avg), 'games': len(scores),
                        'grade': gg, 'grade_class': grade_class(gg)}

    add_heatmap_classes(player_rows, {'win_pct_raw': True, 'csr_delta_raw': True})
    add_composite_grades(player_rows, {
        'win_pct_raw': True, 'kda': True, 'dpm': True, 'csr_delta_raw': True,
    }, 'Weekly grade')

    return {
        'week_label': week_start_local.strftime('%b %d') + ' – ' + (week_end_local - pd.Timedelta(days=1)).strftime('%b %d, %Y'),
        'week_start': week_start_local.strftime('%Y-%m-%d'),
        'week_end': (week_end_local - pd.Timedelta(days=1)).strftime('%Y-%m-%d'),
        'total_games': len(week_df),
        'players': player_rows,
        'week_mvp': week_mvp,
        'game_of_week': game_of_week,
        'best_player': best_player,
        'worst_map': worst_map_row,
        'best_map': best_map_row,
        'best_duo': best_duo,
        'notable_games': notable,
    }


def ensure_suggestions_table(engine) -> None:
    ddl = '''
    CREATE TABLE IF NOT EXISTS halo_suggestions (
        id BIGSERIAL PRIMARY KEY,
        submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        name TEXT,
        gamertag TEXT,
        contact TEXT,
        summary TEXT NOT NULL,
        details TEXT,
        follow_up TEXT
    )
    '''
    with engine.begin() as conn:
        conn.execute(text(ddl))


def fetch_suggestions(engine, limit: int = 50) -> list[dict]:
    ensure_suggestions_table(engine)
    query = text('''
    SELECT id, submitted_at, name, gamertag, contact, summary, details, follow_up
    FROM halo_suggestions
    ORDER BY submitted_at DESC
    LIMIT :limit
    ''')
    
    with engine.begin() as conn:
        rows = conn.execute(query, {'limit': limit}).fetchall()
    
    suggestions = []
    for row in rows:
        suggestions.append({
            'id': row[0],
            'submitted_at': format_date(row[1]),
            'name': row[2] or '',
            'gamertag': row[3] or '',
            'contact': row[4] or '',
            'summary': row[5] or '',
            'details': row[6] or '',
            'follow_up': row[7] or ''
        })
    return suggestions


def save_suggestion(engine, payload: dict) -> None:
    ensure_suggestions_table(engine)
    query = text('''
    INSERT INTO halo_suggestions (name, gamertag, contact, summary, details, follow_up)
    VALUES (:name, :gamertag, :contact, :summary, :details, :follow_up)
    ''')
    with engine.begin() as conn:
        conn.execute(query, payload)


def delete_suggestion(engine, suggestion_id: int) -> None:
    ensure_suggestions_table(engine)
    with engine.begin() as conn:
        conn.execute(text('DELETE FROM halo_suggestions WHERE id=:id'), {'id': suggestion_id})


def load_presence() -> dict:
    """Best-effort load of online/presence information."""
    for name in ['online_status.json', 'player_presence.json', 'player_status.json']:
        path = data_path(name)
        if not path.exists():
            continue
        
        try:
            with open(path, 'r') as file:
                data = json.load(file)
            
            if isinstance(data, dict):
                return data
            
            if isinstance(data, list):
                return {'players': {str(item): {'online': True} for item in data}}
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning('Failed to load presence file %s: %s', path, exc)
    
    return {}


def is_player_online(presence: dict, gamertag: str) -> bool:
    if not presence or not gamertag:
        return False
    
    players = presence.get('players') if isinstance(presence, dict) else None
    
    if isinstance(players, dict):
        for key, val in players.items():
            if str(key).strip().lower() == str(gamertag).strip().lower():
                if isinstance(val, dict):
                    return bool(val.get('online') or val.get('is_online'))
                return bool(val)
    
    if isinstance(presence, dict):
        for key, val in presence.items():
            if str(key).strip().lower() == str(gamertag).strip().lower():
                if isinstance(val, dict):
                    return bool(val.get('online') or val.get('is_online'))
                return bool(val)
    
    return False


def save_settings(settings: dict) -> None:
    try:
        with open(SETTINGS_PATH, 'w') as file:
            json.dump(settings, file, indent=2)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning('Failed to save settings: %s', exc)



from grades import *  # grading engine (extracted to grades.py — Phase 4)



def add_outlier_classes(rows: list, stat_columns: list[str], iqr_mult: float = 1.5) -> None:
    if not rows or not stat_columns:
        return
    
    for col in stat_columns:
        values = [to_number(r.get(col)) for r in rows]
        numeric = [v for v in values if v is not None]
        
        if len(numeric) < 4:
            continue
        
        series = pd.Series(numeric)
        q1 = series.quantile(0.25)
        q3 = series.quantile(0.75)
        iqr = q3 - q1
        
        if iqr == 0:
            mean = series.mean()
            std = series.std()
            if std == 0 or pd.isna(std):
                continue
            low = mean - 2 * std
            high = mean + 2 * std
        else:
            low = q1 - iqr_mult * iqr
            high = q3 + iqr_mult * iqr
        
        for r in rows:
            value = to_number(r.get(col))
            if value is None:
                continue
            
            cls = None
            if value <= low:
                cls = 'outlier-low'
            elif value >= high:
                cls = 'outlier-high'
            
            if not cls:
                continue
            
            existing = r.get(f'{col}_heat', '')
            r[f'{col}_heat'] = f'{existing} {cls}'.strip()



def format_last_update(value) -> str | None:
    if not value:
        return None
    try:
        ts = to_local_timestamp(value)
        if pd.isna(ts):
            return str(value)
        return ts.strftime('%Y-%m-%d %I:%M %p')
    except Exception:
        return str(value)


def format_date(value) -> str:
    if value is None or pd.isna(value):
        return '-'
    try:
        ts = to_local_timestamp(value)
        if pd.isna(ts):
            return str(value)
        return ts.strftime('%Y-%m-%d %I:%M %p')
    except Exception:
        return str(value)


def format_iso(value) -> str:
    try:
        if value is None or pd.isna(value):
            return ''
        ts = to_local_timestamp(value)
        if pd.isna(ts):
            return ''
        return ts.isoformat()
    except Exception:
        return ''


def format_signed(value, digits: int = 0) -> str:
    if value is None or pd.isna(value):
        return '-'
    sign = '+' if value > 0 else ''
    if digits <= 0:
        return f'{sign}{value:.0f}'
    return f'{sign}{value:.{digits}f}'


HILL_TIME_COL = 'zones_stats_stronghold_occupation_time'

# Every objective stat we can surface. Each entry: (column, emoji, label, kind).
# kind 'time' → formatted m:ss; kind 'count' → integer. These all come straight
# from the scraper (CTF / oddball / zones / extraction families already in the
# DB + caches), so adding a stat here is all it takes to make it a candidate
# highlight. Used by the "Objective Standouts" scan in session highlights.
OBJECTIVE_FEAT_CATALOG = [
    # Capture the Flag
    ('capture_the_flag_stats_flag_captures',           '🚩', 'Flag Captures',        'count'),
    ('capture_the_flag_stats_flag_returns',            '🔙', 'Flag Returns',         'count'),
    ('capture_the_flag_stats_flag_steals',             '🏴', 'Flag Steals',          'count'),
    ('capture_the_flag_stats_flag_secures',            '🔒', 'Flag Secures',         'count'),
    ('capture_the_flag_stats_kills_as_flag_carrier',   '⚔️', 'Kills as Flag Carrier','count'),
    ('capture_the_flag_stats_flag_carriers_killed',    '🎯', 'Flag Carriers Killed', 'count'),
    ('capture_the_flag_stats_time_as_flag_carrier',    '⏱️', 'Time as Flag Carrier', 'time'),
    # Oddball
    ('oddball_stats_skull_scoring_ticks',              '💀', 'Oddball Score',        'count'),
    ('oddball_stats_time_as_skull_carrier',            '⏱️', 'Oddball Time',         'time'),
    ('oddball_stats_longest_time_as_skull_carrier',    '⏳', 'Longest Oddball Hold', 'time'),
    ('oddball_stats_kills_as_skull_carrier',           '⚔️', 'Kills as Skull Carrier','count'),
    ('oddball_stats_skull_carriers_killed',            '🎯', 'Skull Carriers Killed','count'),
    # Strongholds / KOTH (zones)
    (HILL_TIME_COL,                                    '⛰️', 'Hill Time',            'time'),
    ('zones_stats_stronghold_captures',                '🚩', 'Zone Captures',        'count'),
    ('zones_stats_stronghold_secures',                 '🔒', 'Zone Secures',         'count'),
    ('zones_stats_stronghold_scoring_ticks',           '📈', 'Zone Score',           'count'),
    ('zones_stats_stronghold_offensive_kills',         '⚔️', 'Zone Offensive Kills', 'count'),
    ('zones_stats_stronghold_defensive_kills',         '🛡️', 'Zone Defensive Kills', 'count'),
    # Extraction
    ('extraction_stats_successful_extractions',        '📦', 'Extractions',          'count'),
    ('extraction_stats_extraction_conversions_completed','🔄', 'Extraction Conversions','count'),
    ('extraction_stats_extraction_initiations_completed','🟢', 'Extraction Initiations','count'),
    # Generic
    ('objectives_completed',                           '🎖️', 'Objectives Completed', 'count'),
]


def _pct_rank(value: float, arr) -> float:
    """Percentile (0–100) of ``value`` within sorted-or-unsorted array ``arr``."""
    import numpy as np
    a = np.asarray(arr, dtype=float)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return 50.0
    below = float(np.sum(a < value))
    equal = float(np.sum(a == value))
    return (below + 0.5 * equal) / a.size * 100.0


def _objective_thresholds(hist_df, q: float = 0.85) -> dict:
    """Per-objective-stat outlier threshold (q-th percentile of non-zero games in
    history). A single-game value at/above this is a genuine outlier for that
    stat. Stats with too little history are omitted (caller floors them)."""
    out = {}
    if hist_df is None or hist_df.empty:
        return out
    for col, _emoji, _label, _kind in OBJECTIVE_FEAT_CATALOG:
        if col not in hist_df.columns:
            continue
        hv = pd.to_numeric(hist_df[col], errors='coerce')
        hv = hv[hv > 0].dropna()
        if len(hv) >= 15:
            out[col] = float(hv.quantile(q))
    return out


def _objective_chips(row, thresholds: dict, max_chips: int = 3) -> list:
    """Outlier objective stats for ONE player in ONE game → compact chips.
    Only includes a stat when the player's value clears the historical outlier
    threshold (or an absolute floor when history is thin). Ranked by how far
    above the bar, capped so a card stays scannable."""
    chips = []
    for col, emoji, label, kind in OBJECTIVE_FEAT_CATALOG:
        if col not in row:
            continue
        v = safe_float(row.get(col, 0))
        if v <= 0:
            continue
        floor = 30.0 if kind == 'time' else 2.0
        thr = max(thresholds.get(col, floor), floor)
        if v < thr:
            continue
        chips.append({
            'emoji': emoji,
            'val': format_mmss(v) if kind == 'time' else f"{int(v)}",
            'lbl': label,
            '_score': (v / thr) if thr else v,
        })
    chips.sort(key=lambda c: c['_score'], reverse=True)
    for c in chips:
        c.pop('_score', None)
    return chips[:max_chips]


def _obj_dash(value, dec=1):
    """Format an objective score / rate. Slayer (and other non-objective) games
    have no objective score, so a value <= 0 renders as '—' rather than a
    misleading '0' with an F grade."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return '—'
    return '—' if v <= 0 else format_float(v, dec)


def _report_card_obj_cells(obj_totals: dict, obj_columns: list) -> dict:
    """Per-player objective cells for the report-card detail table, keyed by
    column. Each value is per-game-of-that-mode (total / games where it applied),
    matching how hill time is shown. Time stats → m:ss, counts → 1-dp."""
    out = {}
    for oc in obj_columns:
        key = oc['key']
        tot = float(obj_totals.get(key, 0) or 0)
        ng = oc.get('games', 0) or 0
        pg = (tot / ng) if ng else 0.0
        if oc['kind'] == 'time':
            display = format_mmss(pg)
            total_disp = format_mmss(tot)
        else:
            # Clean whole numbers ("5/g" not "5.0/g"); 1-dp otherwise.
            display = ('' if not pg else (f"{pg:.0f}" if pg == int(pg) else f"{pg:.1f}"))
            total_disp = f"{int(round(tot))}"
        out[key] = {'val': pg, 'display': display, 'total': total_disp}
    return out


def _hill_game_count(session_df) -> int:
    """Number of matches in a session that were 'hill/zone' games (anyone on the
    tracked squad logged zone occupation time). Used as the denominator for
    per-hill-game hill time so a Slayer-heavy night doesn't dilute it."""
    if HILL_TIME_COL not in session_df.columns or 'match_id' not in session_df.columns:
        return 0
    per_match = session_df.groupby('match_id')[HILL_TIME_COL].apply(
        lambda s: pd.to_numeric(s, errors='coerce').fillna(0).sum())
    return int((per_match > 0).sum())


def format_mmss(seconds) -> str:
    """Format a seconds count as m:ss (e.g. 83 → '1:23'). Blank for 0/None."""
    if seconds is None or pd.isna(seconds):
        return ''
    total = int(round(float(seconds)))
    if total <= 0:
        return ''
    m, s = divmod(total, 60)
    return f"{m}:{s:02d}"


def outcome_class(value: str) -> str:
    text = str(value or '').strip().lower()
    if text in ('win', 'won'):
        return 'outcome-win'
    if text in ('loss', 'lose', 'lost'):
        return 'outcome-loss'
    if text == 'tie':
        return 'outcome-tie'
    if text == 'dnf':
        return 'outcome-dnf'
    if text == 'left':
        return 'outcome-left'
    return 'outcome-unknown'


def safe_kda(kills, assists, deaths) -> float:
    kills = safe_float(kills)
    assists = safe_float(assists)
    deaths = safe_float(deaths)
    return kills + assists / 3 - deaths


def compute_best_streaks(player_df: pd.DataFrame) -> tuple[int, int]:
    """Best (longest) win and loss streaks ever for a player. Distinct from the
    later compute_streaks(df, player) which returns a current-form dict — keep
    the names separate so /hall and /leaderboard get the (max_win, max_loss)
    tuple they unpack."""
    if player_df.empty or 'outcome' not in player_df.columns or 'date' not in player_df.columns:
        return 0, 0
    ordered = player_df.copy()
    ensure_datetime(ordered)
    ordered = ordered.dropna(subset=['date']).sort_values('date')
    if ordered.empty:
        return 0, 0
    max_win = max_loss = 0
    current_win = current_loss = 0
    for outcome in ordered['outcome'].astype(str).str.lower():
        if outcome == 'win':
            current_win += 1
            current_loss = 0
        elif outcome == 'loss':
            current_loss += 1
            current_win = 0
        else:
            current_win = 0
            current_loss = 0
        if current_win > max_win:
            max_win = current_win
        if current_loss > max_loss:
            max_loss = current_loss
    return max_win, max_loss


def compute_current_streak(player_df: pd.DataFrame) -> int:
    if player_df.empty or 'outcome' not in player_df.columns or 'date' not in player_df.columns:
        return 0
    ordered = player_df.copy()
    ensure_datetime(ordered)
    ordered = ordered.dropna(subset=['date']).sort_values('date', ascending=False)
    if ordered.empty:
        return 0
    streak = 0
    for outcome in ordered['outcome'].astype(str).str.lower():
        if outcome not in ('win', 'loss'):
            if streak == 0:
                continue
            break
        if streak == 0:
            streak = 1 if outcome == 'win' else -1
            continue
        if outcome == 'win' and streak > 0:
            streak += 1
        elif outcome == 'loss' and streak < 0:
            streak -= 1
        else:
            break
    return streak


def unique_sorted(series: pd.Series) -> list:
    values = [str(v).strip() for v in series.dropna().unique().tolist() if str(v).strip()]
    return sorted(set(values))


def apply_filters(df: pd.DataFrame, player: str, playlist: str, mode: str) -> pd.DataFrame:
    filtered = df
    if player and player != 'all' and 'player_gamertag' in filtered.columns:
        filtered = filtered[filtered['player_gamertag'] == player]
    if playlist and playlist != 'all' and 'playlist' in filtered.columns:
        filtered = filtered[filtered['playlist'] == playlist]
    if mode and mode != 'all' and 'game_type' in filtered.columns:
        filtered = filtered[filtered['game_type'] == mode]
    return filtered


def extract_csr_values(df: pd.DataFrame) -> pd.Series:
    """Extract CSR values, preferring post_match_csr over pre_match_csr."""
    if df.empty:
        return pd.Series(dtype=float)
    
    post_vals = pd.to_numeric(df.get('post_match_csr', pd.Series()), errors='coerce') if 'post_match_csr' in df.columns else pd.Series()
    pre_vals = pd.to_numeric(df.get('pre_match_csr', pd.Series()), errors='coerce') if 'pre_match_csr' in df.columns else pd.Series()
    
    csr_vals = post_vals.where(post_vals > 0).combine_first(pre_vals.where(pre_vals > 0))
    return csr_vals.dropna()


def compute_csr_window_delta(player_df: pd.DataFrame, days: int) -> float | None:
    """Compute CSR change over the last N days."""
    if player_df.empty or 'date' not in player_df.columns:
        return None
    
    now = pd.Timestamp.now(tz='UTC')
    cutoff = now - pd.Timedelta(days=days)
    window_df = player_df[player_df['date'] >= cutoff].sort_values('date', ascending=True)
    
    if window_df.empty:
        return None
    
    csr_vals = extract_csr_values(window_df)
    if csr_vals.empty:
        return None
    
    return float(csr_vals.iloc[-1] - csr_vals.iloc[0])


def build_csr_overview(df: pd.DataFrame) -> list:
    """Build CSR overview showing current CSR, session change, and deltas."""
    if df.empty or 'playlist' not in df.columns or 'date' not in df.columns or 'player_gamertag' not in df.columns:
        return []
    
    ranked_df = _ranked_only(df)
    if ranked_df.empty:
        return []
    
    ensure_datetime(ranked_df)
    ranked_df = ranked_df.dropna(subset=['date'])
    if ranked_df.empty:
        return []
    
    presence = load_presence()
    rows = []
    
    for player in unique_sorted(ranked_df['player_gamertag']):
        player_df = ranked_df[ranked_df['player_gamertag'] == player].sort_values('date', ascending=False)
        if player_df.empty:
            continue
        
        latest_row = player_df.iloc[0]
        current_csr_val = latest_row.get('post_match_csr')
        if pd.isna(current_csr_val) or current_csr_val is None:
            current_csr_val = latest_row.get('pre_match_csr')
        
        # Find session matches (30 min gap)
        session_rows = [latest_row]
        prev_ts = pd.Timestamp(latest_row['date'])
        if prev_ts.tzinfo is None:
            prev_ts = prev_ts.tz_localize('UTC')
        
        for _, r in player_df.iloc[1:].iterrows():
            ts = pd.Timestamp(r['date'])
            if pd.isna(ts):
                continue
            if ts.tzinfo is None:
                ts = ts.tz_localize('UTC')
            
            if prev_ts - ts <= pd.Timedelta(minutes=SESSION_GAP_MINUTES):
                session_rows.append(r)
                prev_ts = ts
            else:
                break
        
        session_df = pd.DataFrame(session_rows).sort_values('date', ascending=True)
        session_start_csr_val = None
        if not session_df.empty:
            first_row = session_df.iloc[0]
            session_start_csr_val = first_row.get('pre_match_csr')
            if pd.isna(session_start_csr_val) or session_start_csr_val == 0:
                session_start_csr_val = first_row.get('post_match_csr')
        
        session_csr_change = None
        if current_csr_val and session_start_csr_val and current_csr_val > 0 and session_start_csr_val > 0:
            session_csr_change = current_csr_val - session_start_csr_val
        
        # Max CSR
        max_csr_val = None
        max_csr_date = None
        if 'post_match_csr' in player_df.columns:
            post_vals = pd.to_numeric(player_df['post_match_csr'], errors='coerce')
            if post_vals.notna().any():
                max_csr_val = float(post_vals.max())
                max_idx = post_vals.idxmax()
                max_csr_date = player_df.loc[max_idx, 'date']
        
        delta_7 = compute_csr_window_delta(player_df, 7)
        delta_30 = compute_csr_window_delta(player_df, 30)
        delta_90 = compute_csr_window_delta(player_df, 90)
        
        target_delta_val = None
        if current_csr_val and not pd.isna(current_csr_val) and current_csr_val > 0:
            target_delta_val = float(current_csr_val) - 1700.0
        
        rows.append({
            'player': player,
            'is_online': is_player_online(presence, player),
            'last_match_iso': format_iso(latest_row.get('date')),
            'current_csr': format_float(current_csr_val, 1) if current_csr_val and not pd.isna(current_csr_val) else '-',
            'session_start_csr': format_float(session_start_csr_val, 1) if session_start_csr_val and not pd.isna(session_start_csr_val) else '-',
            'session_csr_change': format_signed(session_csr_change, 1) if session_csr_change is not None else '-',
            'delta_7': format_signed(delta_7, 0) if delta_7 is not None else '-',
            'delta_30': format_signed(delta_30, 0) if delta_30 is not None else '-',
            'delta_90': format_signed(delta_90, 0) if delta_90 is not None else '-',
            'target_delta': format_signed(target_delta_val, 0) if target_delta_val is not None else '-',
            'max_csr': format_float(max_csr_val, 1) if max_csr_val else '-',
            'max_csr_date': format_date(max_csr_date) if max_csr_date else '-'
        })
    
    add_heatmap_classes(rows, {
        'current_csr': True, 'session_csr_change': True,
        'delta_7': True, 'delta_30': True, 'delta_90': True,
        'target_delta': True, 'max_csr': True
    })

    add_composite_grades(rows, {'current_csr': True}, 'CSR grade')

    rows.sort(key=lambda x: to_number(x['current_csr']) or -999, reverse=True)
    return rows


def build_csr_trends(df: pd.DataFrame) -> dict:
    """Build CSR trend data for all players."""
    if df.empty or 'player_gamertag' not in df.columns:
        return {}
    
    ranked_df = _ranked_only(df)
    
    if ranked_df.empty or 'date' not in ranked_df.columns:
        return {}
    
    ensure_datetime(ranked_df)
    ranked_df = ranked_df.dropna(subset=['date'])
    
    try:
        ranked_df['date_local'] = ranked_df['date'].dt.tz_convert(APP_TIMEZONE)
    except Exception:
        ranked_df['date_local'] = ranked_df['date']
    
    ranked_df['date_str'] = ranked_df['date_local'].dt.strftime('%Y-%m-%d')
    
    trends = {}
    for player in unique_sorted(ranked_df['player_gamertag']):
        player_df = ranked_df[ranked_df['player_gamertag'] == player].sort_values('date')
        if player_df.empty:
            continue
        
        player_df['csr_value'] = extract_csr_values(player_df)
        player_df = player_df.dropna(subset=['csr_value'])
        
        if player_df.empty:
            continue
        
        daily = player_df.groupby('date_str')['csr_value'].last().reset_index()
        daily['date_key'] = pd.to_datetime(daily['date_str'], errors='coerce')
        daily = daily.sort_values('date_key')
        
        trends[player] = [
            {'date': row['date_str'], 'csr': float(row['csr_value'])}
            for _, row in daily.iterrows() if pd.notna(row['csr_value'])
        ]
    
    return trends


# ---------------------------------------------------------------------------
# Session gap-clustering (canonical "latest session" helpers)
# ---------------------------------------------------------------------------

def latest_session_match_ids(match_times: pd.Series) -> set:
    """The newest gap-cluster of match ids.

    match_times: Series match_id → timestamp, sorted DESCENDING. Walks back
    from the newest match, stopping at the first gap > SESSION_GAP_MINUTES."""
    ids: set = set()
    prev = None
    for mid, mtime in match_times.items():
        if prev is not None and (prev - mtime).total_seconds() / 60 > SESSION_GAP_MINUTES:
            break
        ids.add(mid)
        prev = mtime
    return ids


def latest_session_rows(work: pd.DataFrame) -> pd.DataFrame:
    """Rows belonging to the newest gap-cluster.

    work: non-empty frame sorted by 'date' DESCENDING (one row per player per
    match is fine — consecutive rows of one match are 0 minutes apart)."""
    times = work['date'].tolist()
    keep = [work.index[0]]
    for i in range(1, len(work)):
        if (times[i - 1] - times[i]).total_seconds() / 60.0 > SESSION_GAP_MINUTES:
            break
        keep.append(work.index[i])
    return work.loc[keep]


# ---------------------------------------------------------------------------
# Squad Session Report Card
# ---------------------------------------------------------------------------

def build_squad_report_card(df: pd.DataFrame, mode: str = 'latest', player: str | None = None,
                            anchor_match_id: str | None = None,
                            session_match_ids: set | list | tuple | None = None,
                            _shared: dict | None = None) -> dict:
    """
    Grade each tracked player on a recent ranked session.

    mode='latest' – anchor on the most recent ranked match (squad OR solo) and
                    walk back within SESSION_GAP_MINUTES. A solo night still
                    gets a card (>=1 player).
    mode='squad'  – anchor on the most recent SQUAD match (2+ tracked players
                    together) and gather only squad matches (>=2 players). Used
                    to also surface the latest squad night when the very latest
                    session happened to be solo.
    player=<gt>   – per-player SOLO mode: restrict the pool to matches where this
                    player was the only tracked player, and anchor on their most
                    recent such match. Grades only that player. Used to show every
                    player's latest solo session.

    Returns a dict with keys:
        rows        – list of graded player dicts
        session_date – display date string
        game_count  – int, number of matches in the session
        players_in_session – list of gamertag strings
        kind        – 'squad' | 'solo'
        sid         – anchor match_id (latest match in the session)
    """
    if df.empty or 'match_id' not in df.columns or 'player_gamertag' not in df.columns:
        return {}

    # `_shared` is an optional cross-call memo for loops that build many cards
    # from the SAME df (build_player_solo_cards): the pool prep and the solo
    # baseline arrays are identical for every player, so compute them once.
    # Nothing downstream mutates these frames (read-only slices), so sharing
    # them is safe.
    if _shared is not None and _shared.get('ranked_df') is not None:
        ranked_df = _shared['ranked_df']
        players_per_match = _shared['players_per_match']
    else:
        ranked_df = _ranked_only(df)
        if ranked_df.empty or 'date' not in ranked_df.columns:
            return {}
        ensure_datetime(ranked_df)
        ranked_df = ranked_df.dropna(subset=['date'])
        # Build the candidate match pool. 'latest' walks back from the most
        # recent ranked match (squad or solo); 'squad' restricts to matches
        # where 2+ tracked players played together, so it surfaces the latest
        # genuine squad night.
        players_per_match = ranked_df.groupby('match_id')['player_gamertag'].nunique()
        if _shared is not None:
            _shared['ranked_df'] = ranked_df
            _shared['players_per_match'] = players_per_match
    if ranked_df.empty:
        return {}
    if player is not None:
        # Solo matches for this specific player: they were the only tracked
        # player in the match, and they actually played it.
        solo_ids = set(players_per_match[players_per_match == 1].index)
        player_ids = set(ranked_df[ranked_df['player_gamertag'] == player]['match_id'])
        pool_ids = solo_ids & player_ids
        if not pool_ids:
            return {}
        pool = ranked_df[ranked_df['match_id'].isin(pool_ids)]
    elif mode == 'squad':
        squad_ids = set(players_per_match[players_per_match >= 2].index)
        if not squad_ids:
            return {}
        pool = ranked_df[ranked_df['match_id'].isin(squad_ids)]
    elif mode == 'solo':
        # Solo dashboard: matches where exactly ONE tracked player played.
        solo_ids = set(players_per_match[players_per_match == 1].index)
        if not solo_ids:
            return {}
        pool = ranked_df[ranked_df['match_id'].isin(solo_ids)]
    else:
        pool = ranked_df

    explicit_session_ids = {str(mid) for mid in session_match_ids or []}
    if explicit_session_ids:
        wanted_ids = explicit_session_ids
        pool = pool[pool['match_id'].astype(str).isin(wanted_ids)]

    match_times = pool.groupby('match_id')['date'].max().sort_values(ascending=False)
    if match_times.empty:
        return {}
    session_match_ids: set = set()
    if explicit_session_ids:
        session_match_ids = set(match_times.index)
    elif anchor_match_id is not None and anchor_match_id in match_times.index:
        # Browse mode: build the card for the gap-cluster that CONTAINS the
        # requested anchor match (any recent session), not just the latest.
        _cluster, _prev = [], None
        for mid, mtime in match_times.items():
            if _prev is not None and (_prev - mtime).total_seconds() / 60 > SESSION_GAP_MINUTES:
                if anchor_match_id in _cluster:
                    break
                _cluster = []
            _cluster.append(mid)
            _prev = mtime
        session_match_ids = set(_cluster) if anchor_match_id in _cluster else {anchor_match_id}
    else:
        # Default: the most recent session (walk back from the newest match).
        session_match_ids = latest_session_match_ids(match_times)
    session_df = pool[pool['match_id'].isin(session_match_ids)].copy()

    # 'squad' needs 2+ players; 'latest' just needs someone.
    session_players = list(session_df['player_gamertag'].unique())
    if len(session_players) < (2 if mode == 'squad' else 1):
        return {}

    # Squad night if any match in the session had 2+ tracked players together;
    # otherwise it was a solo grind.
    sess_ppm = session_df.groupby('match_id')['player_gamertag'].nunique()
    squad_games_in_session = int((sess_ppm >= 2).sum())
    session_kind = 'squad' if squad_games_in_session > 0 else 'solo'

    game_count = session_df['match_id'].nunique()
    hill_games = _hill_game_count(session_df)
    # Which OTHER objective stats actually happened this session (hill has its own
    # column). Each becomes a per-game column, with its own denominator = number
    # of games where that objective applied (anyone logged it).
    obj_columns = []
    for _col, _emoji, _label, _kind in OBJECTIVE_FEAT_CATALOG:
        if _col == HILL_TIME_COL or _col not in session_df.columns:
            continue
        if numeric_series(session_df, _col).sum() <= 0:
            continue
        _pm = session_df.groupby('match_id')[_col].apply(
            lambda s: pd.to_numeric(s, errors='coerce').fillna(0).sum())
        obj_columns.append({'key': _col, 'emoji': _emoji, 'label': _label,
                            'kind': _kind, 'games': int((_pm > 0).sum())})
    session_max_dt = session_df['date'].max()
    session_date_str = format_date(session_max_dt)
    session_ts = float(session_max_dt.timestamp()) if pd.notna(session_max_dt) else 0.0
    # Anchor sid = the latest match in the session (for "view full session" link).
    session_sid = str(session_df.sort_values('date')['match_id'].iloc[-1])

    # Session-wide game numbering: every player's G-numbers refer to the same
    # chronological session order, so a late joiner's first game reads "G6"
    # (the squad's 6th game of the night), not "G1".
    _sess_game_num = {mid: i for i, mid in enumerate(
        session_df.sort_values('date').drop_duplicates('match_id')['match_id'].astype(str), 1)}

    # Automatic cheat-check for every game of the session — flags land on the
    # game-by-game breakdown without opening each match page.
    try:
        _sess_sus = _sus_flags_for_matches(ENGINE, list(_sess_game_num.keys()))
    except Exception as _sus_exc:
        logger.warning('session sus flags failed: %s', _sus_exc)
        _sess_sus = {}

    # ── Per-player stats ─────────────────────────────────────────
    stat_rows: list[dict] = []
    for player in session_players:
        p_df = session_df[session_df['player_gamertag'] == player]
        games = len(p_df)
        if games == 0:
            continue

        outcomes = p_df['outcome'].astype(str).str.lower() if 'outcome' in p_df.columns else pd.Series()
        wins = int((outcomes == 'win').sum()) if not outcomes.empty else 0
        win_pct = wins / games * 100

        total_kills  = numeric_series(p_df, 'kills').sum()
        total_deaths = numeric_series(p_df, 'deaths').sum()
        total_assists = numeric_series(p_df, 'assists').sum()
        total_perfect = numeric_series(p_df, 'medal_perfect').sum()
        total_hill = numeric_series(p_df, HILL_TIME_COL).sum()
        obj_totals = {oc['key']: numeric_series(p_df, oc['key']).sum() for oc in obj_columns}
        kills_pg   = total_kills / games
        deaths_pg  = total_deaths / games
        assists_pg = total_assists / games
        perfect_pg = total_perfect / games
        kda = safe_kda(kills_pg, assists_pg, deaths_pg)
        kd1 = kills_pg / deaths_pg if deaths_pg > 0 else kills_pg

        fired = numeric_series(p_df, 'shots_fired').sum()
        hit   = numeric_series(p_df, 'shots_hit').sum()
        accuracy = hit / fired * 100 if fired > 0 else 0.0

        total_dmg_dealt = numeric_series(p_df, 'damage_dealt').sum()
        total_dmg_taken = numeric_series(p_df, 'damage_taken').sum()
        dmg_plus_pg = total_dmg_dealt / games
        dmg_minus_pg = total_dmg_taken / games
        dmg_diff = total_dmg_dealt - total_dmg_taken

        total_dur = numeric_series(p_df, 'duration').sum()
        dmg_per_min = total_dmg_dealt / (total_dur / 60) if total_dur > 0 else 0.0
        dmg_diff_pg = (total_dmg_dealt - total_dmg_taken) / games

        avg_life_pg = float(numeric_series(p_df, 'average_life_duration').mean() or 0.0)

        score_pg = score_series(p_df).sum() / games if games else 0

        obj_scores = objective_score_series(p_df)
        _obj_games = int((obj_scores > 0).sum()) if not obj_scores.empty else 0
        obj_score_pg = float(obj_scores.sum()) / _obj_games if _obj_games else 0.0

        team_score = pd.to_numeric(p_df.get('team_personal_score', 0), errors='coerce').fillna(0).sum()
        total_score = score_series(p_df).sum()
        score_pct = float(total_score / team_score * 100) if team_score > 0 else 0.0

        # CSR delta across the session
        p_asc = p_df.sort_values('date', ascending=True)
        post_vals = pd.to_numeric(p_asc.get('post_match_csr', pd.Series()), errors='coerce')
        post_vals = post_vals[post_vals > 0]
        pre_vals  = pd.to_numeric(p_asc.get('pre_match_csr', pd.Series()), errors='coerce')
        pre_vals  = pre_vals[pre_vals > 0]
        csr_delta: float | None = None
        if not post_vals.empty and not pre_vals.empty:
            csr_delta = float(post_vals.iloc[-1]) - float(pre_vals.iloc[0])
        current_csr = float(post_vals.iloc[-1]) if not post_vals.empty else None

        # Every-game grade: an absolute Game Grade per match in the session
        # (chronological), so players can see how each individual game scored.
        # Each entry also carries the game's context (map/mode/result/KDA) so the
        # mobile game-by-game list can show what each game actually was, plus a
        # "worth watching" flag when an outlier slaying/objective stat happened.
        _obj_by_idx = objective_score_series(p_df)  # index-aligned per-game objective score
        _pk_series = numeric_series(p_df, 'kills')
        _kills_hi = max(20.0, float(_pk_series.quantile(0.9))) if len(_pk_series) >= 3 else 20.0
        _obj_pos = _obj_by_idx[_obj_by_idx > 0]
        _obj_hi = float(_obj_pos.quantile(0.75)) if len(_obj_pos) >= 3 else 0.0
        game_grades = []
        for _gi, (_idx, grow) in enumerate(p_df.sort_values('date').iterrows(), start=1):
            gg = _match_grade_for_row(grow)
            if not gg:
                continue
            _oc = str(grow.get('outcome', '')).lower()
            _gk = safe_float(grow.get('kills', 0))
            _gd = safe_float(grow.get('deaths', 0))
            _ga = safe_float(grow.get('assists', 0))
            _gkda = safe_kda(_gk, _ga, _gd)
            _gobj = float(_obj_by_idx.get(_idx, 0.0) or 0.0)
            _gbase = gg.get('grade', '')
            # Outlier detection — flag genuinely notable games (slaying OR objective)
            _reasons = []
            if _gk >= _kills_hi and _gk >= 18:
                _reasons.append(f"🔫 {int(_gk)}-kill game")
            elif _gkda >= 6:
                _reasons.append(f"🔫 {_gkda:.1f} KDA")
            if _obj_hi > 0 and _gobj >= _obj_hi and _gobj > 0:
                _reasons.append(f"🎯 {_gobj:.0f} obj score")
            if _gd <= 3 and _gk >= 12:
                _reasons.append(f"🧼 only {int(_gd)} death{'s' if int(_gd) != 1 else ''}")
            if _gbase in ('S', 'A+') and not _reasons:
                _reasons.append(f"⭐ {_gbase} game")
            game_grades.append({
                # Session-wide game number (shared across players), falling
                # back to the per-player sequence if the map misses.
                'num': _sess_game_num.get(str(grow.get('match_id', '')), _gi),
                'grade': _gbase,
                'grade_class': gg.get('grade_class', ''),
                'score': gg.get('grade_score', 0),
                'emoji': _grade_emoji_square(_gbase),
                'won': _oc == 'win',
                'result': 'W' if _oc == 'win' else ('T' if _oc == 'tie' else 'L'),
                'result_class': outcome_class(grow.get('outcome', '')),
                'map': normalize_map_name(grow.get('map', '')),
                'mode': clean_mode(grow.get('game_type', '')),
                'kda': f"{_gkda:.1f}",
                'kills': int(_gk), 'deaths': int(_gd), 'assists': int(_ga),
                'watch': bool(_reasons),
                'watch_reason': ' · '.join(_reasons[:2]),
                'sus_kind': _sess_sus.get(str(grow.get('match_id', '')), {}).get('kind', ''),
                'sus_tip': _sess_sus.get(str(grow.get('match_id', '')), {}).get('tip', ''),
                'mid': str(grow.get('match_id', '')),  # for the live add/remove regrade
            })
        _gscores = [g['score'] for g in game_grades if g.get('score') is not None]
        avg_game_score = round(sum(_gscores) / len(_gscores)) if _gscores else None
        avg_game_grade = grade_from_percentile(avg_game_score) if avg_game_score is not None else ''

        stat_rows.append({
            'player': player,
            'games': games,
            'game_grades': game_grades,
            'avg_game_grade': avg_game_grade,
            'avg_game_score': avg_game_score,
            'avg_game_grade_class': grade_class(avg_game_grade) if avg_game_grade else '',
            'wins': wins,
            'win_pct': win_pct,
            'kda': kda,
            'kd1': kd1,
            'kills_pg': kills_pg,
            'deaths_pg': deaths_pg,
            'assists_pg': assists_pg,
            'perfect_pg': perfect_pg,
            'perfect_total': int(total_perfect),
            'hill_secs': float(total_hill),
            'obj_totals': obj_totals,
            'accuracy': accuracy,
            'dmg_plus_pg': dmg_plus_pg,
            'dmg_minus_pg': dmg_minus_pg,
            'dmg_diff_pg': dmg_diff_pg,
            'dmg_per_min': dmg_per_min,
            'kda_min': kda_per_min(p_df),
            'obj_min': obj_per_min(p_df),
            'avg_life_pg': avg_life_pg,
            'score_pg': score_pg,
            'obj_score_pg': obj_score_pg,
            'score_pct': score_pct,
            'csr_delta': csr_delta,
            'current_csr': current_csr,
            'opp_mmr': (lambda _o: round(float(_o.mean())) if len(_o) else None)(
                pd.to_numeric(p_df.get('enemy_team_mmr'), errors='coerce').dropna()
                if 'enemy_team_mmr' in p_df.columns else pd.Series(dtype=float)),
        })

    if not stat_rows:
        return {}

    # ── Stack-size records ────────────────────────────────────────
    # A match's "stack" = how many tracked players queued together in it; the
    # squad shares one team result. Report 4/3/2-stack W-L, plus a per-player
    # solo record for anyone who played alone in the session.
    #
    # mode='squad' excludes solo games from the card pool, so re-derive the full
    # "sitting" — every ranked match (squad OR solo) gap-connected to the squad
    # session — so solo games interspersed in the same night still count. Single
    # pass: gap-cluster all ranked matches (desc by time), take the cluster that
    # overlaps the squad session.
    night_ids = set(session_match_ids)
    if 'date' in ranked_df.columns and not ranked_df.empty:
        _mt = ranked_df.dropna(subset=['date']).groupby('match_id')['date'].max().sort_values(ascending=False)
        _gap = pd.Timedelta(minutes=SESSION_GAP_MINUTES)
        _cluster, _prev, _chosen = [], None, None
        for _mid, _t in _mt.items():
            if _prev is not None and (_prev - _t) > _gap:
                if session_match_ids & set(_cluster):
                    _chosen = _cluster
                    break
                _cluster = []
            _cluster.append(_mid)
            _prev = _t
        if _chosen is None and (session_match_ids & set(_cluster)):
            _chosen = _cluster
        if _chosen:
            night_ids = set(_chosen)
    night_df = ranked_df[ranked_df['match_id'].isin(night_ids)].copy() if night_ids else session_df

    def _match_won(g) -> int | None:
        if 'outcome' not in g.columns:
            return None
        o = g['outcome'].astype(str).str.lower()
        ow = o[o.isin(['win', 'loss', 'lose', 'loose'])]
        if ow.empty:
            return None
        return 1 if ow.iloc[0] == 'win' else 0

    # Group by the exact roster (set of tracked players queued together), so the
    # records show WHO was in each stack — two different trios are two rows.
    _stack_acc: dict = {}     # roster(tuple of gamertags) -> [wins, losses]
    _solo_acc: dict = {}      # player -> [wins, losses]
    for _mid, _g in night_df.groupby('match_id'):
        _won = _match_won(_g)
        if _won is None:
            continue
        _roster = tuple(sorted(_g['player_gamertag'].astype(str).unique()))
        if len(_roster) >= 2:
            acc = _stack_acc.setdefault(_roster, [0, 0])
        else:
            acc = _solo_acc.setdefault(_roster[0], [0, 0])
        acc[0 if _won else 1] += 1

    def _rec_tone(w, l) -> str:
        return 'rec-pos' if w > l else ('rec-neg' if l > w else 'rec-even')

    stack_records = []
    for _roster, (w, l) in sorted(_stack_acc.items(),
                                  key=lambda kv: (-len(kv[0]), -(kv[1][0] + kv[1][1]), kv[0])):
        stack_records.append({
            'label': f"{len(_roster)}-stack", 'size': len(_roster),
            'players': list(_roster), 'players_str': ', '.join(_roster),
            'record': f"{w}-{l}", 'games': w + l, 'tone': _rec_tone(w, l),
        })
    solo_records = []
    for _pl, (w, l) in sorted(_solo_acc.items(), key=lambda kv: (-(kv[1][0] + kv[1][1]), kv[0])):
        solo_records.append({
            'player': _pl, 'record': f"{w}-{l}", 'games': w + l, 'tone': _rec_tone(w, l),
        })

    # ── Session ranking: where does THIS night rank vs the last 30 days? ──
    # Cluster the same pool the card was built from (squad matches for mode=
    # 'squad', all ranked for 'latest') into gap-separated sessions, score each
    # by win rate, and report the current session's rank. Win rate is
    # volume-adjusted with a Wilson lower bound so a lone 1-0 night can't
    # outrank a sustained 12-4 night. Shared team result per match (_match_won),
    # matching the records above.
    session_rank = None
    try:
        _rmt = pool.dropna(subset=['date']).groupby('match_id')['date'].max().sort_values(ascending=False)
        if not _rmt.empty:
            _rwin = pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=30)
            # Only need matches in the 30d window plus the current session's.
            _recent_ids = set(_rmt[_rmt >= _rwin].index) | set(session_match_ids)
            _rmt = _rmt[_rmt.index.isin(_recent_ids)]
            _res_map = {}
            for _mid, _g in pool[pool['match_id'].isin(_recent_ids)].groupby('match_id'):
                _res_map[_mid] = _match_won(_g)

            _rgap = pd.Timedelta(minutes=SESSION_GAP_MINUTES)
            _clusters, _cl, _prev = [], [], None
            for _mid, _t in _rmt.items():
                if _prev is not None and (_prev - _t) > _rgap:
                    _clusters.append(_cl)
                    _cl = []
                _cl.append(_mid)
                _prev = _t
            if _cl:
                _clusters.append(_cl)

            def _wilson_lb(w: int, n: int) -> float:
                if n <= 0:
                    return 0.0
                z = 1.96
                phat = w / n
                denom = 1 + z * z / n
                centre = phat + z * z / (2 * n)
                margin = z * ((phat * (1 - phat) + z * z / (4 * n)) / n) ** 0.5
                return (centre - margin) / denom

            _scored = []
            for _cl in _clusters:
                _w = sum(1 for _m in _cl if _res_map.get(_m) == 1)
                _l = sum(1 for _m in _cl if _res_map.get(_m) == 0)
                _decided = _w + _l
                _is_cur = bool(set(_cl) & set(session_match_ids))
                if _decided >= 1 and _is_cur:
                    _scored.append({'w': _w, 'l': _l, 'decided': _decided,
                                    'key': _wilson_lb(_w, _decided), 'current': True})
                elif _decided >= 1:
                    _scored.append({'w': _w, 'l': _l, 'decided': _decided,
                                    'key': _wilson_lb(_w, _decided), 'current': False})
            if any(s['current'] for s in _scored) and len(_scored) >= 2:
                _scored.sort(key=lambda s: (s['key'], s['decided']), reverse=True)
                _cur_i = next(i for i, s in enumerate(_scored) if s['current'])
                _cs = _scored[_cur_i]
                _wp = _cs['w'] / _cs['decided'] * 100 if _cs['decided'] else 0.0
                session_rank = {
                    'rank': _cur_i + 1,
                    'total': len(_scored),
                    'window_days': 30,
                    'record': f"{_cs['w']}-{_cs['l']}",
                    'win_pct': f"{_wp:.0f}%",
                    'kind': session_kind,
                }
    except Exception:
        session_rank = None

    # ── Historical baselines (last 365 days, raw columns → same formula as session) ─────
    # Using rolling 1-year window so grades reflect current skill level, not old history.
    # In solo mode (player=...) this whole block is player-independent, so a
    # _shared memo skips it after the first card of the loop.
    if player is not None and _shared is not None and 'solo_hist_df' in _shared:
        hist_df = _shared['solo_hist_df']
    else:
        _cutoff_1y = pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=365)
        hist_df = ranked_df[ranked_df['date'] >= _cutoff_1y] if 'date' in ranked_df.columns else ranked_df
        if hist_df.empty:
            hist_df = ranked_df  # fall back to all-time if <1yr of data

        # Solo report cards grade ONLY against solo history. Queuing alone is a
        # different game than stacking, so grading a solo night against squad games
        # (or the reverse) skews it. When player=... (solo mode), restrict the
        # baseline distribution to matches where just ONE tracked player played.
        if player is not None and 'match_id' in hist_df.columns:
            _solo_hist_ids = set(players_per_match[players_per_match == 1].index)
            _hist_solo = hist_df[hist_df['match_id'].isin(_solo_hist_ids)]
            if not _hist_solo.empty:
                hist_df = _hist_solo
            if _shared is not None:
                _shared['solo_hist_df'] = hist_df

    def _hist_stats(series: pd.Series) -> tuple[float, float]:
        """Returns (mean, std) for display purposes only."""
        s = series.dropna()
        if s.empty:
            return 0.0, 1.0
        return float(s.mean()), max(float(s.std(ddof=0)), 1e-6)

    # ── Grading helpers ───────────────────────────────────────────
    # One implementation, shared with /api/regrade + build_grade_timeline: the
    # module-level _rc_* helpers. Local aliases keep this function readable.
    _sorted_arr = _rc_sorted_arr
    _percentile = _rc_percentile
    _grade_pct = _rc_grade_pct
    _grade_class = _rc_grade_class
    _heat_class_from_pct = _rc_heat
    _build_arrays = _rc_build_arrays

    def _pct_tip(label: str, value: float, pct: float, p90: float, p50: float) -> str:
        return f"{label}: {value:.2f}  (top {100-pct:.0f}%,  50th={p50:.1f}  S≥{p90:.1f})"

    # Sorted percentile-lookup arrays over the history frame, so each stat can
    # be graded against the squad's pooled history (default) or each player's
    # own history (self toggle). Also memoized across a solo-cards loop.
    if player is not None and _shared is not None and 'solo_arrays' in _shared:
        squad_arrays = _shared['solo_arrays']
        _hist_by_player = _shared['solo_hist_by_player']
        _self_arrays_cache = _shared.setdefault('solo_self_arrays', {})
    else:
        squad_arrays = _build_arrays(hist_df)
        # Per-player history frames for self-relative grading. A player with too
        # few games of their own history falls back to the squad arrays so their
        # grades aren't statistical noise.
        _hist_by_player = (
            {gt: f for gt, f in hist_df.groupby('player_gamertag')}
            if 'player_gamertag' in hist_df.columns else {}
        )
        _self_arrays_cache = {}
        if player is not None and _shared is not None:
            _shared['solo_arrays'] = squad_arrays
            _shared['solo_hist_by_player'] = _hist_by_player
            _shared['solo_self_arrays'] = _self_arrays_cache
    _MIN_SELF_GAMES = 20

    # Win-correlated composite weights (top 5 stats that actually move with
    # winning for this squad) — used for tonight AND the history pool below,
    # so the two sides of every percentile always speak the same language.
    if player is not None and _shared is not None and 'solo_weights' in _shared:
        W = _shared['solo_weights']
    else:
        W = _rc_win_weights(hist_df)
        if player is not None and _shared is not None:
            _shared['solo_weights'] = W


    def _self_arrays_for(player_gt) -> dict:
        if player_gt in _self_arrays_cache:
            return _self_arrays_cache[player_gt]
        pf = _hist_by_player.get(player_gt)
        A = squad_arrays if (pf is None or len(pf) < _MIN_SELF_GAMES) else _build_arrays(pf)
        _self_arrays_cache[player_gt] = A
        return A

    # Back-compat names for the squad-wide baselines legend below.
    arr_kda      = squad_arrays['kda']
    arr_ddiff    = squad_arrays['ddiff']
    arr_life     = squad_arrays['life']
    arr_obj      = squad_arrays['obj']
    arr_scorepct = squad_arrays['scorepct']
    arr_dmgmin   = squad_arrays['dmgmin']
    arr_kdamin   = squad_arrays['kdamin']
    arr_objmin   = squad_arrays['objmin']

    import numpy as np

    def _p(arr, q):
        """q-th percentile value from sorted array, or 0 if empty."""
        return float(np.percentile(arr, q)) if len(arr) else 0.0

    def _grade_set(r: dict, A: dict) -> dict:
        """Grade one player's session stats against the arrays `A`
        (either squad-wide pooled history or that player's own history).
        Returns letter grades, grade-colour classes, heat classes, and tips."""
        pct_kda    = _percentile(r['kda'],            A['kda'])
        pct_ddiff  = _percentile(r['dmg_diff_pg'],     A['ddiff'])
        pct_life   = _percentile(r['avg_life_pg'],     A['life'])
        pct_obj    = _percentile(r['obj_score_pg'],    A['obj'])
        pct_sc     = _percentile(r['score_pct'],       A['scorepct'])
        pct_dmgm   = _percentile(r['dmg_per_min'],     A['dmgmin'])
        pct_kdamin = _percentile(r.get('kda_min', 0),  A['kdamin'])
        pct_objmin = _percentile(r.get('obj_min', 0),  A['objmin'])

        _pcts = {'kda': pct_kda, 'ddiff': pct_ddiff, 'life': pct_life,
                 'obj': pct_obj, 'scorepct': pct_sc, 'dmgmin': pct_dmgm,
                 'kdamin': pct_kdamin, 'objmin': pct_objmin}
        composite_pct = (0.75 * sum(w * _pcts[k] for k, w in W.items())
                         + 0.25 * float(r.get('win_pct') or 0.0))

        kda_g    = _grade_pct(pct_kda)
        ddiff_g  = _grade_pct(pct_ddiff)
        life_g   = _grade_pct(pct_life)
        obj_g    = _grade_pct(pct_obj)
        score_g  = _grade_pct(pct_sc)
        dmgm_g   = _grade_pct(pct_dmgm)
        kdamin_g = _grade_pct(pct_kdamin)
        objmin_g = _grade_pct(pct_objmin)

        # Slayer / no-objective sessions have no objective score — show no grade
        # (the value renders as '—') instead of a misleading 0 / F.
        if r['obj_score_pg'] is None or r['obj_score_pg'] <= 0:
            obj_g = ''
        if r.get('obj_min') is None or r.get('obj_min', 0) <= 0:
            objmin_g = ''

        grade = _grade_pct(composite_pct)
        return {
            'composite': composite_pct,
            # Raw percentile numbers (0-100 vs the grading pool) so the share/
            # copy text can speak in numbers instead of letter grades.
            'kda_pctile': round(pct_kda),
            'kda_min_pctile': round(pct_kdamin),
            'obj_min_pctile': (round(pct_objmin) if objmin_g else None),
            'ddiff_pctile': round(pct_ddiff),
            'life_pctile': round(pct_life),
            'obj_pctile': (round(pct_obj) if obj_g else None),
            'scorepct_pctile': round(pct_sc),
            'dmgm_pctile': round(pct_dmgm),
            'grade': grade, 'grade_class': _grade_class(grade),
            'grade_tip': f"composite {composite_pct:.0f}/100 — 75% performance (win-correlated stats) + 25% session result  (S=top 10%, A=top 25%, B=top 50%)",
            'kda_grade': kda_g, 'kda_grade_class': _grade_class(kda_g),
            'kda_tip': _pct_tip('KDA', r['kda'], pct_kda, _p(A['kda'], 90), _p(A['kda'], 50)),
            'kda_heat': _heat_class_from_pct(pct_kda),
            'kda_min_grade': kdamin_g, 'kda_min_grade_class': _grade_class(kdamin_g),
            'kda_min_tip': _pct_tip('KDA/min', r.get('kda_min', 0), pct_kdamin, _p(A['kdamin'], 90), _p(A['kdamin'], 50)),
            'kda_min_heat': _heat_class_from_pct(pct_kdamin),
            'obj_min_grade': objmin_g, 'obj_min_grade_class': _grade_class(objmin_g),
            'obj_min_tip': _pct_tip('Obj/min', r.get('obj_min', 0), pct_objmin, _p(A['objmin'], 90), _p(A['objmin'], 50)),
            'obj_min_heat': _heat_class_from_pct(pct_objmin),
            'ddiff_grade': ddiff_g, 'ddiff_grade_class': _grade_class(ddiff_g),
            'ddiff_tip': _pct_tip('Dmg±/g', r['dmg_diff_pg'], pct_ddiff, _p(A['ddiff'], 90), _p(A['ddiff'], 50)),
            'ddiff_heat': _heat_class_from_pct(pct_ddiff),
            'life_grade': life_g, 'life_grade_class': _grade_class(life_g),
            'life_tip': _pct_tip('Life', r['avg_life_pg'], pct_life, _p(A['life'], 90), _p(A['life'], 50)),
            'life_heat': _heat_class_from_pct(pct_life),
            'obj_grade': obj_g, 'obj_grade_class': _grade_class(obj_g),
            'obj_tip': _pct_tip('ObjSc', r['obj_score_pg'], pct_obj, _p(A['obj'], 90), _p(A['obj'], 50)),
            'obj_heat': _heat_class_from_pct(pct_obj),
            'obj_score_heat': _heat_class_from_pct(pct_obj),
            'score_grade': score_g, 'score_grade_class': _grade_class(score_g),
            'score_tip': _pct_tip('Sc%', r['score_pct'], pct_sc, _p(A['scorepct'], 90), _p(A['scorepct'], 50)),
            'score_heat': _heat_class_from_pct(pct_sc),
            'dmgm_grade': dmgm_g, 'dmgm_grade_class': _grade_class(dmgm_g),
            'dmgm_tip': _pct_tip('Dmg/m', r['dmg_per_min'], pct_dmgm, _p(A['dmgmin'], 90), _p(A['dmgmin'], 50)),
            'dmgm_heat': _heat_class_from_pct(pct_dmgm),
        }

    # ── Baselines legend ──────────────────────────────────────────
    # p5/p95 = bottom-5% / top-5% of all squad games, so anyone reading the
    # numbers knows what "awful" and "elite" actually look like per stat.
    baselines = {
        'KDA':    {'avg': round(_p(arr_kda,    50), 2), 'S': f'≥{_p(arr_kda,    90):.1f}',
                   'p5': f'{_p(arr_kda, 5):.1f}', 'p95': f'{_p(arr_kda, 95):.1f}',
                   'note': 'K+A/3−D per match · S = top 10%'},
        'KDA/min': {'avg': round(_p(arr_kdamin, 50), 2), 'S': f'≥{_p(arr_kdamin, 90):.2f}',
                   'p5': f'{_p(arr_kdamin, 5):.2f}', 'p95': f'{_p(arr_kdamin, 95):.2f}',
                   'note': 'KDA per minute of play · S = top 10%'},
        'Dmg±/g': {'avg': round(_p(arr_ddiff,  50), 0), 'S': f'≥{_p(arr_ddiff,  90):+.0f}',
                   'p5': f'{_p(arr_ddiff, 5):+.0f}', 'p95': f'{_p(arr_ddiff, 95):+.0f}',
                   'note': 'dealt−taken per match · S = top 10%'},
        'Life':   {'avg': round(_p(arr_life,   50), 1), 'S': f'≥{_p(arr_life,   90):.1f}s',
                   'p5': f'{_p(arr_life, 5):.1f}s', 'p95': f'{_p(arr_life, 95):.1f}s',
                   'note': 'avg life seconds (≥5s matches only) · S = top 10%'},
        'ObjSc':  {'avg': round(_p(arr_obj,    50), 0), 'S': f'≥{_p(arr_obj,    90):.0f}',
                   'p5': f'{_p(arr_obj, 5):.0f}', 'p95': f'{_p(arr_obj, 95):.0f}',
                   'note': 'obj score (objective modes only) · S = top 10%'},
        'Obj/min': {'avg': round(_p(arr_objmin, 50), 1), 'S': f'≥{_p(arr_objmin, 90):.1f}',
                   'p5': f'{_p(arr_objmin, 5):.1f}', 'p95': f'{_p(arr_objmin, 95):.1f}',
                   'note': 'objective score per minute (objective modes only) · S = top 10%'},
        'Sc%':    {'avg': round(_p(arr_scorepct,50), 1), 'S': f'≥{_p(arr_scorepct,90):.1f}%',
                   'p5': f'{_p(arr_scorepct, 5):.1f}%', 'p95': f'{_p(arr_scorepct, 95):.1f}%',
                   'note': '% of team personal score · S = top 10%'},
        'Dmg/m':  {'avg': round(_p(arr_dmgmin, 50), 0), 'S': f'≥{_p(arr_dmgmin, 90):.0f}',
                   'p5': f'{_p(arr_dmgmin, 5):.0f}', 'p95': f'{_p(arr_dmgmin, 95):.0f}',
                   'note': 'damage per minute · S = top 10%'},
    }

    # ── Grade each player ─────────────────────────────────────────
    # Weighted composite: KDA 30% · Dmg Diff 27% · Avg Life 16% · Obj Score 11% · Sc% 9% · Dmg/Min 7%
    rows: list[dict] = []
    for r in stat_rows:
        # Squad-wide grades (default render) + self-relative grades (toggle).
        S = _grade_set(r, squad_arrays)
        SELF = _grade_set(r, _self_arrays_for(r['player']))

        csr_d = r['csr_delta']
        row = {
            'player':      r['player'],
            'games':       r['games'],
            'wins':        r['wins'],
            'win_pct':     f"{r['win_pct']:.0f}%",
            'kda':         f"{r['kda']:.2f}",
            'kda_min':     f"{r.get('kda_min', 0):.2f}",
            'obj_min':     _obj_dash(r.get('obj_min', 0)),
            'kd1':         f"{r['kd1']:.2f}",
            'kills':       f"{r['kills_pg']:.1f}",
            'deaths':      f"{r['deaths_pg']:.1f}",
            'assists':     f"{r['assists_pg']:.1f}",
            'perfect_pg':  f"{r['perfect_pg']:.2f}",
            'perfect_total': r['perfect_total'],
            'hill_games':  hill_games,
            'hill_pg_secs': (r['hill_secs'] / hill_games) if hill_games else 0.0,
            'hill_pg':     format_mmss(r['hill_secs'] / hill_games) if hill_games else '',
            'hill_total':  format_mmss(r['hill_secs']),
            'objs':        _report_card_obj_cells(r.get('obj_totals', {}), obj_columns),
            'accuracy':    f"{r['accuracy']:.1f}%",
            'dmg_plus':    f"{r['dmg_plus_pg']:.0f}",
            'dmg_minus':   f"{r['dmg_minus_pg']:.0f}",
            'dmg_diff':    format_signed(r['dmg_diff_pg'] * r['games'], 0),
            'dmg_diff_pg': format_signed(r['dmg_diff_pg'], 0),
            'dmg_per_min': f"{r['dmg_per_min']:.0f}",
            'avg_life':    f"{r['avg_life_pg']:.1f}s",
            'obj_score':   _obj_dash(r['obj_score_pg']),
            'score_pct':   f"{r['score_pct']:.1f}%",
            'csr_delta':   format_signed(csr_d, 0) if csr_d is not None else '—',
            'opp_mmr':     r.get('opp_mmr'),
            'current_csr': f"{r['current_csr']:.0f}" if r['current_csr'] else '—',
            'composite':   S['composite'],
            'game_grades': r.get('game_grades', []),
            'avg_game_grade': r.get('avg_game_grade', ''),
            'avg_game_score': r.get('avg_game_score'),
            'avg_game_grade_class': r.get('avg_game_grade_class', ''),
        }
        # Default keys = squad-relative; *_self keys = self-relative (JS toggle).
        for k, v in S.items():
            if k != 'composite':
                row[k] = v
        for k, v in SELF.items():
            if k != 'composite':
                row[k + '_self'] = v

        # Extra vs-365d-history percentiles for the Full Stat Table's cell
        # tint (data-pct). Convention: 0 = bad → 100 = good, ALWAYS — deaths
        # and damage-taken are inverted HERE (lower is better) so the JS can
        # treat every data-pct identically. Squad pool only (no self toggle;
        # the tint is a fixed vs-history reading, not a grading mode).
        _xa = squad_arrays
        row['kills_pctile']    = round(_percentile(r['kills_pg'],       _xa.get('kills', ())))
        row['deaths_pctile']   = round(100 - _percentile(r['deaths_pg'], _xa.get('deaths', ())))
        row['assists_pctile']  = round(_percentile(r['assists_pg'],     _xa.get('assists', ())))
        row['kd1_pctile']      = round(_percentile(r['kd1'],            _xa.get('kd', ())))
        row['acc_pctile']      = round(_percentile(r['accuracy'],       _xa.get('acc', ())))
        row['dmgplus_pctile']  = round(_percentile(r['dmg_plus_pg'],    _xa.get('dmgplus', ())))
        row['dmgminus_pctile'] = round(100 - _percentile(r['dmg_minus_pg'], _xa.get('dmgminus', ())))
        row['perfect_pctile']  = round(_percentile(r['perfect_pg'],     _xa.get('perfect', ())))

        # Norm & form: the player's own 365d body of work (ALL their games,
        # not session-sliced), aggregated and graded on the same squad game
        # scale as tonight. form_delta = tonight's composite minus their norm.
        _pf = _hist_by_player.get(r['player'])
        if _pf is not None and len(_pf) >= 30:
            _norm_stats = _rc_session_stats(_pf)
            _norm = _rc_composite(_norm_stats, squad_arrays, W) if _norm_stats else None
        else:
            _norm = None
        row['usual_score'] = round(_norm) if _norm is not None else None
        row['form_delta'] = round(S['composite'] - _norm) if _norm is not None else None
        rows.append(row)

    rows.sort(key=lambda x: x['composite'], reverse=True)
    for r in rows:
        r['composite_pct'] = round(float(r['composite']), 1)  # for the grade-over-time chart
        del r['composite']

    # Heatmap the objective chips across the squad (relative ranking, like the
    # stat tables) — hill time + each objective stat, higher = better.
    if rows:
        _hill_vals = [r.get('hill_pg_secs', 0) for r in rows]
        for r in rows:
            r['hill_heat'] = get_heatmap_class(r.get('hill_pg_secs', 0), _hill_vals, True)
        for oc in obj_columns:
            k = oc['key']
            _ov = [(r.get('objs', {}).get(k) or {}).get('val', 0) for r in rows]
            for r in rows:
                cell = r.get('objs', {}).get(k)
                if cell is not None:
                    cell['heat'] = get_heatmap_class(cell.get('val', 0), _ov, True)

    return {
        'rows': rows,
        'baselines': baselines,
        'session_date': session_date_str,
        'game_count': game_count,
        'players_in_session': session_players,
        'kind': session_kind,
        'squad_games': squad_games_in_session,
        'sid': session_sid,
        'session_ts': session_ts,
        'hill_games': hill_games,
        'has_hill': hill_games > 0 and any(r.get('hill_pg_secs', 0) > 0 for r in rows),
        'obj_columns': obj_columns,
        'stack_records': stack_records,
        'solo_records': solo_records,
        'session_rank': session_rank,
        'enemy_summary': _rc_enemy_summary(session_df),
        'weight_info': [{'label': _RC_WEIGHT_LABEL[k], 'w': round(w * 100)}
                        for k, w in sorted(W.items(), key=lambda kv: -kv[1])],
    }


def build_player_solo_cards(df: pd.DataFrame) -> list[dict]:
    """
    For every tracked player, summarise their most recent SOLO session (matches
    where they were the only tracked player, gap-clustered). Returns a compact
    row per player, sorted most-recent solo session first.
    """
    cards: list[dict] = []
    if df.empty or 'player_gamertag' not in df.columns:
        return cards
    rdf = _ranked_only(df)
    players = unique_sorted(rdf['player_gamertag']) if not rdf.empty else []
    # One memo for the whole loop: the ranked pool + solo baseline arrays are
    # identical for every player, so only the first card pays for them.
    shared: dict = {}
    for p in players:
        card = build_squad_report_card(df, player=p, _shared=shared)
        rows = card.get('rows') or []
        if not rows:
            continue
        row = rows[0]
        games = card.get('game_count', 0)
        wins = row.get('wins', 0)
        losses = max(games - wins, 0)
        cards.append({
            'player': p,
            'css': get_player_class(p),
            'grade': row['grade'],
            'grade_class': row['grade_class'],
            'grade_tip': row['grade_tip'],
            'session_date': card.get('session_date', ''),
            'session_ts': card.get('session_ts', 0.0),
            'sid': card.get('sid', ''),
            'games': games,
            'record': f"{wins}-{losses}",
            'win_pct': row['win_pct'],
            'kda': row['kda'],
            'kda_heat': row.get('kda_heat', ''),
            'obj_score': row.get('obj_score', '—'),
            'obj_score_heat': row.get('obj_score_heat', row.get('obj_heat', '')),
            'kills': row['kills'],
            'deaths': row['deaths'],
            'assists': row['assists'],
            'perfect_pg': row['perfect_pg'],
            'perfect_total': row['perfect_total'],
            'hill_games': row['hill_games'],
            'hill_pg': row['hill_pg'],
            'hill_pg_secs': row['hill_pg_secs'],
            'hill_total': row['hill_total'],
            'csr_delta': row['csr_delta'],
            'opp_mmr': (card.get('enemy_summary') or {}).get('enemy_mmr'),
            'opp_gap': (card.get('enemy_summary') or {}).get('mmr_gap'),
        })
    cards.sort(key=lambda c: c['session_ts'], reverse=True)
    return cards


def build_recent_solo_strip(df: pd.DataFrame) -> list[dict]:
    """Squad-dash cross-flag: one line per player whose latest SOLO session
    is newer than the last squad night (inside a 48h freshness window), so a
    fresh solo grind is visible without opening the Solo Dash. Returns [] on
    days nobody soloed — the strip simply doesn't render."""
    try:
        cards = build_player_solo_cards(df)
    except Exception as exc:
        logger.warning('recent solo strip failed: %s', exc)
        return []
    if not cards:
        return []
    squad_ts = 0.0
    try:
        rdf = _ranked_only(df)
        if not rdf.empty and 'match_id' in rdf.columns and 'date' in rdf.columns:
            work = rdf.copy()
            ensure_datetime(work)
            work = work.dropna(subset=['date'])
            ppm = work.groupby('match_id')['player_gamertag'].nunique()
            squad_ids = set(ppm[ppm >= 2].index)
            if squad_ids:
                squad_ts = float(pd.Timestamp(
                    work[work['match_id'].isin(squad_ids)]['date'].max()).timestamp())
    except Exception:
        squad_ts = 0.0
    floor_ts = max(squad_ts, time.time() - 48 * 3600)
    strip = []
    for c in cards:
        ts = float(c.get('session_ts') or 0.0)
        if ts <= floor_ts:
            continue
        strip.append({
            'player': c['player'], 'css': c['css'],
            'grade': c['grade'], 'grade_class': c['grade_class'],
            'record': c['record'], 'games': c['games'],
            'csr_delta': c['csr_delta'], 'session_date': c['session_date'],
            'sid': c['sid'],
        })
    return strip



def build_solo_all_table(df: pd.DataFrame) -> dict:
    """
    The /solo Full Stat Table: ONE row per tracked player = that player's
    LATEST SOLO session (the sessions differ in date, so each row carries
    session_date/session_sid/session_games and the macro adds a Session
    column). Rows are the full report-card rows — same fields and the same
    vs-365d-solo-history data-pct tint as the squad table. obj_columns is the
    UNION across the per-player cards (deduped by key); the macro guards
    row.objs lookups with .get() since a given row may lack union columns.
    """
    out = {'rows': [], 'obj_columns': [], 'has_hill': False, 'kind': 'solo',
           'session_date': '', 'game_count': 0, 'sid': ''}
    if df.empty or 'player_gamertag' not in df.columns:
        return out
    rdf = _ranked_only(df)
    players = unique_sorted(rdf['player_gamertag']) if not rdf.empty else []
    # Same memo pattern as build_player_solo_cards: the solo history pool /
    # arrays / weights are identical for every player, so only the first
    # card build pays for them.
    shared: dict = {}
    rows: list[dict] = []
    obj_columns: list[dict] = []
    seen_obj_keys: set = set()
    has_hill = False
    for p in players:
        card = build_squad_report_card(df, player=p, _shared=shared)
        crows = card.get('rows') or []
        if not crows:
            continue
        row = dict(crows[0])
        row['session_date'] = card.get('session_date', '')
        row['session_sid'] = card.get('sid', '')
        row['session_games'] = card.get('game_count', 0)
        rows.append(row)
        for oc in card.get('obj_columns') or []:
            if oc.get('key') not in seen_obj_keys:
                seen_obj_keys.add(oc.get('key'))
                obj_columns.append(oc)
        has_hill = has_hill or (bool(card.get('has_hill'))
                                and row.get('hill_pg_secs', 0) > 0)
    rows.sort(key=lambda r: r.get('composite_pct', 0) or 0, reverse=True)
    out.update(rows=rows, obj_columns=obj_columns, has_hill=has_hill,
               game_count=sum(r.get('session_games', 0) or 0 for r in rows))
    return out


def build_session_list(df: pd.DataFrame, mode: str = 'squad', limit: int = 40) -> list[dict]:
    """Recent sessions (gap-clustered) for the session-browser picker. Each entry
    carries the anchor match_id (sid) so /report (mode='squad') or /solo
    (mode='solo') can render that session's card. mode='all' = no stack filter."""
    if df.empty or 'match_id' not in df.columns or 'player_gamertag' not in df.columns:
        return []
    ranked = _ranked_only(df)
    if ranked.empty or 'date' not in ranked.columns:
        return []
    ensure_datetime(ranked)
    ranked = ranked.dropna(subset=['date'])
    if ranked.empty:
        return []
    ppm = ranked.groupby('match_id')['player_gamertag'].nunique()
    if mode == 'squad':
        ids = set(ppm[ppm >= 2].index)
        pool = ranked[ranked['match_id'].isin(ids)]
    elif mode == 'solo':
        # Solo Dash browser: matches where exactly ONE tracked player played
        # (same pool rule as build_squad_report_card(mode='solo')).
        ids = set(ppm[ppm == 1].index)
        pool = ranked[ranked['match_id'].isin(ids)]
    else:
        pool = ranked
    if pool.empty:
        return []
    mt = pool.groupby('match_id')['date'].max().sort_values(ascending=False)
    gap = pd.Timedelta(minutes=SESSION_GAP_MINUTES)
    clusters, cluster, prev = [], [], None
    for mid, t in mt.items():
        if prev is not None and (prev - t) > gap:
            clusters.append(cluster)
            cluster = []
        cluster.append((mid, t))
        prev = t
    if cluster:
        clusters.append(cluster)
    out = []
    for cl in clusters[:limit]:
        cids = [m for m, _ in cl]
        latest = max(t for _, t in cl)
        anchor = cids[0]  # mt is desc, so the first is the session's latest match
        sdf = pool[pool['match_id'].isin(cids)]
        w = l = 0
        for _mid, g in sdf.groupby('match_id'):
            o = g['outcome'].astype(str).str.lower() if 'outcome' in g.columns else pd.Series(dtype=str)
            ow = o[o.isin(['win', 'loss', 'lose', 'loose'])]
            if ow.empty:
                continue
            if ow.iloc[0] == 'win':
                w += 1
            else:
                l += 1
        players = sorted(sdf['player_gamertag'].astype(str).unique())
        _dec = w + l
        # Session MVP = best average KDA (kills + assists/3 − deaths) that night.
        _mvp = ''
        try:
            _k = numeric_series(sdf, 'kills') + numeric_series(sdf, 'assists') / 3 - numeric_series(sdf, 'deaths')
            _kd = _k.groupby(sdf['player_gamertag']).mean()  # vectorized (no per-group .apply)
            if not _kd.empty:
                _mvp = str(_kd.idxmax())
        except Exception:
            _mvp = ''
        out.append({
            'sid': str(anchor),
            'date': format_date(latest),
            'ts': float(latest.timestamp()) if pd.notna(latest) else 0.0,
            'games': len(cids),
            'record': f"{w}-{l}",
            'win_pct': int(round(w / _dec * 100)) if _dec else None,  # for dropdown heatmap
            'mvp': _mvp,
            'players': players,
            'players_str': ', '.join(players),
            'kind': 'squad' if len(players) >= 2 else 'solo',
        })
    return out


def _grade_emoji_square(letter: str) -> str:
    """Grade letter → colored square for emoji sparklines in copy-for-text."""
    base = (letter or '').rstrip('+-')
    return {'S': '🟩', 'A': '🟩', 'B': '🟨', 'C': '🟧', 'D': '🟥', 'F': '🟥'}.get(base, '⬜')


def build_grade_timeline(df: pd.DataFrame, mode: str = 'squad', max_sessions: int = 12) -> dict:
    """Per-player composite report-card grade for each of the last N sessions.
    Computes the 365-day baseline ONCE and grades each session directly with the
    module-level _rc_* helpers (same model as the cards) — ~12x faster than the
    old approach of rebuilding a full report card per session."""
    if df is None or df.empty or not {'match_id', 'player_gamertag', 'outcome'} <= set(df.columns):
        return {}
    ranked = _ranked_only(df)
    if ranked.empty or 'date' not in ranked.columns:
        return {}
    ensure_datetime(ranked)
    ranked = ranked.dropna(subset=['date'])
    if ranked.empty:
        return {}
    # Baseline arrays built ONCE (365d), reused for every session below.
    cutoff = pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=365)
    hist = ranked[ranked['date'] >= cutoff]
    if hist.empty:
        hist = ranked
    A = _rc_build_arrays(hist)
    _W = _rc_win_weights(hist)
    ppm = ranked.groupby('match_id')['player_gamertag'].nunique()
    if mode == 'squad':
        pool = ranked[ranked['match_id'].isin(set(ppm[ppm >= 2].index))]
    elif mode == 'solo':
        pool = ranked[ranked['match_id'].isin(set(ppm[ppm == 1].index))]
    else:
        pool = ranked
    if pool.empty:
        return {}
    mt = pool.groupby('match_id')['date'].max().sort_values(ascending=False)
    gap = pd.Timedelta(minutes=SESSION_GAP_MINUTES)
    clusters, cluster, prev = [], [], None
    for mid, t in mt.items():
        if prev is not None and (prev - t) > gap:
            clusters.append(cluster); cluster = []
        cluster.append(mid); prev = t
    if cluster:
        clusters.append(cluster)
    clusters = list(reversed(clusters[:max_sessions]))  # oldest → newest

    def _composite(st):
        return _rc_composite(st, A, _W)

    per_player: dict = {}
    labels = []
    for si, cids in enumerate(clusters):
        sdf = pool[pool['match_id'].isin(set(cids))]
        labels.append(format_date(sdf['date'].max()))
        for name, pf in sdf.groupby('player_gamertag'):
            st = _rc_session_stats(pf)
            if not st:
                continue
            comp = _composite(st)
            per_player.setdefault(name, {})[si] = {'grade': _rc_grade_pct(comp), 'pct': round(comp, 1)}
    if not per_player:
        return {}
    n = len(clusters)
    players_out = []
    for name, pts in per_player.items():
        series = [pts.get(si) for si in range(n)]
        present = [(si, p) for si, p in enumerate(series) if p]
        if len(present) < 2:
            continue  # need at least two graded sessions to draw a trend
        emoji = ''.join(_grade_emoji_square(p['grade']) for _, p in present)
        first_g = present[0][1]['grade']
        last_g = present[-1][1]['grade']
        players_out.append({
            'player': name,
            'cls': get_player_class(name),
            'points': [{'pct': p['pct'], 'grade': p['grade']} if p else None for p in series],
            'emoji': emoji,
            'first_grade': first_g,
            'last_grade': last_g,
            'trend': f"{first_g} → {last_g}",
            'trend_pct': f"{present[0][1]['pct']:.0f} → {present[-1][1]['pct']:.0f}",
        })
    if not players_out:
        return {}
    players_out.sort(key=lambda p: p['player'].lower())
    return {'labels': labels, 'players': players_out, 'count': n}


# ── Report-card grading engine (single source of truth) ────────────────────
# THE grading helpers. build_squad_report_card aliases these locally, and
# regrade_session (/api/regrade) + build_grade_timeline call them directly —
# so "all games included" always reproduces the card's grade exactly, by
# construction rather than by keeping two copies in sync.
def _rc_grade_pct(pct: float) -> str:
    """Percentile → letter grade.
      S ≥90th · A+ ≥83 · A ≥75 · A- ≥67 · B+ ≥58 · B ≥50 (average) · B- ≥42
      C+ ≥33 · C ≥25 · C- ≥17 · D+ ≥10 · D ≥6 · D- ≥3 · F below."""
    if pct >= 90: return 'S'
    if pct >= 83: return 'A+'
    if pct >= 75: return 'A'
    if pct >= 67: return 'A-'
    if pct >= 58: return 'B+'
    if pct >= 50: return 'B'
    if pct >= 42: return 'B-'
    if pct >= 33: return 'C+'
    if pct >= 25: return 'C'
    if pct >= 17: return 'C-'
    if pct >= 10: return 'D+'
    if pct >= 6:  return 'D'
    if pct >= 3:  return 'D-'
    return 'F'


def _rc_grade_class(g: str) -> str:
    base = (g or '').rstrip('+-')
    return {'S': 'grade-s', 'A': 'grade-a', 'B': 'grade-b',
            'C': 'grade-c', 'D': 'grade-d', 'F': 'grade-f'}.get(base, '')


def _rc_enemy_summary(sdf: pd.DataFrame) -> dict:
    """Session-level opposition summary from the enemy_team_* match columns.

    The team/enemy columns repeat on every tracked player's row of a match, so
    collapse to one row per match first. Fields with no data (older matches
    stored NULL enemy columns) are simply omitted rather than zeroed."""
    if sdf is None or sdf.empty or 'match_id' not in sdf.columns:
        return {}
    g = sdf.groupby('match_id').first()

    def _avg(col):
        if col not in g.columns:
            return None
        s = pd.to_numeric(g[col], errors='coerce').dropna()
        return float(s.mean()) if len(s) else None

    ours_mmr, theirs_mmr = _avg('team_mmr'), _avg('enemy_team_mmr')
    ours_k, theirs_k = _avg('team_kills'), _avg('enemy_team_kills')
    ours_dmg, theirs_dmg = _avg('team_damage_dealt'), _avg('enemy_team_damage_dealt')
    theirs_acc = _avg('enemy_team_accuracy')
    out: dict = {'games': int(len(g))}
    parts = []
    if ours_mmr is not None and theirs_mmr is not None:
        gap = theirs_mmr - ours_mmr
        out.update({'team_mmr': round(ours_mmr), 'enemy_mmr': round(theirs_mmr),
                    'mmr_gap': round(gap)})
        tone = 'harder lobbies' if gap >= 15 else ('easier lobbies' if gap <= -15 else 'even lobbies')
        parts.append(f"lobbies rated {round(theirs_mmr)} vs our {round(ours_mmr)} ({tone})")
    if ours_k is not None and theirs_k is not None:
        out.update({'team_kills_pg': round(ours_k, 1), 'enemy_kills_pg': round(theirs_k, 1)})
        if round(ours_k) == round(theirs_k):
            parts.append(f"even on frags {ours_k:.0f}-{theirs_k:.0f}/g")
        elif ours_k > theirs_k:
            parts.append(f"outfragged them {ours_k:.0f}-{theirs_k:.0f}/g")
        else:
            parts.append(f"they outfragged us {theirs_k:.0f}-{ours_k:.0f}/g")
    if ours_dmg is not None and theirs_dmg is not None:
        diff = ours_dmg - theirs_dmg
        out['team_dmg_diff_pg'] = round(diff)
        parts.append(f"team dmg {'+' if diff >= 0 else ''}{round(diff)}/g")
    if theirs_acc is not None:
        out['enemy_acc'] = round(theirs_acc, 1)
        parts.append(f"they shot {theirs_acc:.0f}% acc")
    if not parts:
        return {}
    out['line'] = ' · '.join(parts)
    return out


def _rc_sorted_arr(series):
    import numpy as np
    s = series.dropna().values.astype(float)
    s = s[np.isfinite(s)]
    s.sort()
    return s


def _rc_build_arrays(frame: pd.DataFrame) -> dict:
    rk = numeric_series(frame, 'kills'); rd = numeric_series(frame, 'deaths'); ra = numeric_series(frame, 'assists')
    kda_vals = (rk + ra / 3 - rd).replace([float('inf'), float('-inf')], pd.NA)
    rdmgp = numeric_series(frame, 'damage_dealt'); rdmgm = numeric_series(frame, 'damage_taken')
    ddiff_vals = (rdmgp - rdmgm).replace([float('inf'), float('-inf')], pd.NA)
    life_vals = numeric_series(frame, 'average_life_duration')
    obj_vals = objective_score_series(frame)
    ps = score_series(frame)
    ts = pd.to_numeric(frame.get('team_personal_score', pd.Series(dtype=float)), errors='coerce').fillna(0)
    scorepct_vals = (ps / ts.where(ts > 0) * 100).replace([float('inf'), float('-inf')], pd.NA)
    rdur = numeric_series(frame, 'duration')
    dmgmin_vals = rdmgp.where(rdur > 0) / (rdur / 60).replace(0, pd.NA)
    kdamin_vals = numeric_series(frame, 'kda/min').replace([float('inf'), float('-inf')], pd.NA)
    objmin_vals = numeric_series(frame, 'obj/min').replace([float('inf'), float('-inf')], pd.NA)
    # Extra per-game distributions for the Full Stat Table's vs-365d-history
    # tint (kills/deaths/assists/KD/acc/dmg dealt/dmg taken/perfect medals).
    # Same pool as the graded stats, so every data-pct in that table speaks
    # the same language. Deaths/dmg-taken stay raw here — the INVERSION
    # (lower = better) happens where the row percentiles are computed.
    kd_vals = (rk / rd.where(rd > 0)).where(rd > 0, rk)  # deathless game → KD = kills
    fired = numeric_series(frame, 'shots_fired')
    hit = numeric_series(frame, 'shots_hit')
    acc_vals = (hit / fired.where(fired > 0) * 100).replace([float('inf'), float('-inf')], pd.NA)
    perfect_vals = numeric_series(frame, 'medal_perfect')
    return {
        'kda': _rc_sorted_arr(kda_vals),
        'ddiff': _rc_sorted_arr(ddiff_vals),
        'life': _rc_sorted_arr(life_vals[life_vals >= 5]),
        'obj': _rc_sorted_arr(obj_vals[obj_vals > 0]),
        'scorepct': _rc_sorted_arr(scorepct_vals),
        'dmgmin': _rc_sorted_arr(dmgmin_vals),
        'kdamin': _rc_sorted_arr(kdamin_vals),
        'objmin': _rc_sorted_arr(objmin_vals[objmin_vals > 0]),
        'kills': _rc_sorted_arr(rk),
        'deaths': _rc_sorted_arr(rd),
        'assists': _rc_sorted_arr(ra),
        'kd': _rc_sorted_arr(kd_vals),
        'acc': _rc_sorted_arr(acc_vals),
        'dmgplus': _rc_sorted_arr(rdmgp),
        'dmgminus': _rc_sorted_arr(rdmgm),
        'perfect': _rc_sorted_arr(perfect_vals),
    }


# ── Win-correlated composite weights ─────────────────────────────────────
# "Who's to say which stats really matter" — the wins do. The composite is
# weighted by the top 5 stats most correlated (per game, point-biserial) with
# actually WINNING over the squad's own history. Falls back to the legacy
# hand-tuned weights when history is too small to trust the correlations.
_RC_DEFAULT_WEIGHTS = {'kda': 0.30, 'ddiff': 0.27, 'life': 0.16,
                       'obj': 0.11, 'scorepct': 0.09, 'dmgmin': 0.07}
_RC_STAT_FIELD = {'kda': 'kda', 'ddiff': 'dmg_diff_pg', 'life': 'avg_life_pg',
                  'obj': 'obj_score_pg', 'scorepct': 'score_pct',
                  'dmgmin': 'dmg_per_min', 'kdamin': 'kda_min', 'objmin': 'obj_min'}
_RC_WEIGHT_LABEL = {'kda': 'KDA', 'ddiff': 'Dmg±', 'life': 'Life', 'obj': 'ObjSc',
                    'scorepct': 'Sc%', 'dmgmin': 'Dmg/m', 'kdamin': 'KDA/m', 'objmin': 'Obj/m'}


def _rc_win_weights(hist_df: pd.DataFrame) -> dict:
    import numpy as np
    try:
        if (hist_df is None or hist_df.empty or 'outcome' not in hist_df.columns
                or len(hist_df) < 200):
            return dict(_RC_DEFAULT_WEIGHTS)
        win = (hist_df['outcome'].astype(str).str.lower() == 'win').astype(float)
        rk = numeric_series(hist_df, 'kills'); rd = numeric_series(hist_df, 'deaths')
        ra = numeric_series(hist_df, 'assists')
        dmgp = numeric_series(hist_df, 'damage_dealt'); dmgt = numeric_series(hist_df, 'damage_taken')
        ps = score_series(hist_df)
        ts = pd.to_numeric(hist_df.get('team_personal_score', pd.Series(dtype=float)), errors='coerce').fillna(0)
        dur = numeric_series(hist_df, 'duration')
        series = {
            'kda': rk + ra / 3 - rd,
            'ddiff': dmgp - dmgt,
            'life': numeric_series(hist_df, 'average_life_duration'),
            'obj': objective_score_series(hist_df),
            'scorepct': ps / ts.where(ts > 0) * 100,
            'dmgmin': dmgp.where(dur > 0) / (dur / 60).replace(0, pd.NA),
            # kda/min deliberately excluded — redundant with KDA (Pat 2026-07-02)
            'objmin': numeric_series(hist_df, 'obj/min'),
        }
        corrs = {}
        for k, s in series.items():
            s = pd.to_numeric(s, errors='coerce').replace([float('inf'), float('-inf')], pd.NA)
            # objective stats only mean anything in objective modes
            mask = (s > 0) if k in ('obj', 'objmin') else s.notna()
            m = mask.fillna(False) & s.notna()
            if int(m.sum()) < 100:
                continue
            c = pd.Series(s[m].astype(float)).corr(win[m])
            if c is not None and np.isfinite(c) and c > 0:
                corrs[k] = float(c)
        top = sorted(corrs.items(), key=lambda kv: -kv[1])[:5]
        if len(top) < 3:
            return dict(_RC_DEFAULT_WEIGHTS)
        tot = sum(c for _, c in top)
        return {k: c / tot for k, c in top}
    except Exception:
        return dict(_RC_DEFAULT_WEIGHTS)


def _rc_composite(st: dict, A: dict, W: dict) -> float:
    """Weighted composite of a session-stats dict against arrays A.
    75% how you played (win-correlated stat percentiles) + 25% how it went
    (session win rate) — a 5-0 night should not grade like an 0-5 night."""
    base = sum(w * _rc_percentile(st.get(_RC_STAT_FIELD[k]) or 0, A[k])
               for k, w in W.items())
    games = st.get('games')
    if games:
        win_pct = st.get('wins', 0) / games * 100.0
        return 0.75 * base + 0.25 * win_pct
    return base


def _rc_heat(pct: float) -> str:
    if pct >= 80: return 'heat-excellent'
    if pct >= 60: return 'heat-good'
    if pct >= 40: return 'heat-average'
    if pct >= 20: return 'heat-below'
    return 'heat-poor'


def _rc_percentile(value, arr) -> float:
    import numpy as np
    if len(arr) == 0:
        return 50.0
    below = float(np.sum(arr < value)); equal = float(np.sum(arr == value))
    return (below + 0.5 * equal) / len(arr) * 100.0


def _rc_session_stats(frame: pd.DataFrame):
    games = len(frame)
    if games == 0:
        return None
    kills_pg = numeric_series(frame, 'kills').sum() / games
    deaths_pg = numeric_series(frame, 'deaths').sum() / games
    assists_pg = numeric_series(frame, 'assists').sum() / games
    kda = safe_kda(kills_pg, assists_pg, deaths_pg)
    dmg_p = numeric_series(frame, 'damage_dealt').sum(); dmg_m = numeric_series(frame, 'damage_taken').sum()
    dmg_diff_pg = (dmg_p - dmg_m) / games
    avg_life_pg = float(numeric_series(frame, 'average_life_duration').mean() or 0.0)
    obj_scores = objective_score_series(frame)
    _obj_games = int((obj_scores > 0).sum()) if not obj_scores.empty else 0
    obj_score_pg = float(obj_scores.sum()) / _obj_games if _obj_games else 0.0
    team_score = pd.to_numeric(frame.get('team_personal_score', 0), errors='coerce').fillna(0).sum()
    total_score = score_series(frame).sum()
    score_pct = float(total_score / team_score * 100) if team_score > 0 else 0.0
    total_dur = numeric_series(frame, 'duration').sum()
    dmg_per_min = dmg_p / (total_dur / 60) if total_dur > 0 else 0.0
    fired = numeric_series(frame, 'shots_fired').sum(); hit = numeric_series(frame, 'shots_hit').sum()
    accuracy = hit / fired * 100 if fired > 0 else 0.0
    outcomes = frame['outcome'].astype(str).str.lower() if 'outcome' in frame.columns else pd.Series(dtype=str)
    wins = int((outcomes == 'win').sum()) if not outcomes.empty else 0
    return {'kda': kda, 'dmg_diff_pg': dmg_diff_pg, 'avg_life_pg': avg_life_pg,
            'obj_score_pg': obj_score_pg, 'score_pct': score_pct, 'dmg_per_min': dmg_per_min,
            'kda_min': kda_per_min(frame), 'obj_min': obj_per_min(frame),
            'kills_pg': kills_pg, 'deaths_pg': deaths_pg, 'assists_pg': assists_pg,
            'dmg_plus_pg': dmg_p / games, 'dmg_minus_pg': dmg_m / games, 'accuracy': accuracy,
            'games': games, 'wins': wins}


def regrade_session(df: pd.DataFrame, player: str, match_ids: list, mode: str = 'squad') -> dict:
    """Recompute a player's session composite grade over just `match_ids`."""
    if df is None or df.empty or not player or not match_ids or 'player_gamertag' not in df.columns:
        return {'ok': False}
    ranked = _ranked_only(df)
    if ranked.empty or 'date' not in ranked.columns:
        return {'ok': False}
    ensure_datetime(ranked)
    ranked = ranked.dropna(subset=['date'])
    cutoff = pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=365)
    hist = ranked[ranked['date'] >= cutoff]
    if hist.empty:
        hist = ranked
    A = _rc_build_arrays(hist)
    if mode == 'self':
        pf = hist[hist['player_gamertag'] == player]
        if len(pf) >= 20:
            A = _rc_build_arrays(pf)
    wanted = {str(m) for m in match_ids}
    frame = ranked[(ranked['player_gamertag'] == player) & (ranked['match_id'].astype(str).isin(wanted))]
    stats = _rc_session_stats(frame)
    if not stats:
        return {'ok': False}
    pk = _rc_percentile(stats['kda'], A['kda'])
    pdd = _rc_percentile(stats['dmg_diff_pg'], A['ddiff'])
    pl = _rc_percentile(stats['avg_life_pg'], A['life'])
    po = _rc_percentile(stats['obj_score_pg'], A['obj'])
    psc = _rc_percentile(stats['score_pct'], A['scorepct'])
    pm = _rc_percentile(stats['dmg_per_min'], A['dmgmin'])
    pkm = _rc_percentile(stats['kda_min'], A['kdamin'])
    pom = _rc_percentile(stats['obj_min'], A['objmin'])
    comp = _rc_composite(stats, A, _rc_win_weights(hist))
    letter = _rc_grade_pct(comp)
    games = stats['games']; wins = stats['wins']
    win_pct = wins / games * 100 if games else 0.0

    def cell(pct, value, blank_when_zero=False, raw=0.0):
        # A stat cell mirroring the card: grade letter/class, heat, value string.
        if blank_when_zero and raw <= 0:
            return {'val': _obj_dash(raw), 'grade': '', 'grade_class': '', 'heat': _rc_heat(pct)}
        return {'val': value, 'grade': _rc_grade_pct(pct), 'grade_class': _rc_grade_class(_rc_grade_pct(pct)), 'heat': _rc_heat(pct)}

    stat_cells = {
        'kda':     cell(pk,  f"{stats['kda']:.2f}"),
        'kda_min': cell(pkm, f"{stats['kda_min']:.2f}"),
        'obj_min': cell(pom, _obj_dash(stats['obj_min']), blank_when_zero=True, raw=stats['obj_min']),
        'ddiff':   cell(pdd, format_signed(stats['dmg_diff_pg'], 0)),
        'life':    cell(pl,  f"{stats['avg_life_pg']:.1f}s"),
        'obj':     cell(po,  _obj_dash(stats['obj_score_pg']), blank_when_zero=True, raw=stats['obj_score_pg']),
        'score':   cell(psc, f"{stats['score_pct']:.1f}%"),
        'dmgm':    cell(pm,  f"{stats['dmg_per_min']:.0f}"),
    }
    return {
        'ok': True, 'grade': letter, 'grade_class': _rc_grade_class(letter),
        'composite_pct': round(comp, 1), 'games': games, 'wins': wins,
        'win_pct': f"{win_pct:.0f}%",
        'record': f"{wins}W / {games}G",
        'stats': stat_cells,
        'extra': {
            'kills': f"{stats['kills_pg']:.1f}", 'deaths': f"{stats['deaths_pg']:.1f}",
            'assists': f"{stats['assists_pg']:.1f}", 'accuracy': f"{stats['accuracy']:.1f}%",
        },
    }


@app.route('/api/regrade')
def api_regrade():
    df = cache.get()
    player = request.args.get('player', '')
    mode = 'self' if request.args.get('mode') == 'self' else 'squad'
    mids = [m for m in request.args.get('games', '').split(',') if m]
    return jsonify(regrade_session(df, player, mids, mode))


def build_outlier_spotlight(df: pd.DataFrame, range_key: str | None = None) -> list[dict]:
    """Build spotlight highlighting player outliers vs the group."""
    if df.empty or 'player_gamertag' not in df.columns:
        return []
    
    ranked_df = _ranked_only(df)
    range_key = (range_key or 'all').lower()
    if range_key == 'lifetime':
        range_key = 'all'
    if ranked_df.empty:
        return []

    if range_key != 'all':
        if 'date' not in ranked_df.columns:
            return []
        ranked_df = ranked_df.copy()
        ensure_datetime(ranked_df)
        ranked_df = ranked_df.dropna(subset=['date'])
        ranked_df = apply_trend_range(ranked_df, range_key)
        if ranked_df.empty:
            return []
    
    player_stats = []
    for player in unique_sorted(ranked_df['player_gamertag']):
        player_df = ranked_df[ranked_df['player_gamertag'] == player]
        if player_df.empty:
            continue
        
        games = len(player_df)
        if games == 0:
            continue
        
        total_kills = numeric_series(player_df, 'kills').sum()
        total_deaths = numeric_series(player_df, 'deaths').sum()
        total_assists = numeric_series(player_df, 'assists').sum()
        
        kills_pg = total_kills / games
        deaths_pg = total_deaths / games
        assists_pg = total_assists / games
        kda = safe_kda(kills_pg, assists_pg, deaths_pg)
        
        fired = numeric_series(player_df, 'shots_fired').sum()
        hit = numeric_series(player_df, 'shots_hit').sum()
        if fired > 0:
            accuracy = hit / fired * 100
        else:
            accuracy = numeric_series(player_df, 'accuracy').mean()
            if accuracy <= 1:
                accuracy *= 100
        
        total_dmg_dealt = numeric_series(player_df, 'damage_dealt').sum()
        total_dmg_taken = numeric_series(player_df, 'damage_taken').sum()
        dmg_diff_pg = (total_dmg_dealt - total_dmg_taken) / games
        
        total_duration = numeric_series(player_df, 'duration').sum()
        dmg_per_min = total_dmg_dealt / (total_duration / 60) if total_duration > 0 else 0
        
        score_total = score_series(player_df).sum()
        score_pg = score_total / games
        
        obj_scores = objective_score_series(player_df)
        obj_score_pg = obj_scores.sum() / games if not obj_scores.empty else 0
        
        outcomes = player_df['outcome'].astype(str).str.lower() if 'outcome' in player_df.columns else pd.Series()
        wins = (outcomes == 'win').sum() if not outcomes.empty else 0
        win_rate = wins / games * 100
        
        player_stats.append({
            'player': player,
            'games': games,
            'win_rate': win_rate,
            'kda': kda,
            'kills_pg': kills_pg,
            'deaths_pg': deaths_pg,
            'assists_pg': assists_pg,
            'accuracy': accuracy,
            'dmg_per_min': dmg_per_min,
            'dmg_diff_pg': dmg_diff_pg,
            'score_pg': score_pg,
            'obj_score_pg': obj_score_pg
        })
    
    if not player_stats:
        return []
    
    stats_info = [
        {'key': 'win_rate', 'label': 'Win Rate', 'higher_better': True, 'format': lambda v: f'{v:.1f}%'},
        {'key': 'kda', 'label': 'KDA', 'higher_better': True, 'format': lambda v: f'{v:.2f}'},
        {'key': 'kills_pg', 'label': 'Kills/Game', 'higher_better': True, 'format': lambda v: f'{v:.1f}'},
        {'key': 'deaths_pg', 'label': 'Deaths/Game', 'higher_better': False, 'format': lambda v: f'{v:.1f}'},
        {'key': 'assists_pg', 'label': 'Assists/Game', 'higher_better': True, 'format': lambda v: f'{v:.1f}'},
        {'key': 'accuracy', 'label': 'Accuracy', 'higher_better': True, 'format': lambda v: f'{v:.1f}%'},
        {'key': 'dmg_per_min', 'label': 'Damage/Min', 'higher_better': True, 'format': lambda v: f'{v:.0f}'},
        {'key': 'dmg_diff_pg', 'label': 'Damage Diff/Game', 'higher_better': True, 'format': lambda v: format_signed(v, 0)},
        {'key': 'score_pg', 'label': 'Score/Game', 'higher_better': True, 'format': lambda v: f'{v:.0f}'},
        {'key': 'obj_score_pg', 'label': 'Obj Score/Game', 'higher_better': True, 'format': lambda v: f'{v:.1f}'}
    ]
    
    stat_means = {}
    stat_stds = {}
    stat_values = {}
    for stat in stats_info:
        values = [row[stat['key']] for row in player_stats]
        series = pd.Series(values, dtype=float)
        stat_means[stat['key']] = series.mean()
        stat_stds[stat['key']] = series.std(ddof=0)
        stat_values[stat['key']] = values
    
    positive_vibes = ['On Fire', 'Heat Check', 'Hot Hand', 'Glow Up', 'Pop Off']
    negative_vibes = ['Cold Snap', 'Slump', 'Ice Bath', 'Rough Patch', 'Frost Bite']
    
    rows = []
    for row in player_stats:
        candidates = []
        for stat in stats_info:
            value = row[stat['key']]
            std = stat_stds.get(stat['key'], 0)
            if std == 0 or pd.isna(std):
                continue
            mean = stat_means.get(stat['key'], 0)
            z = (value - mean) / std
            adj = z if stat['higher_better'] else -z
            values = stat_values.get(stat['key'], [])
            if len(values) > 1:
                others_mean = (sum(values) - value) / (len(values) - 1)
            else:
                others_mean = mean
            if abs(others_mean) < 1e-6:
                diff_pct = 0.0 if abs(value) < 1e-6 else 100.0
            else:
                diff_pct = (value - others_mean) / abs(others_mean) * 100
            candidates.append({
                'stat': stat,
                'value': value,
                'adj': adj,
                'diff_pct': diff_pct
            })
        
        good = sorted([c for c in candidates if c['adj'] > 0], key=lambda c: c['adj'], reverse=True)
        bad  = sorted([c for c in candidates if c['adj'] < 0], key=lambda c: c['adj'])

        picks = good[:3] + bad[:3]

        highlights = []
        for entry in picks:
            stat = entry['stat']
            diff_pct = entry['diff_pct']
            sign = '+' if diff_pct >= 0 else ''
            value = stat['format'](entry['value'])
            advantage = entry['adj'] > 0
            emoji = '🔥' if advantage else '🥶'
            vibe_list = positive_vibes if advantage else negative_vibes
            vibe = vibe_list[abs(hash(stat['key'])) % len(vibe_list)]
            highlights.append({
                'text': f"{emoji} {stat['label']} {value} ({sign}{diff_pct:.0f}% vs pack)",
                'good': advantage,
            })

        rows.append({'player': row['player'], 'highlights': highlights})
    
    return rows


def build_ranked_arena_summary(df: pd.DataFrame) -> list:
    """Build summary for each player's last ranked session."""
    if df.empty:
        return []
    
    ranked_df = _ranked_only(df)
    
    if 'date' not in ranked_df.columns:
        return []
    
    ensure_datetime(ranked_df)
    ranked_df = ranked_df.dropna(subset=['date']).sort_values('date', ascending=False)
    
    if ranked_df.empty:
        return []
    
    rows = []
    for player in unique_sorted(ranked_df['player_gamertag']):
        player_df = ranked_df[ranked_df['player_gamertag'] == player].sort_values('date', ascending=False)
        if player_df.empty:
            continue
        
        # Find session (30 min gaps)
        session_matches = []
        prev_time = None
        
        for idx, row_data in player_df.iterrows():
            match_time = row_data['date']
            if prev_time is None:
                session_matches.append(idx)
                prev_time = match_time
                continue
            
            time_diff = (prev_time - match_time).total_seconds() / 60
            if time_diff <= SESSION_GAP_MINUTES:
                session_matches.append(idx)
                prev_time = match_time
            else:
                break
        
        session_df = player_df.loc[session_matches]
        if session_df.empty:
            continue
        
        games = len(session_df)
        outcomes = session_df['outcome'].astype(str).str.lower() if 'outcome' in session_df.columns else pd.Series()
        wins = (outcomes == 'win').sum() if not outcomes.empty else 0
        win_pct = wins / games * 100 if games > 0 else 0
        
        # Basic stats
        total_kills = pd.to_numeric(session_df.get('kills', 0), errors='coerce').fillna(0).sum()
        total_deaths = pd.to_numeric(session_df.get('deaths', 0), errors='coerce').fillna(0).sum()
        total_assists = pd.to_numeric(session_df.get('assists', 0), errors='coerce').fillna(0).sum()
        kills = total_kills / games if games else 0
        deaths = total_deaths / games if games else 0
        assists = total_assists / games if games else 0
        kda = safe_kda(kills, assists, deaths)
        kd1 = kills / deaths if deaths > 0 else kills
        kd2 = (kills + assists) / deaths if deaths > 0 else kills + assists
        
        # Damage stats
        total_dmg_dealt = pd.to_numeric(session_df.get('damage_dealt', 0), errors='coerce').fillna(0).sum()
        total_dmg_taken = pd.to_numeric(session_df.get('damage_taken', 0), errors='coerce').fillna(0).sum()
        dmg_plus = total_dmg_dealt / games if games else 0
        dmg_minus = total_dmg_taken / games if games else 0
        dmg_diff = total_dmg_dealt - total_dmg_taken
        dmg_per_ka = total_dmg_dealt / (total_kills + total_assists) if (total_kills + total_assists) > 0 else 0
        dmg_per_death = total_dmg_dealt / total_deaths if total_deaths > 0 else total_dmg_dealt
        
        # Duration and dmg/min
        total_duration = pd.to_numeric(session_df.get('duration', 0), errors='coerce').fillna(0).sum()
        dmg_per_min = total_dmg_dealt / (total_duration / 60) if total_duration > 0 else 0
        
        # Team damage percentage
        team_dmg = pd.to_numeric(session_df.get('team_damage_dealt', 0), errors='coerce').fillna(0).sum()
        enemy_dmg = pd.to_numeric(session_df.get('enemy_team_damage_dealt', 0), errors='coerce').fillna(0).sum()
        dmg_pct_plus = (total_dmg_dealt / team_dmg * 100) if team_dmg > 0 else 0
        dmg_pct_minus = (total_dmg_dealt / enemy_dmg * 100) if enemy_dmg > 0 else 0
        
        # Accuracy
        fired = pd.to_numeric(session_df.get('shots_fired', 0), errors='coerce').fillna(0).sum()
        hit = pd.to_numeric(session_df.get('shots_hit', 0), errors='coerce').fillna(0).sum()
        accuracy = hit / fired * 100 if fired > 0 else 0
        
        # Score
        total_score = score_series(session_df).sum()
        score = total_score / games if games else 0
        team_score = pd.to_numeric(session_df.get('team_personal_score', 0), errors='coerce').fillna(0).sum()
        score_pct = (total_score / team_score * 100) if team_score > 0 else 0
        obj_scores = objective_score_series(session_df)
        obj_score = obj_scores.sum() / games if games else 0
        
        # Medals and misc
        total_medals = pd.to_numeric(session_df.get('medal_count', 0), errors='coerce').fillna(0).sum()
        medals = total_medals / games if games else 0
        avg_life = pd.to_numeric(session_df.get('average_life_duration', 0), errors='coerce').fillna(0).mean()
        callouts = pd.to_numeric(session_df.get('callout_assists', 0), errors='coerce').fillna(0).sum() / games if games else 0
        
        # CSR — session_df is descending; sort ascending so first=oldest, last=newest
        session_asc = session_df.sort_values('date', ascending=True)
        latest_csr = None
        pre_csr = None
        post_csr = None
        if 'post_match_csr' in session_asc.columns:
            post_vals = pd.to_numeric(session_asc['post_match_csr'], errors='coerce')
            post_vals = post_vals[post_vals > 0]
            if not post_vals.empty:
                # Last game of the session = current CSR
                latest_csr = post_vals.iloc[-1]
                post_csr = post_vals.iloc[-1]

        if 'pre_match_csr' in session_asc.columns:
            pre_vals = pd.to_numeric(session_asc['pre_match_csr'], errors='coerce')
            pre_vals = pre_vals[pre_vals > 0]
            if not pre_vals.empty:
                # First game of the session = starting CSR
                pre_csr = pre_vals.iloc[0]

        csr_delta = None
        if pre_csr is not None and post_csr is not None:
            csr_delta = float(post_csr) - float(pre_csr)
        
        rows.append({
            'player': player,
            'session_date': format_date(session_df['date'].max()),
            'csr': format_float(latest_csr, 1) if latest_csr else '-',
            'games': format_int(games),
            'win_pct': format_float(win_pct, 1),
            'kills': format_float(kills, 1),
            'deaths': format_float(deaths, 1),
            'assists': format_float(assists, 1),
            'kd1': format_float(kd1, 2),
            'kd2': format_float(kd2, 2),
            'kda': format_float(kda, 2),
            'dmg_plus': format_float(dmg_plus, 0),
            'dmg_minus': format_float(dmg_minus, 0),
            'dmg_diff': format_signed(dmg_diff, 0),
            'dmg_per_ka': format_float(dmg_per_ka, 0),
            'dmg_per_death': format_float(dmg_per_death, 0),
            'dmg_per_min': format_float(dmg_per_min, 0),
            'dmg_pct_plus': format_float(dmg_pct_plus, 1),
            'dmg_pct_minus': format_float(dmg_pct_minus, 1),
            'fired': format_int(fired),
            'landed': format_int(hit),
            'accuracy': format_float(accuracy, 1),
            'score': format_float(score, 0),
            'obj_score': _obj_dash(obj_score, 1),
            'score_pct': format_float(score_pct, 1),
            'medals': format_float(medals, 1),
            'avg_life': format_float(avg_life, 1),
            'callouts': format_float(callouts, 1),
            'pre_csr': format_int(pre_csr) if pre_csr else '-',
            'post_csr': format_int(post_csr) if post_csr else '-',
            'csr_delta': format_signed(csr_delta, 0) if csr_delta is not None else '-'
        })
    
    add_heatmap_classes(rows, {
        'csr': True, 'games': True, 'win_pct': True, 'kda': True, 'kd1': True, 'kd2': True,
        'kills': True, 'deaths': False, 'assists': True,
        'dmg_plus': True, 'dmg_minus': False, 'dmg_diff': True,
        'dmg_per_ka': True, 'dmg_per_death': True, 'dmg_per_min': True,
        'dmg_pct_plus': True, 'dmg_pct_minus': True,
        'fired': True, 'landed': True, 'accuracy': True,
        'score': True, 'obj_score': True, 'score_pct': True,
        'medals': True, 'avg_life': True, 'callouts': True, 'csr_delta': True
    })
    add_weighted_composite_grades(rows, REPORT_CARD_GRADE_WEIGHTS, 'Session grade')
    
    rows.sort(key=lambda x: to_number(x['kda']) or 0, reverse=True)
    return rows


def _build_ranked_arena_period(df: pd.DataFrame, days: int | None = None) -> list:
    """Build summary for ranked matches per player over a given period.
    
    Args:
        days: Number of days to look back. None means lifetime (no cutoff).
    """
    if df.empty:
        return []
    
    if days is not None and 'date' not in df.columns:
        return []
    
    ranked_df = _ranked_only(df)
    
    if ranked_df.empty:
        return []

    kda_baseline_values: list[float] = []
    obj_baseline_values: list[float] = []
    if 'date' in ranked_df.columns:
        baseline_df = ranked_df.copy()
        ensure_datetime(baseline_df)
        baseline_df = baseline_df.dropna(subset=['date'])
        if not baseline_df.empty:
            baseline_cutoff = baseline_df['date'].max() - pd.Timedelta(days=365)
            baseline_df = baseline_df[baseline_df['date'] >= baseline_cutoff]
            if not baseline_df.empty:
                kda_vals = (
                    numeric_series(baseline_df, 'kills')
                    + numeric_series(baseline_df, 'assists') / 3
                    - numeric_series(baseline_df, 'deaths')
                ).replace([float('inf'), float('-inf')], pd.NA).dropna()
                obj_vals = objective_score_series(baseline_df).replace([float('inf'), float('-inf')], pd.NA).dropna()
                kda_baseline_values = [float(v) for v in kda_vals.tolist()]
                obj_baseline_values = [float(v) for v in obj_vals[obj_vals > 0].tolist()]
    
    if days is not None:
        ranked_df = ranked_df.copy()
        ensure_datetime(ranked_df)
        ranked_df = ranked_df.dropna(subset=['date'])
        if ranked_df.empty:
            return []
        max_date = ranked_df['date'].max()
        cutoff_date = max_date - pd.Timedelta(days=days)
        ranked_df = ranked_df[ranked_df['date'] >= cutoff_date]
        if ranked_df.empty:
            return []
    
    rows = []
    for player in unique_sorted(ranked_df['player_gamertag']):
        player_df = ranked_df[ranked_df['player_gamertag'] == player]
        if player_df.empty:
            continue
        
        games = len(player_df)
        outcomes = player_df['outcome'].astype(str).str.lower() if 'outcome' in player_df.columns else pd.Series()
        wins = (outcomes == 'win').sum() if not outcomes.empty else 0
        
        stats = calculate_player_stats(player_df, games)
        
        latest_csr = None
        pre_csr = None
        post_csr = None
        if 'post_match_csr' in player_df.columns and 'date' in player_df.columns:
            sorted_df = player_df.sort_values('date', ascending=False)
            post_vals = pd.to_numeric(sorted_df['post_match_csr'], errors='coerce')
            post_vals = post_vals[post_vals > 0]
            if not post_vals.empty:
                latest_csr = post_vals.iloc[0]
                post_csr = post_vals.iloc[0]
        
        if days is not None and 'pre_match_csr' in player_df.columns and 'date' in player_df.columns:
            sorted_asc = player_df.sort_values('date')
            pre_vals = pd.to_numeric(sorted_asc['pre_match_csr'], errors='coerce')
            pre_vals = pre_vals[pre_vals > 0]
            if not pre_vals.empty:
                pre_csr = pre_vals.iloc[0]
        
        csr_delta = (post_csr - pre_csr) if pre_csr and post_csr else 0
        
        row = format_player_stats_row(player, games, wins, stats, latest_csr)
        row['kda_baseline_heat'] = get_heatmap_class(row.get('kda'), kda_baseline_values, True)
        row['obj_score_baseline_heat'] = get_heatmap_class(row.get('obj_score'), obj_baseline_values, True)
        if days is not None:
            row['pre_csr'] = format_int(pre_csr) if pre_csr else '-'
            row['post_csr'] = format_int(post_csr) if post_csr else '-'
            row['csr_delta'] = format_signed(csr_delta, 0)
        rows.append(row)
    
    add_heatmap_classes(rows, FULL_HEATMAP_CONFIG)
    add_weighted_composite_grades(rows, REPORT_CARD_GRADE_WEIGHTS, 'Ranked period grade')
    rows.sort(key=lambda x: to_number(x['kda']) or 0, reverse=True)
    return rows







def calculate_player_stats(player_df: pd.DataFrame, games: int) -> dict:
    """Calculate all stats for a player's matches. Returns a dict of stat values."""
    if games == 0:
        return {}
    
    # Basic stats
    total_kills = pd.to_numeric(player_df.get('kills', 0), errors='coerce').fillna(0).sum()
    total_deaths = pd.to_numeric(player_df.get('deaths', 0), errors='coerce').fillna(0).sum()
    total_assists = pd.to_numeric(player_df.get('assists', 0), errors='coerce').fillna(0).sum()
    kills = total_kills / games
    deaths = total_deaths / games
    assists = total_assists / games
    kda = safe_kda(kills, assists, deaths)
    kd1 = kills / deaths if deaths > 0 else kills
    kd2 = (kills + assists) / deaths if deaths > 0 else kills + assists
    
    # Damage stats
    total_dmg_dealt = pd.to_numeric(player_df.get('damage_dealt', 0), errors='coerce').fillna(0).sum()
    total_dmg_taken = pd.to_numeric(player_df.get('damage_taken', 0), errors='coerce').fillna(0).sum()
    dmg_plus = total_dmg_dealt / games
    dmg_minus = total_dmg_taken / games
    dmg_diff = total_dmg_dealt - total_dmg_taken
    dmg_per_ka = total_dmg_dealt / (total_kills + total_assists) if (total_kills + total_assists) > 0 else 0
    dmg_per_death = total_dmg_dealt / total_deaths if total_deaths > 0 else total_dmg_dealt
    
    # Duration and dmg/min
    total_duration = pd.to_numeric(player_df.get('duration', 0), errors='coerce').fillna(0).sum()
    dmg_per_min = total_dmg_dealt / (total_duration / 60) if total_duration > 0 else 0
    
    # Team damage percentage
    team_dmg = pd.to_numeric(player_df.get('team_damage_dealt', 0), errors='coerce').fillna(0).sum()
    enemy_dmg = pd.to_numeric(player_df.get('enemy_team_damage_dealt', 0), errors='coerce').fillna(0).sum()
    dmg_pct_plus = (total_dmg_dealt / team_dmg * 100) if team_dmg > 0 else 0
    dmg_pct_minus = (total_dmg_dealt / enemy_dmg * 100) if enemy_dmg > 0 else 0
    
    # Accuracy
    fired = pd.to_numeric(player_df.get('shots_fired', 0), errors='coerce').fillna(0).sum()
    hit = pd.to_numeric(player_df.get('shots_hit', 0), errors='coerce').fillna(0).sum()
    accuracy = hit / fired * 100 if fired > 0 else 0
    
    # Score
    total_score = score_series(player_df).sum()
    score = total_score / games
    team_score = pd.to_numeric(player_df.get('team_personal_score', 0), errors='coerce').fillna(0).sum()
    score_pct = (total_score / team_score * 100) if team_score > 0 else 0
    obj_scores = objective_score_series(player_df)
    obj_score = obj_scores.sum() / games if games else 0
    
    # Medals and misc
    total_medals = pd.to_numeric(player_df.get('medal_count', 0), errors='coerce').fillna(0).sum()
    medals = total_medals / games
    avg_life = pd.to_numeric(player_df.get('average_life_duration', 0), errors='coerce').fillna(0).mean()
    callouts = pd.to_numeric(player_df.get('callout_assists', 0), errors='coerce').fillna(0).sum() / games
    
    return {
        'kills': kills, 'deaths': deaths, 'assists': assists,
        'kd1': kd1, 'kd2': kd2, 'kda': kda,
        'dmg_plus': dmg_plus, 'dmg_minus': dmg_minus, 'dmg_diff': dmg_diff,
        'dmg_per_ka': dmg_per_ka, 'dmg_per_death': dmg_per_death, 'dmg_per_min': dmg_per_min,
        'dmg_pct_plus': dmg_pct_plus, 'dmg_pct_minus': dmg_pct_minus,
        'fired': fired, 'hit': hit, 'accuracy': accuracy,
        'score': score, 'obj_score': obj_score, 'score_pct': score_pct,
        'medals': medals, 'avg_life': avg_life, 'callouts': callouts
    }


def format_player_stats_row(player: str, games: int, wins: int, stats: dict, csr: float = None) -> dict:
    """Format stats dict into a row dict with proper formatting."""
    win_pct = wins / games * 100 if games > 0 else 0
    return {
        'player': player,
        'csr': format_float(csr, 1) if csr else '-',
        'games': format_int(games),
        'win_pct': format_float(win_pct, 1),
        'kills': format_float(stats.get('kills', 0), 1),
        'deaths': format_float(stats.get('deaths', 0), 1),
        'assists': format_float(stats.get('assists', 0), 1),
        'kd1': format_float(stats.get('kd1', 0), 2),
        'kd2': format_float(stats.get('kd2', 0), 2),
        'kda': format_float(stats.get('kda', 0), 2),
        'dmg_plus': format_float(stats.get('dmg_plus', 0), 0),
        'dmg_minus': format_float(stats.get('dmg_minus', 0), 0),
        'dmg_diff': format_signed(stats.get('dmg_diff', 0), 0),
        'dmg_per_ka': format_float(stats.get('dmg_per_ka', 0), 0),
        'dmg_per_death': format_float(stats.get('dmg_per_death', 0), 0),
        'dmg_per_min': format_float(stats.get('dmg_per_min', 0), 0),
        'dmg_pct_plus': format_float(stats.get('dmg_pct_plus', 0), 1),
        'dmg_pct_minus': format_float(stats.get('dmg_pct_minus', 0), 1),
        'fired': format_int(stats.get('fired', 0)),
        'landed': format_int(stats.get('hit', 0)),
        'accuracy': format_float(stats.get('accuracy', 0), 1),
        'score': format_float(stats.get('score', 0), 0),
        'obj_score': format_float(stats.get('obj_score', 0), 1),
        'score_pct': format_float(stats.get('score_pct', 0), 1),
        'medals': format_float(stats.get('medals', 0), 1),
        'avg_life': format_float(stats.get('avg_life', 0), 1),
        'callouts': format_float(stats.get('callouts', 0), 1)
    }


FULL_HEATMAP_CONFIG = {
    'csr': True, 'games': True, 'win_pct': True, 'kda': True, 'kd1': True, 'kd2': True,
    'kills': True, 'deaths': False, 'assists': True,
    'dmg_plus': True, 'dmg_minus': False, 'dmg_diff': True,
    'dmg_per_ka': True, 'dmg_per_death': True, 'dmg_per_min': True,
    'dmg_pct_plus': True, 'dmg_pct_minus': True,
    'fired': True, 'landed': True, 'accuracy': True,
    'score': True, 'obj_score': True, 'score_pct': True,
    'medals': True, 'avg_life': True, 'callouts': True, 'csr_delta': True
}



def build_breakdown(df: pd.DataFrame, column: str, limit: int = 100) -> list:
    """Build breakdown stats for maps/playlists/modes."""
    if df.empty or column not in df.columns:
        return []
    
    working = df
    group_col = column
    
    if column == 'map':
        working = add_normalized_map_column(df, column)
        group_col = '_map_normalized'
    
    rows = []
    grouped = working.groupby(group_col)
    active_maps = get_active_map_set() if column == 'map' else None

    for name, group in grouped:
        if not str(name).strip():
            continue
        if _map_hidden(name, active_maps):  # hide retired/out-of-rotation maps
            continue

        matches = len(group)
        outcomes = group['outcome'].astype(str).str.lower() if 'outcome' in group.columns else pd.Series()
        wins = (outcomes == 'win').sum() if not outcomes.empty else 0
        win_rate = wins / matches * 100 if matches else 0
        
        rows.append({
            'name': name,
            'matches': matches,
            'win_rate': win_rate
        })
    
    rows.sort(key=lambda item: item['matches'], reverse=True)
    trimmed = rows[:limit] if limit else rows
    
    out = [
        {
            'name': row['name'],
            'matches': format_int(row['matches']),
            'win_rate': f"{row['win_rate']:.1f}%" if row['matches'] else '0%'
        }
        for row in trimmed
    ]
    
    add_heatmap_classes(out, {'matches': True, 'win_rate': True})
    return out


def build_cards(df: pd.DataFrame) -> list:
    """Build summary cards for dashboard."""
    if df.empty:
        return []
    
    matches = len(df)
    outcomes = df['outcome'].astype(str).str.lower() if 'outcome' in df.columns else pd.Series()
    wins = (outcomes == 'win').sum() if not outcomes.empty else 0
    losses = (outcomes == 'loss').sum() if not outcomes.empty else 0
    
    kills = pd.to_numeric(df.get('kills', 0), errors='coerce').fillna(0).sum() if matches else 0
    deaths = pd.to_numeric(df.get('deaths', 0), errors='coerce').fillna(0).sum() if matches else 0
    assists = pd.to_numeric(df.get('assists', 0), errors='coerce').fillna(0).sum() if matches else 0
    
    avg_kda = safe_kda(kills / matches if matches else 0, 
                       assists / matches if matches else 0,
                       deaths / matches if matches else 0)
    
    accuracy = 0
    if 'shots_fired' in df.columns and 'shots_hit' in df.columns:
        fired = pd.to_numeric(df['shots_fired'], errors='coerce').fillna(0).sum()
        hit = pd.to_numeric(df['shots_hit'], errors='coerce').fillna(0).sum()
        accuracy = hit / fired * 100 if fired > 0 else 0
    
    win_rate = wins / matches * 100 if matches else 0
    
    return [
        {
            'label': 'Matches',
            'value': format_int(matches),
            'detail': 'Total matches'
        },
        {
            'label': 'Win Rate',
            'value': f'{win_rate:.1f}%',
            'detail': f'{wins}W - {losses}L'
        },
        {
            'label': 'Avg KDA',
            'value': format_float(avg_kda, 2),
            'detail': 'Kills + Assists/3 - Deaths'
        },
        {
            'label': 'Accuracy',
            'value': format_pct(accuracy / 100),
            'detail': 'Shot accuracy'
        }
    ]


def normalize_trend_df(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize dataframe for trend analysis."""
    if df.empty:
        return df
    
    working = df.copy()
    
    if 'playlist' in working.columns:
        working = _ranked_only(working)
    
    if 'outcome' in working.columns:
        outcome_lower = working['outcome'].astype(str).str.lower()
        working = working[outcome_lower != 'dnf'].copy()
    
    if 'date' in working.columns:
        ensure_datetime(working)
        working = working.dropna(subset=['date'])
        
        try:
            working['date_local'] = working['date'].dt.tz_convert(APP_TIMEZONE)
        except Exception:
            working['date_local'] = working['date']
        
        working['date_str'] = working['date_local'].dt.strftime('%Y-%m-%d')
    
    return working


def apply_trend_range(df: pd.DataFrame, range_key: str) -> pd.DataFrame:
    """Filter trends to a specific date range."""
    if df.empty or 'date' not in df.columns:
        return df
    
    days_map = {
        '7': 7, '30': 30, '90': 90, '180': 180,
        '365': 365, '730': 730, '1095': 1095
    }
    
    days = days_map.get(range_key)
    if days is None:  # 'all' or unknown
        return df
    
    date_series = pd.to_datetime(df['date'], errors='coerce', utc=True)
    cutoff = pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=days)
    return df[date_series >= cutoff]


def apply_leaderboard_period(df: pd.DataFrame, period: str) -> pd.DataFrame:
    if df.empty or 'date' not in df.columns:
        return df
    
    period_days = {
        'week': 7,
        'month': 30
    }
    days = period_days.get(period)
    if not days:
        return df
    
    working = df.copy()
    ensure_datetime(working)
    working = working.dropna(subset=['date'])
    if working.empty:
        return working
    
    cutoff = pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=days)
    return working[working['date'] >= cutoff]


def build_lifetime_stats(df: pd.DataFrame) -> list:
    """Build lifetime statistics per player."""
    if df.empty:
        return []
    
    rows = []
    for player in unique_sorted(df['player_gamertag']):
        player_df = df[df['player_gamertag'] == player]
        if player_df.empty:
            continue
        
        games = len(player_df)
        outcomes = player_df['outcome'].astype(str).str.lower() if 'outcome' in player_df.columns else pd.Series()
        wins = (outcomes == 'win').sum() if not outcomes.empty else 0
        losses = (outcomes == 'loss').sum() if not outcomes.empty else 0
        ties = (outcomes == 'tie').sum() if not outcomes.empty else 0
        
        total_kills = pd.to_numeric(player_df.get('kills', 0), errors='coerce').fillna(0).sum()
        total_deaths = pd.to_numeric(player_df.get('deaths', 0), errors='coerce').fillna(0).sum()
        total_assists = pd.to_numeric(player_df.get('assists', 0), errors='coerce').fillna(0).sum()
        
        total_damage_dealt = pd.to_numeric(player_df.get('damage_dealt', 0), errors='coerce').fillna(0).sum()
        total_damage_taken = pd.to_numeric(player_df.get('damage_taken', 0), errors='coerce').fillna(0).sum()
        
        kills_pg = total_kills / games if games else 0
        deaths_pg = total_deaths / games if games else 0
        assists_pg = total_assists / games if games else 0
        damage_pg = total_damage_dealt / games if games else 0
        
        kda = safe_kda(kills_pg, assists_pg, deaths_pg)
        kd_ratio = kills_pg / deaths_pg if deaths_pg > 0 else kills_pg
        
        accuracy = 0
        if 'shots_fired' in player_df.columns and 'shots_hit' in player_df.columns:
            fired = pd.to_numeric(player_df['shots_fired'], errors='coerce').fillna(0).sum()
            hit = pd.to_numeric(player_df['shots_hit'], errors='coerce').fillna(0).sum()
            accuracy = hit / fired if fired > 0 else 0
        
        total_score = score_series(player_df).sum()
        avg_score = total_score / games if games else 0
        obj_scores = objective_score_series(player_df)
        avg_obj_score = obj_scores.sum() / games if games else 0

        total_perfect = numeric_series(player_df, 'medal_perfect').sum()
        perfect_pg = total_perfect / games if games else 0

        _opp = (pd.to_numeric(player_df.get('enemy_team_mmr'), errors='coerce').dropna()
                if 'enemy_team_mmr' in player_df.columns else pd.Series(dtype=float))
        opp_mmr = round(float(_opp.mean())) if len(_opp) else None

        rows.append({
            'player': player,
            'matches': format_int(games),
            'wins': format_int(wins),
            'losses': format_int(losses),
            'win_rate': format_float(wins / (wins + losses) * 100 if (wins + losses) else 0, 1),
            'kills': format_float(kills_pg, 1),
            'deaths': format_float(deaths_pg, 1),
            'assists': format_float(assists_pg, 1),
            'kda': format_float(kda, 2),
            'kda_min': format_float(kda_per_min(player_df), 2),
            'obj_min': _obj_dash(obj_per_min(player_df), 1),
            'accuracy': format_pct(accuracy),
            'avg_score': format_float(avg_score, 0),
            'avg_obj_score': format_float(avg_obj_score, 1),
            'perfect_pg': format_float(perfect_pg, 2),
            'perfect_total': format_int(total_perfect),
            'opp_mmr': format_int(opp_mmr) if opp_mmr is not None else '—',
        })

    add_heatmap_classes(rows, {
        'matches': True, 'wins': True, 'losses': False, 'win_rate': True,
        'kills': True, 'deaths': False, 'assists': True,
        'kda': True, 'accuracy': True, 'avg_score': True, 'avg_obj_score': True,
        'perfect_pg': True, 'opp_mmr': True
    })
    # Lifetime grade = ABSOLUTE report-card overall (same fixed scale as the
    # dashboard card and the player page), so a player's overall grade reads the
    # same everywhere on the site rather than being a squad-relative ranking.
    for row in rows:
        try:
            rc = build_player_report_card(df[df['player_gamertag'] == row['player']])
            if rc and rc.get('overall'):
                row['grade'] = rc['overall']['grade']
                row['grade_class'] = rc['overall']['grade_class']
                row['grade_tip'] = f"Overall skill grade (absolute scale, same as the player card) · {rc['games']} games"
            else:
                row['grade'] = '—'
                row['grade_class'] = ''
        except Exception:
            row['grade'] = '—'
            row['grade_class'] = ''

    return rows


def build_session_history(df: pd.DataFrame, limit: int | None = 20) -> list:
    """Build recent match history across all players."""
    if df.empty or 'date' not in df.columns:
        return []
    
    working = df.copy()
    ensure_datetime(working)
    working = working.dropna(subset=['date'])
    
    if working.empty:
        return []
    
    if isinstance(limit, int) and limit > 0:
        working = working.nlargest(limit, 'date')
    else:
        working = working.sort_values('date', ascending=False)
    
    score_values = score_series(working)
    if score_values.empty:
        score_values = pd.Series(0.0, index=working.index)
    obj_scores = objective_score_series(working)
    if obj_scores.empty:
        obj_scores = pd.Series(0.0, index=working.index)
    
    rows = []
    for idx, row in working.iterrows():
        kills = safe_float(row.get('kills', 0))
        deaths = safe_float(row.get('deaths', 0))
        assists = safe_float(row.get('assists', 0))
        kda = safe_kda(kills, assists, deaths)
        kd = kills / deaths if deaths > 0 else kills
        
        damage_dealt = safe_float(row.get('damage_dealt', 0))
        damage_taken = safe_float(row.get('damage_taken', 0))
        damage_diff = damage_dealt - damage_taken
        
        fired = safe_float(row.get('shots_fired', 0))
        hit = safe_float(row.get('shots_hit', 0))
        accuracy = hit / fired * 100 if fired > 0 else safe_float(row.get('accuracy', 0))
        
        score = safe_float(score_values.loc[idx]) if idx in score_values.index else 0
        obj_score = safe_float(obj_scores.loc[idx]) if idx in obj_scores.index else 0

        _dmin = safe_float(row.get('duration', 0)) / 60.0
        kda_min = (kda / _dmin) if _dmin > 0 else None
        obj_min = (obj_score / _dmin) if _dmin > 0 else None

        game_grade = compute_match_grade(
            kda=kda, accuracy=accuracy, dmg_dealt=damage_dealt,
            dmg_taken=damage_taken, outcome=row.get('outcome'),
        ) or {}
        rows.append({
            'date': format_date(row.get('date')),
            'match_id': row.get('match_id', ''),
            'player': row.get('player_gamertag', ''),
            'grade': game_grade.get('grade', ''),
            'grade_class': game_grade.get('grade_class', ''),
            'grade_tip': game_grade.get('grade_tip', ''),
            'game_type': row.get('game_type', ''),
            'map': row.get('map', ''),
            'playlist': row.get('playlist', ''),
            'outcome': str(row.get('outcome', '')).title(),
            'outcome_class': outcome_class(row.get('outcome', '')),
            'kills': format_int(kills),
            'deaths': format_int(deaths),
            'assists': format_int(assists),
            'kda': format_float(kda, 2),
            'kda_min': format_float(kda_min, 2) if kda_min is not None else '—',
            'obj_min': _obj_dash(obj_min),
            'kd': format_float(kd, 2),
            'damage_dealt': format_int(damage_dealt),
            'damage_taken': format_int(damage_taken),
            'damage_diff': format_signed(damage_diff, 0),
            'shots_fired': format_int(fired),
            'shots_landed': format_int(hit),
            'accuracy': format_pct(accuracy),
            'score': format_int(score),
            'obj_score': _obj_dash(obj_score, 1),
            'medals': format_int(row.get('medal_count', 0)),
            'perfect': format_int(row.get('medal_perfect', 0)),
            'avg_life': format_float(row.get('average_life_duration', 0), 1),
            'headshots': format_int(row.get('headshot_kills', 0)),
            'melee': format_int(row.get('melee_kills', 0)),
            'grenade': format_int(row.get('grenade_kills', 0)),
            'power': format_int(row.get('power_weapon_kills', 0)),
            'callouts': format_int(row.get('callout_assists', 0))
        })
    
    add_heatmap_classes(rows, {
        'kills': True, 'deaths': False, 'assists': True,
        'kda': True, 'kd': True,
        'damage_dealt': True, 'damage_taken': False, 'damage_diff': True,
        'accuracy': True, 'score': True, 'obj_score': True,
        'medals': True, 'perfect': True, 'avg_life': True,
        'headshots': True, 'melee': True, 'grenade': True, 'power': True,
        'callouts': True
    })
    
    return rows


def extract_objective_score(df: pd.DataFrame) -> pd.Series:
    """Calculate objective score from personal score and combat/callout bonuses."""
    return objective_score_series(df)


def safe_col_sum(df: pd.DataFrame, col_name: str) -> float:
    """Safely get sum of a column, returning 0 if column doesn't exist."""
    if col_name in df.columns:
        return pd.to_numeric(df[col_name], errors='coerce').fillna(0).sum()
    return 0


def build_objective_stats(df: pd.DataFrame, period: str = 'all') -> list:
    """Build objective statistics (CTF, Oddball, Stronghold, KOTH, Extraction)."""
    if df.empty:
        return []
    
    # Filter by period if needed
    working = df.copy()
    if period == 'session':
        if 'date' in working.columns:
            ensure_datetime(working)
            working = working.sort_values('date', ascending=False).head(50)
    elif period == '30day' and 'date' in working.columns:
        ensure_datetime(working)
        cutoff = pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=30)
        working = working[working['date'] >= cutoff]
    
    rows = []
    for player in unique_sorted(working['player_gamertag']):
        player_df = working[working['player_gamertag'] == player]
        if player_df.empty:
            continue
        
        games = len(player_df)
        
        # CTF stats
        ctf_caps = safe_col_sum(player_df, 'capture_the_flag_stats_flag_captures')
        ctf_grabs = safe_col_sum(player_df, 'capture_the_flag_stats_flag_grabs')
        ctf_returns = safe_col_sum(player_df, 'capture_the_flag_stats_flag_returns')
        ctf_steals = safe_col_sum(player_df, 'capture_the_flag_stats_flag_steals')
        ctf_secures = safe_col_sum(player_df, 'capture_the_flag_stats_flag_secures')
        ctf_carrier_kills = safe_col_sum(player_df, 'capture_the_flag_stats_flag_carriers_killed')
        ctf_returner_kills = safe_col_sum(player_df, 'capture_the_flag_stats_flag_returners_killed')
        ctf_kills_as_carrier = safe_col_sum(player_df, 'capture_the_flag_stats_kills_as_flag_carrier')
        ctf_kills_as_returner = safe_col_sum(player_df, 'capture_the_flag_stats_kills_as_flag_returner')
        ctf_time = safe_col_sum(player_df, 'capture_the_flag_stats_time_as_flag_carrier')
        
        # Oddball stats
        oddball_time = safe_col_sum(player_df, 'oddball_stats_time_as_skull_carrier')
        oddball_longest = safe_col_sum(player_df, 'oddball_stats_longest_time_as_skull_carrier')
        oddball_grabs = safe_col_sum(player_df, 'oddball_stats_skull_grabs')
        oddball_ticks = safe_col_sum(player_df, 'oddball_stats_skull_scoring_ticks')
        oddball_carrier_kills = safe_col_sum(player_df, 'oddball_stats_kills_as_skull_carrier')
        oddball_carriers_killed = safe_col_sum(player_df, 'oddball_stats_skull_carriers_killed')
        
        # Stronghold/Zone stats
        sh_caps = safe_col_sum(player_df, 'zones_stats_stronghold_captures')
        sh_secures = safe_col_sum(player_df, 'zones_stats_stronghold_secures')
        sh_ticks = safe_col_sum(player_df, 'zones_stats_stronghold_scoring_ticks')
        sh_off_kills = safe_col_sum(player_df, 'zones_stats_stronghold_offensive_kills')
        sh_def_kills = safe_col_sum(player_df, 'zones_stats_stronghold_defensive_kills')
        sh_time = safe_col_sum(player_df, 'zones_stats_stronghold_occupation_time')
        
        # KOTH is same as stronghold in Halo Infinite
        koth_time = sh_time
        koth_ticks = sh_ticks
        
        # Extraction stats
        extract_success = safe_col_sum(player_df, 'extraction_stats_successful_extractions')
        extract_conv_complete = safe_col_sum(player_df, 'extraction_stats_extraction_conversions_completed')
        extract_conv_denied = safe_col_sum(player_df, 'extraction_stats_extraction_conversions_denied')
        extract_init_complete = safe_col_sum(player_df, 'extraction_stats_extraction_initiations_completed')
        extract_init_denied = safe_col_sum(player_df, 'extraction_stats_extraction_initiations_denied')
        
        # Average life
        avg_life = 0
        if 'average_life_duration' in player_df.columns:
            avg_life = pd.to_numeric(player_df['average_life_duration'], errors='coerce').fillna(0).mean()
        
        rows.append({
            'player': player,
            'games': format_int(games),
            # CTF
            'ctf_caps': format_int(ctf_caps),
            'ctf_grabs': format_int(ctf_grabs),
            'ctf_returns': format_int(ctf_returns),
            'ctf_steals': format_int(ctf_steals),
            'ctf_secures': format_int(ctf_secures),
            'ctf_carrier_kills': format_int(ctf_carrier_kills),
            'ctf_returner_kills': format_int(ctf_returner_kills),
            'ctf_kills_as_carrier': format_int(ctf_kills_as_carrier),
            'ctf_kills_as_returner': format_int(ctf_kills_as_returner),
            'ctf_time': int(ctf_time),
            # Oddball
            'oddball_time': int(oddball_time),
            'oddball_longest': int(oddball_longest),
            'oddball_grabs': format_int(oddball_grabs),
            'oddball_ticks': format_int(oddball_ticks),
            'oddball_carrier_kills': format_int(oddball_carrier_kills),
            'oddball_carriers_killed': format_int(oddball_carriers_killed),
            # Stronghold
            'sh_caps': format_int(sh_caps),
            'sh_secures': format_int(sh_secures),
            'sh_ticks': format_int(sh_ticks),
            'sh_off_kills': format_int(sh_off_kills),
            'sh_def_kills': format_int(sh_def_kills),
            'sh_time': int(sh_time),
            # KOTH
            'koth_time': int(koth_time),
            'koth_ticks': format_int(koth_ticks),
            # Extraction
            'extract_success': format_int(extract_success),
            'extract_conv_complete': format_int(extract_conv_complete),
            'extract_conv_denied': format_int(extract_conv_denied),
            'extract_init_complete': format_int(extract_init_complete),
            'extract_init_denied': format_int(extract_init_denied),
            # Misc
            'avg_life': format_float(avg_life, 1)
        })
    
    add_heatmap_classes(rows, {
        'games': True,
        'ctf_caps': True, 'ctf_grabs': True, 'ctf_returns': True, 'ctf_steals': True,
        'ctf_secures': True, 'ctf_carrier_kills': True, 'ctf_kills_as_carrier': True, 'ctf_time': True,
        'oddball_time': True, 'oddball_longest': True, 'oddball_grabs': True, 
        'oddball_ticks': True, 'oddball_carrier_kills': True,
        'sh_caps': True, 'sh_secures': True, 'sh_ticks': True,
        'sh_off_kills': True, 'sh_def_kills': True, 'sh_time': True,
        'koth_time': True, 'koth_ticks': True,
        'extract_success': True, 'extract_conv_complete': True, 'extract_init_complete': True,
        'avg_life': True
    })
    add_composite_grades(rows, {
        'ctf_secures': True, 'ctf_caps': True, 'ctf_carrier_kills': True,
        'oddball_ticks': True, 'oddball_carrier_kills': True,
        'sh_secures': True, 'sh_ticks': True, 'sh_off_kills': True, 'sh_def_kills': True,
        'extract_success': True,
    }, 'Objective grade')

    return rows


def build_medal_matrix(
    df: pd.DataFrame,
    players: list[str],
    medal_cols: list[str],
    per_game: bool
) -> list[dict]:
    rows = []
    for col in medal_cols:
        medal_name = col.replace('medal_', '').replace('_', ' ').title()
        row = {'medal': medal_name}
        values = []
        for player in players:
            player_df = df[df['player_gamertag'] == player]
            total = pd.to_numeric(player_df.get(col, 0), errors='coerce').fillna(0).sum()
            if per_game:
                games = len(player_df)
                value = total / games if games else 0
                row[player] = format_float(value, 2)
            else:
                value = total
                row[player] = format_int(value)
            values.append(value)
        for player, value in zip(players, values):
            row[f'{player}_heat'] = get_heatmap_class(value, values, True)
        rows.append(row)
    return rows


def build_medal_stats(df: pd.DataFrame) -> tuple[list[str], list[dict], list[dict]]:
    """Build medal statistics - returns (players, per_game_rows, total_rows)."""
    if df.empty:
        return [], [], []
    
    players = unique_sorted(df['player_gamertag'])
    if not players:
        return [], [], []
    
    medal_cols = [
        col for col in df.columns
        if col.startswith('medal_') and col != 'medal_count'
    ]
    
    if not medal_cols:
        return players, [], []
    
    medal_totals = []
    for col in medal_cols:
        total = pd.to_numeric(df[col], errors='coerce').fillna(0).sum()
        if total > 0:
            medal_totals.append((col, total))
    
    medal_totals.sort(key=lambda item: item[1], reverse=True)
    top_cols = [col for col, _ in medal_totals[:50]]

    ranked_df = _ranked_only(df)

    # The per-game (Ranked) table must pick its columns from RANKED totals, else
    # medals earned only in social show up as all-zero rows in the Ranked table.
    ranked_totals = []
    for col in medal_cols:
        total = pd.to_numeric(ranked_df[col], errors='coerce').fillna(0).sum() if col in ranked_df.columns else 0
        if total > 0:
            ranked_totals.append((col, total))
    ranked_totals.sort(key=lambda item: item[1], reverse=True)
    top_cols_ranked = [col for col, _ in ranked_totals[:50]]

    per_game_rows = build_medal_matrix(ranked_df, players, top_cols_ranked, per_game=True)
    total_rows = build_medal_matrix(df, players, top_cols, per_game=False)

    return players, per_game_rows, total_rows


def build_highlight_games(df: pd.DataFrame, limit: int = 20) -> list:
    """Build the top games by KDA across all tracked players (Ranked, completed
    games only)."""
    if df.empty:
        return []

    # Match the subtitle's promise: Ranked-only, decided games (drop DNFs).
    df = df.copy()
    if 'playlist' in df.columns:
        df = _ranked_only(df)
    if 'outcome' in df.columns:
        df = df[df['outcome'].astype(str).str.lower().isin(['win', 'loss'])]
    if df.empty:
        return []

    highlights = []

    # Add KDA column for sorting
    if 'kills' in df.columns and 'deaths' in df.columns and 'assists' in df.columns:
        kills = pd.to_numeric(df['kills'], errors='coerce').fillna(0)
        deaths = pd.to_numeric(df['deaths'], errors='coerce').fillna(0)
        assists = pd.to_numeric(df['assists'], errors='coerce').fillna(0)
        df['_kda_score'] = kills + assists / 3 - deaths
    
    # Top KDA games
    if '_kda_score' in df.columns:
        top_kda = df.nlargest(limit, '_kda_score')
        
        for _, row in top_kda.iterrows():
            gg = compute_match_grade(
                kda=row.get('_kda_score'), accuracy=row.get('accuracy'),
                dmg_dealt=row.get('damage_dealt'), dmg_taken=row.get('damage_taken'),
                outcome=row.get('outcome'),
            ) or {}
            highlights.append({
                'player': row.get('player_gamertag', 'Unknown'),
                'date': format_date(row.get('date')),
                'grade': gg.get('grade', ''),
                'grade_class': gg.get('grade_class', ''),
                'grade_tip': gg.get('grade_tip', ''),
                'playlist': row.get('playlist', ''),
                'game_type': row.get('game_type', ''),
                'map': normalize_map_name(row.get('map', '')),
                'outcome': str(row.get('outcome', '')).title(),
                'kills': format_int(row.get('kills', 0)),
                'kills_heat': '',
                'deaths': format_int(row.get('deaths', 0)),
                'deaths_heat': '',
                'assists': format_int(row.get('assists', 0)),
                'assists_heat': '',
                'kda': format_float(row.get('_kda_score', 0), 2),
                'kda_heat': '',
                'accuracy': format_pct(row.get('accuracy', 0)),
                'accuracy_heat': '',
                'score': format_int(row.get('personal_score', 0)),
                'score_heat': '',
                'dmg_min': format_float(row.get('dmg/min', 0), 1),
                'dmg_min_heat': '',
                'dmg_diff': format_int(row.get('dmg_difference', 0)),
                'dmg_diff_heat': '',
                'medals': format_int(row.get('medal_count', 0)),
                'medals_heat': ''
            })
    
    return highlights[:limit]


def build_hall_fame_shame(df: pd.DataFrame) -> tuple[list, list]:
    """Build hall of fame and hall of shame."""
    if df.empty:
        return [], []
    
    ranked_df = _ranked_only(df)
    if ranked_df.empty:
        return [], []
    
    fame_rows = []
    shame_rows = []
    
    for player in unique_sorted(ranked_df['player_gamertag']):
        player_df = ranked_df[ranked_df['player_gamertag'] == player]
        if player_df.empty:
            continue
        
        win_streak, loss_streak = compute_best_streaks(player_df)
        
        kills = numeric_series(player_df, 'kills')
        deaths = numeric_series(player_df, 'deaths')
        assists = numeric_series(player_df, 'assists')
        kd_series = pd.Series(0.0, index=player_df.index)
        nonzero = deaths > 0
        kd_series.loc[nonzero] = kills[nonzero] / deaths[nonzero]
        kd_series.loc[~nonzero] = kills[~nonzero]
        kda_series = kills + assists / 3 - deaths
        
        if 'shots_fired' in player_df.columns and 'shots_hit' in player_df.columns:
            fired = numeric_series(player_df, 'shots_fired')
            hit = numeric_series(player_df, 'shots_hit')
            accuracy = pd.Series(0.0, index=player_df.index)
            nonzero = fired > 0
            accuracy.loc[nonzero] = hit[nonzero] / fired[nonzero] * 100
        else:
            accuracy = numeric_series(player_df, 'accuracy')
        damage_dealt = numeric_series(player_df, 'damage_dealt')
        damage_taken = numeric_series(player_df, 'damage_taken')
        damage_diff = damage_dealt - damage_taken
        score_vals = score_series(player_df)
        obj_score = objective_score_series(player_df)
        
        medals = numeric_series(player_df, 'medal_count')
        headshots = numeric_series(player_df, 'headshot_kills')
        grenades = numeric_series(player_df, 'grenade_kills')
        melee = numeric_series(player_df, 'melee_kills')
        power = numeric_series(player_df, 'power_weapon_kills')
        callouts = numeric_series(player_df, 'callout_assists')
        fired = numeric_series(player_df, 'shots_fired')
        landed = numeric_series(player_df, 'shots_hit')
        objectives = numeric_series(player_df, 'objectives_completed')
        spree = numeric_series(player_df, 'max_killing_spree')
        avg_life = numeric_series(player_df, 'average_life_duration')
        betrayals = numeric_series(player_df, 'betrayals')
        suicides = numeric_series(player_df, 'suicides')
        
        csr_delta = pd.Series(dtype=float)
        if 'pre_match_csr' in player_df.columns and 'post_match_csr' in player_df.columns:
            pre = pd.to_numeric(player_df['pre_match_csr'], errors='coerce').fillna(0)
            post = pd.to_numeric(player_df['post_match_csr'], errors='coerce').fillna(0)
            csr_delta = post - pre
        
        fame_rows.append({
            'player': player,
            'win_streak': format_int(win_streak),
            'max_kills': format_int(kills.max() if not kills.empty else 0),
            'max_assists': format_int(assists.max() if not assists.empty else 0),
            'max_kda': format_float(kda_series.max() if not kda_series.empty else 0, 2),
            'max_kd': format_float(kd_series.max() if not kd_series.empty else 0, 2),
            'max_accuracy': format_float(accuracy.max() if not accuracy.empty else 0, 1),
            'max_damage_dealt': format_int(damage_dealt.max() if not damage_dealt.empty else 0),
            'max_damage_diff': format_signed(damage_diff.max() if not damage_diff.empty else 0, 0),
            'max_score': format_int(score_vals.max() if not score_vals.empty else 0),
            'max_obj_score': format_float(obj_score.max() if not obj_score.empty else 0, 1),
            'max_medals': format_int(medals.max() if not medals.empty else 0),
            'max_headshots': format_int(headshots.max() if not headshots.empty else 0),
            'max_grenades': format_int(grenades.max() if not grenades.empty else 0),
            'max_melee': format_int(melee.max() if not melee.empty else 0),
            'max_power': format_int(power.max() if not power.empty else 0),
            'max_callouts': format_int(callouts.max() if not callouts.empty else 0),
            'max_fired': format_int(fired.max() if not fired.empty else 0),
            'max_landed': format_int(landed.max() if not landed.empty else 0),
            'max_objectives': format_int(objectives.max() if not objectives.empty else 0),
            'max_spree': format_int(spree.max() if not spree.empty else 0),
            'max_avg_life': format_float(avg_life.max() if not avg_life.empty else 0, 1),
            'max_csr_gain': format_signed(csr_delta.max() if not csr_delta.empty else 0, 0)
        })
        
        shame_rows.append({
            'player': player,
            'loss_streak': format_int(loss_streak),
            'max_deaths': format_int(deaths.max() if not deaths.empty else 0),
            'min_kda': format_float(kda_series.min() if not kda_series.empty else 0, 2),
            'min_kd': format_float(kd_series.min() if not kd_series.empty else 0, 2),
            'min_accuracy': format_float(accuracy.min() if not accuracy.empty else 0, 1),
            'max_damage_taken': format_int(damage_taken.max() if not damage_taken.empty else 0),
            'min_damage_diff': format_signed(damage_diff.min() if not damage_diff.empty else 0, 0),
            'min_score': format_int(score_vals.min() if not score_vals.empty else 0),
            'min_obj_score': format_float(obj_score.min() if not obj_score.empty else 0, 1),
            'min_medals': format_int(medals.min() if not medals.empty else 0),
            'min_avg_life': format_float(avg_life.min() if not avg_life.empty else 0, 1),
            'max_csr_loss': format_signed(csr_delta.min() if not csr_delta.empty else 0, 0),
            'max_betrayals': format_int(betrayals.max() if not betrayals.empty else 0),
            'max_suicides': format_int(suicides.max() if not suicides.empty else 0)
        })
    
    add_heatmap_classes(fame_rows, {
        'win_streak': True, 'max_kills': True, 'max_assists': True,
        'max_kda': True, 'max_kd': True, 'max_accuracy': True,
        'max_damage_dealt': True, 'max_damage_diff': True, 'max_score': True,
        'max_obj_score': True, 'max_medals': True, 'max_headshots': True,
        'max_grenades': True, 'max_melee': True, 'max_power': True,
        'max_callouts': True, 'max_fired': True, 'max_landed': True,
        'max_objectives': True, 'max_spree': True, 'max_avg_life': True,
        'max_csr_gain': True
    })
    
    add_heatmap_classes(shame_rows, {
        'loss_streak': True, 'max_deaths': True, 'min_kda': False,
        'min_kd': False, 'min_accuracy': False, 'max_damage_taken': True,
        'min_damage_diff': False, 'min_score': False, 'min_obj_score': False,
        'min_medals': False, 'min_avg_life': False, 'max_csr_loss': False,
        'max_betrayals': True, 'max_suicides': True
    })

    add_composite_grades(fame_rows, {
        'max_kda': True, 'max_kd': True, 'max_accuracy': True,
        'max_damage_dealt': True, 'win_streak': True,
    }, 'Hall of Fame grade')

    add_composite_grades(shame_rows, {
        'max_deaths': False, 'min_kda': True, 'min_accuracy': True,
        'max_betrayals': False, 'max_suicides': False, 'loss_streak': False,
    }, 'Hall of Shame grade')

    return fame_rows, shame_rows


_active_maps_cache = {'count': -1, 'set': None}


def get_active_map_set():
    """Normalized map names with >= MAP_MIN_GAMES squad-wide ranked games.

    Maps below the floor are retired / out of rotation, so they're hidden from
    every map-specific view. Determined SQUAD-WIDE (from the full ranked cache),
    not per-player, so a casual player still sees active maps but nobody sees a
    dead map. Returns None when data is too thin to judge (disables filtering).
    Cached against the row count so it only recomputes when the DB grows.
    """
    try:
        cnt = count_cache.get()
    except Exception:
        cnt = -1
    cached = _active_maps_cache
    if cached['count'] == cnt and cached['set'] is not None:
        return cached['set']
    active = None
    try:
        df = cache.get()
        if not df.empty and 'map' in df.columns:
            working = add_normalized_map_column(df)
            counts = working.groupby('_map_normalized').size()
            keep = {str(k) for k, v in counts.items() if str(k).strip() and v >= MAP_MIN_GAMES}
            if keep:  # never hide every map on thin data
                active = keep
    except Exception as exc:
        logger.warning('active map set failed: %s', exc)
        active = None
    cached['count'] = cnt
    cached['set'] = active
    return active


def _map_hidden(map_name, active) -> bool:
    """True if this normalized map should be hidden (retired / out of rotation)."""
    return active is not None and str(map_name) not in active


def build_map_stats(df: pd.DataFrame) -> list:
    """Build detailed map statistics."""
    if df.empty or 'map' not in df.columns:
        return []
    
    working = add_normalized_map_column(df)
    active = get_active_map_set()

    rows = []
    for map_name in unique_sorted(working['_map_normalized']):
        if not map_name or _map_hidden(map_name, active):
            continue

        map_df = working[working['_map_normalized'] == map_name]
        if map_df.empty:
            continue

        games = len(map_df)
        outcomes = map_df['outcome'].astype(str).str.lower() if 'outcome' in map_df.columns else pd.Series()
        wins = (outcomes == 'win').sum() if not outcomes.empty else 0

        kills = numeric_series(map_df, 'kills')
        deaths = numeric_series(map_df, 'deaths')
        assists = numeric_series(map_df, 'assists')
        kda_series = kills + assists / 3 - deaths

        avg_kills = kills.mean() if games else 0
        avg_deaths = deaths.mean() if games else 0
        avg_kda = kda_series.mean() if games else 0
        
        rows.append({
            'map': map_name,
            'games': format_int(games),
            'wins': format_int(wins),
            'win_pct': format_float(wins / games * 100 if games else 0, 1),
            'avg_kills': format_float(avg_kills, 1),
            'avg_deaths': format_float(avg_deaths, 1),
            'avg_kda': format_float(avg_kda, 2),
            'kda_min': format_float(kda_per_min(map_df), 2),
            'obj_min': _obj_dash(obj_per_min(map_df), 1),
        })

    add_heatmap_classes(rows, {
        'games': True, 'win_pct': True, 'avg_kills': True,
        'avg_deaths': False, 'avg_kda': True, 'kda_min': True
    })
    add_composite_grades(rows, {'win_pct': True}, 'Map grade')
    # Sort by win% so the Grade column (also win%-based) reads top→bottom.
    rows.sort(key=lambda x: to_number(x.get('win_pct')) or 0, reverse=True)
    
    return rows


def build_mode_stats(df: pd.DataFrame) -> list:
    """Build detailed mode statistics."""
    if df.empty or 'game_type' not in df.columns:
        return []
    
    rows = []
    for mode_name in unique_sorted(df['game_type']):
        if not mode_name:
            continue
        
        mode_df = df[df['game_type'] == mode_name]
        games = len(mode_df)
        if games == 0:
            continue
        
        outcomes = mode_df['outcome'].astype(str).str.lower() if 'outcome' in mode_df.columns else pd.Series()
        wins = (outcomes == 'win').sum() if not outcomes.empty else 0
        
        kills = numeric_series(mode_df, 'kills')
        deaths = numeric_series(mode_df, 'deaths')
        assists = numeric_series(mode_df, 'assists')
        kda_series = kills + assists / 3 - deaths
        avg_kda = kda_series.mean() if games else 0
        
        score_vals = score_series(mode_df)
        avg_score = score_vals.mean() if not score_vals.empty else 0
        
        rows.append({
            'mode': mode_name,
            'games': format_int(games),
            'win_pct': format_float(wins / games * 100 if games else 0, 1),
            'avg_kda': format_float(avg_kda, 2),
            'kda_min': format_float(kda_per_min(mode_df), 2),
            'obj_min': _obj_dash(obj_per_min(mode_df), 1),
            'avg_score': format_float(avg_score, 0)
        })

    add_heatmap_classes(rows, {
        'games': True, 'win_pct': True, 'avg_kda': True, 'avg_score': True, 'kda_min': True
    })
    add_composite_grades(rows, {'win_pct': True}, 'Mode grade')

    rows.sort(key=lambda x: to_number(x.get('win_pct')) or 0, reverse=True)
    return rows


def build_player_map_stats(df: pd.DataFrame) -> list:
    """Build per-player map performance."""
    if df.empty or 'map' not in df.columns:
        return []
    
    working = add_normalized_map_column(df)
    active = get_active_map_set()

    rows = []
    for player in unique_sorted(working['player_gamertag']):
        player_df = working[working['player_gamertag'] == player]

        for map_name in unique_sorted(player_df['_map_normalized']):
            if not map_name or _map_hidden(map_name, active):
                continue

            map_df = player_df[player_df['_map_normalized'] == map_name]
            games = len(map_df)

            if games < 3:  # Skip maps with few games
                continue
            
            outcomes = map_df['outcome'].astype(str).str.lower() if 'outcome' in map_df.columns else pd.Series()
            wins = (outcomes == 'win').sum() if not outcomes.empty else 0
            
            kills = numeric_series(map_df, 'kills')
            deaths = numeric_series(map_df, 'deaths')
            assists = numeric_series(map_df, 'assists')
            kda_series = kills + assists / 3 - deaths
            avg_kda = kda_series.mean() if games else 0
            
            rows.append({
                'player': player,
                'map': map_name,
                'games': format_int(games),
                'win_pct': format_float(wins / games * 100 if games else 0, 1),
                'avg_kda': format_float(avg_kda, 2)
            })
    
    add_heatmap_classes(rows, {'win_pct': True, 'avg_kda': True})
    add_composite_grades(rows, {'win_pct': True}, 'Player-map grade')
    rows.sort(key=lambda x: (x['player'], -to_number(x['win_pct']) or 0))

    return rows


def build_player_mode_stats(df: pd.DataFrame) -> list:
    """Per-player game-mode performance (mirror of build_player_map_stats)."""
    if df.empty or 'game_type' not in df.columns:
        return []
    rows = []
    for player in unique_sorted(df['player_gamertag']):
        player_df = df[df['player_gamertag'] == player]
        for mode_name in unique_sorted(player_df['game_type']):
            if not mode_name:
                continue
            mode_df = player_df[player_df['game_type'] == mode_name]
            games = len(mode_df)
            if games < 3:
                continue
            outcomes = mode_df['outcome'].astype(str).str.lower() if 'outcome' in mode_df.columns else pd.Series()
            wins = (outcomes == 'win').sum() if not outcomes.empty else 0
            kills = numeric_series(mode_df, 'kills')
            deaths = numeric_series(mode_df, 'deaths')
            assists = numeric_series(mode_df, 'assists')
            avg_kda = (kills + assists / 3 - deaths).mean() if games else 0
            rows.append({
                'player': player,
                'mode': mode_name,
                'games': format_int(games),
                'win_pct': format_float(wins / games * 100 if games else 0, 1),
                'avg_kda': format_float(avg_kda, 2),
            })
    add_heatmap_classes(rows, {'win_pct': True, 'avg_kda': True})
    add_composite_grades(rows, {'win_pct': True}, 'Player-mode grade')
    rows.sort(key=lambda x: (x['player'], -(to_number(x['win_pct']) or 0)))
    return rows


def build_trend_data(df: pd.DataFrame, stat_col: str, stat_name: str) -> dict:
    """Build generic trend data for a statistic."""
    if df.empty or 'date' not in df.columns or stat_col not in df.columns:
        return {}
    
    working = df.copy()
    ensure_datetime(working)
    working = working.dropna(subset=['date'])
    
    try:
        working['date_local'] = working['date'].dt.tz_convert(APP_TIMEZONE)
    except Exception:
        working['date_local'] = working['date']
    
    working['date_str'] = working['date_local'].dt.strftime('%Y-%m-%d')
    working[stat_col] = pd.to_numeric(working[stat_col], errors='coerce').fillna(0)
    
    trends = {}
    for player in unique_sorted(working['player_gamertag']):
        player_df = working[working['player_gamertag'] == player].sort_values('date')
        
        daily = player_df.groupby('date_str')[stat_col].mean().reset_index()
        
        trends[player] = [
            {'date': row['date_str'], stat_name: float(row[stat_col])}
            for _, row in daily.iterrows()
        ]
    
    return trends


def build_win_rate_trends(df: pd.DataFrame) -> dict:
    """Build cumulative win rate trends per player."""
    if df.empty or 'date' not in df.columns:
        return {}
    
    working = df.copy()
    ensure_datetime(working)
    working = working.dropna(subset=['date'])
    if working.empty:
        return {}
    
    try:
        working['date_local'] = working['date'].dt.tz_convert(APP_TIMEZONE)
    except Exception:
        working['date_local'] = working['date']
    
    working['date_key'] = working['date_local'].dt.normalize()
    
    if 'outcome' in working.columns:
        outcome_lower = working['outcome'].astype(str).str.lower()
        working['win_flag'] = (outcome_lower == 'win').astype(int)
    else:
        working['win_flag'] = 0
    working['game_flag'] = 1
    
    trends = {}
    for player in unique_sorted(working['player_gamertag']):
        player_df = working[working['player_gamertag'] == player]
        daily = player_df.groupby('date_key')[['win_flag', 'game_flag']].sum().sort_index()
        if daily.empty:
            trends[player] = []
            continue
        
        cumulative = daily[['win_flag', 'game_flag']].cumsum()
        win_rate = (cumulative['win_flag'] / cumulative['game_flag'] * 100).fillna(0)
        
        trends[player] = [
            {'date': idx.strftime('%Y-%m-%d'), 'win_rate': float(value)}
            for idx, value in win_rate.items()
        ]
    
    return trends


def build_activity_heatmap(df: pd.DataFrame) -> list[dict]:
    """Build activity heatmap data by weekday and hour."""
    if df.empty or 'date' not in df.columns:
        return []
    
    working = df.copy()
    ensure_datetime(working)
    working = working.dropna(subset=['date'])
    if working.empty:
        return []
    
    try:
        working['date_local'] = working['date'].dt.tz_convert(APP_TIMEZONE)
    except Exception:
        working['date_local'] = working['date']
    
    working['day_idx'] = working['date_local'].dt.dayofweek
    working['hour'] = working['date_local'].dt.hour
    
    counts = working.groupby(['day_idx', 'hour']).size().to_dict()
    max_count = max(counts.values()) if counts else 0
    
    day_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    rows = []
    for day_idx, day_name in enumerate(day_names):
        hours = []
        for hour in range(24):
            count = int(counts.get((day_idx, hour), 0))
            if max_count > 0 and count > 0:
                intensity = 0.1 + 0.9 * (count / max_count)
            else:
                intensity = 0
            hours.append({
                'hour': hour,
                'count': count,
                'intensity': round(intensity, 3)
            })
        rows.append({'day': day_name, 'hours': hours})
    
    return rows


def build_win_corr(df: pd.DataFrame, limit: int = 20) -> list[dict]:
    """Build win correlation rows for stats vs win flag."""
    if df.empty or 'outcome' not in df.columns:
        return []
    
    working = df.copy()
    outcome_lower = working['outcome'].astype(str).str.lower()
    mask = outcome_lower.isin(['win', 'loss'])
    working = working[mask]
    outcome_lower = outcome_lower[mask]
    
    if working.empty:
        return []
    
    win_flag = (outcome_lower == 'win').astype(int)
    if win_flag.nunique() < 2:
        return []
    
    rows = []
    
    def add_corr(label: str, series: pd.Series) -> None:
        values = pd.to_numeric(series, errors='coerce')
        if values.dropna().empty or values.nunique(dropna=True) < 2:
            return
        corr = values.corr(win_flag)
        if pd.isna(corr):
            return
        rows.append({'stat': label, 'corr': float(corr)})
    
    if 'kills' in working.columns:
        add_corr('Kills', working['kills'])
    if 'deaths' in working.columns:
        add_corr('Deaths', working['deaths'])
    if 'assists' in working.columns:
        add_corr('Assists', working['assists'])
    if 'kda' in working.columns:
        add_corr('KDA', working['kda'])
    
    if 'shots_fired' in working.columns and 'shots_hit' in working.columns:
        fired = pd.to_numeric(working['shots_fired'], errors='coerce')
        hit = pd.to_numeric(working['shots_hit'], errors='coerce')
        acc = pd.Series(0.0, index=working.index)
        nonzero = fired > 0
        acc.loc[nonzero] = hit[nonzero] / fired[nonzero] * 100
        add_corr('Accuracy', acc)
    elif 'accuracy' in working.columns:
        acc = pd.to_numeric(working['accuracy'], errors='coerce')
        if acc.dropna().max() <= 1:
            acc = acc * 100
        add_corr('Accuracy', acc)
    
    if 'damage_dealt' in working.columns:
        add_corr('Damage Dealt', working['damage_dealt'])
    if 'damage_taken' in working.columns:
        add_corr('Damage Taken', working['damage_taken'])
    
    if 'dmg/min' in working.columns:
        add_corr('DMG/Min', working['dmg/min'])
    elif 'damage_dealt' in working.columns and 'duration' in working.columns:
        damage_dealt = pd.to_numeric(working['damage_dealt'], errors='coerce')
        duration = pd.to_numeric(working['duration'], errors='coerce')
        dmg_per_min = pd.Series(0.0, index=working.index)
        nonzero = duration > 0
        dmg_per_min.loc[nonzero] = damage_dealt[nonzero] / (duration[nonzero] / 60.0)
        add_corr('DMG/Min', dmg_per_min)
    
    if 'dmg_difference' in working.columns:
        add_corr('Damage Diff', working['dmg_difference'])
    elif 'damage_dealt' in working.columns and 'damage_taken' in working.columns:
        damage_dealt = pd.to_numeric(working['damage_dealt'], errors='coerce')
        damage_taken = pd.to_numeric(working['damage_taken'], errors='coerce')
        add_corr('Damage Diff', damage_dealt - damage_taken)
    
    score_vals = score_series(working)
    if not score_vals.empty:
        add_corr('Personal Score', score_vals)
    
    obj_scores = objective_score_series(working)
    if not obj_scores.empty:
        add_corr('Objective Score', obj_scores)
    
    if 'medal_count' in working.columns:
        add_corr('Medals', working['medal_count'])
    if 'headshot_kills' in working.columns:
        add_corr('Headshots', working['headshot_kills'])
    if 'melee_kills' in working.columns:
        add_corr('Melee Kills', working['melee_kills'])
    if 'grenade_kills' in working.columns:
        add_corr('Grenade Kills', working['grenade_kills'])
    if 'power_weapon_kills' in working.columns:
        add_corr('Power Weapon Kills', working['power_weapon_kills'])
    if 'callout_assists' in working.columns:
        add_corr('Callouts', working['callout_assists'])
    if 'average_life_duration' in working.columns:
        add_corr('Avg Life', working['average_life_duration'])
    
    rows.sort(key=lambda item: abs(item['corr']), reverse=True)
    return rows[:limit]


_WC_FMT = {'KDA': '{:.2f}', 'Accuracy': '{:.1f}%', 'Avg Life': '{:.1f}s', 'DMG/Min': '{:.0f}',
           'Damage Dealt': '{:.0f}', 'Damage Taken': '{:.0f}', 'Damage Diff': '{:+.0f}',
           'Personal Score': '{:.0f}', 'Objective Score': '{:.0f}'}
# Stats where a LOWER number is the good direction.
_WC_LOWER_BETTER = {'Deaths', 'Damage Taken'}


def _win_corr_stat_value(df: pd.DataFrame, label: str):
    """Per-game average for a build_win_corr stat label (same definitions)."""
    try:
        if label == 'Accuracy':
            fired = numeric_series(df, 'shots_fired'); hit = numeric_series(df, 'shots_hit')
            return float(hit.sum() / fired.sum() * 100) if fired.sum() > 0 else None
        if label == 'Damage Diff':
            return float((numeric_series(df, 'damage_dealt') - numeric_series(df, 'damage_taken')).mean())
        if label == 'DMG/Min':
            dd = numeric_series(df, 'damage_dealt'); dur = numeric_series(df, 'duration')
            return float(dd.sum() / (dur.sum() / 60)) if dur.sum() > 0 else None
        if label == 'Personal Score':
            s = score_series(df)
            return float(s.mean()) if len(s) else None
        if label == 'Objective Score':
            s = objective_score_series(df)
            s = s[s > 0]  # objective modes only — 0s from slayer would dilute
            return float(s.mean()) if len(s) else None
        col = {'Kills': 'kills', 'Deaths': 'deaths', 'Assists': 'assists', 'KDA': 'kda',
               'Damage Dealt': 'damage_dealt', 'Damage Taken': 'damage_taken',
               'Medals': 'medal_count', 'Headshots': 'headshot_kills',
               'Melee Kills': 'melee_kills', 'Grenade Kills': 'grenade_kills',
               'Power Weapon Kills': 'power_weapon_kills', 'Callouts': 'callout_assists',
               'Avg Life': 'average_life_duration'}.get(label)
        if not col or col not in df.columns:
            return None
        s = pd.to_numeric(df[col], errors='coerce').dropna()
        return float(s.mean()) if len(s) else None
    except Exception:
        return None


def build_win_corr_by_player(df: pd.DataFrame) -> dict:
    """Per-player win-correlation cards: each player's own top win-linked
    stats (their personal version of the overall list), their ACTUAL per-game
    level on each vs the squad's, plus plain-English analysis of what drives
    THEIR wins and where the real improvement lever is. Replaces the old
    grouped bar chart."""
    if df is None or df.empty or 'player_gamertag' not in df.columns:
        return {'players': []}
    squad_rows = build_win_corr(df, limit=100)
    squad_map = {r['stat']: r['corr'] for r in squad_rows}
    squad_top = next((r['stat'] for r in squad_rows if r['corr'] > 0), None)

    def _strength(r: float) -> str:
        a = abs(r)
        return 'strong' if a >= 0.4 else ('solid' if a >= 0.25 else ('mild' if a >= 0.12 else 'weak'))

    players_out = []
    for player in unique_sorted(df['player_gamertag']):
        pdf = df[df['player_gamertag'] == player]
        n = len(pdf)
        rows = build_win_corr(pdf, limit=100) if n >= 50 else []
        card = {'player': player, 'cls': get_player_class(player), 'games': n,
                'core': [], 'extra': [], 'drivers': [], 'drags': [], 'analysis': []}
        if n < 50 or not rows:
            card['analysis'].append(
                f"Only {n} games in this window — not enough for reliable correlations.")
            players_out.append(card)
            continue
        # Core stats every card shows in the same order (comparable across
        # players — Damage Diff etc. can't silently drop off a card just
        # because it ranks low for one player), plus the next few notables.
        _CORE = ['KDA', 'Damage Diff', 'Avg Life', 'Objective Score', 'Deaths']
        _by_stat = {r['stat']: r for r in rows}
        core = [dict(_by_stat[s]) for s in _CORE if s in _by_stat]
        extra = [dict(r) for r in rows if r['stat'] not in _CORE][:3]

        # drivers/drags still feed the analysis sentences below.
        drivers = [dict(r) for r in rows if r['corr'] > 0][:5]
        drags = sorted((dict(r) for r in rows if r['corr'] < 0), key=lambda r: r['corr'])[:3]

        # Attach the player's ACTUAL per-game level vs the squad's for every
        # listed stat — correlation says which stat matters; the level says
        # whether it's a strength to lean on or a gap to close.
        _sq_cache: dict = {}

        def _levels(label):
            pv = _win_corr_stat_value(pdf, label)
            if label not in _sq_cache:
                _sq_cache[label] = _win_corr_stat_value(df, label)
            return pv, _sq_cache[label]

        def _fmt(label, v):
            return _WC_FMT.get(label, '{:.1f}').format(v)

        for r in core + extra + drivers + drags:
            r['strength'] = _strength(r['corr'])
            pv, sv = _levels(r['stat'])
            r['pv'] = _fmt(r['stat'], pv) if pv is not None else ''
            r['sv'] = _fmt(r['stat'], sv) if sv is not None else ''
            r['_pv'], r['_sv'] = pv, sv
        card['core'], card['extra'] = core, extra
        card['drivers'], card['drags'] = drivers, drags

        def _above(label, pv, sv, margin=0.02):
            """Is the player meaningfully on the GOOD side of the squad avg?"""
            if pv is None or sv is None or sv == 0:
                return None
            rel = (pv - sv) / abs(sv)
            if abs(rel) < margin:
                return None
            good = rel < 0 if label in _WC_LOWER_BETTER else rel > 0
            return good

        a = card['analysis']
        pm = {r['stat']: r['corr'] for r in rows}
        if drivers and drivers[0]['corr'] >= 0.15:
            top = drivers[0]
            a.append(f"Wins follow their {top['stat']} (r {top['corr']:+.2f}, {top['strength']}) — "
                     f"the clearest tell in their game.")
            side = _above(top['stat'], top['_pv'], top['_sv'])
            if side is True:
                a.append(f"They already run above squad average there ({top['pv']} vs {top['sv']}) — "
                         f"a genuine strength to keep leaning on.")
            elif side is False:
                a.append(f"And they sit BELOW squad average on it ({top['pv']} vs {top['sv']}) — "
                         f"that's the biggest lever: pulling their {top['stat']} up to squad level "
                         f"should convert directly into wins.")
            if squad_top and top['stat'] != squad_top:
                a.append(f"That differs from the squad overall, where {squad_top} matters most — "
                         f"their wins have their own recipe.")
        else:
            a.append("No single stat reliably predicts their wins in this window — results look "
                     "team-driven rather than tied to one part of their game.")
        # A second lever: a real driver (not the top one) where they lag the squad.
        for d in drivers[1:]:
            if d['corr'] >= 0.15 and _above(d['stat'], d['_pv'], d['_sv']) is False:
                a.append(f"Also worth work: {d['stat']} moves their wins too (r {d['corr']:+.2f}) "
                         f"and they trail the squad there ({d['pv']} vs {d['sv']}).")
                break
        if drags and drags[0]['corr'] <= -0.25:
            d = drags[0]
            line = (f"The flip side: {d['stat']} is the biggest drag (r {d['corr']:+.2f}) — "
                    f"when it climbs, they lose.")
            side = _above(d['stat'], d['_pv'], d['_sv'])
            if side is False:
                line += f" And they run worse than squad average on it ({d['pv']} vs {d['sv']})."
            elif side is True:
                line += (f" Their baseline is actually better than squad average ({d['pv']} vs "
                         f"{d['sv']}) — the drag is about their bad nights, not their norm.")
            if d['stat'] == 'Deaths' and (not drivers or abs(d['corr']) > abs(drivers[0]['corr'])):
                line += " Keeping deaths down beats chasing kills."
            a.append(line)
        # Style read — compares two CORE rows (KDA vs Objective Score) so every
        # number quoted here is visible on the card above.
        k, o = pm.get('KDA'), pm.get('Objective Score')
        if k is not None and o is not None and max(abs(k), abs(o)) >= 0.15:
            opv, osv = _levels('Objective Score')
            obj_note = ''
            if opv is not None and osv is not None:
                obj_note = f" (they average {_fmt('Objective Score', opv)} obj score vs squad {_fmt('Objective Score', osv)})"
            if o - k >= 0.1:
                a.append(f"Objective Score links to their wins more than KDA does "
                         f"({o:+.2f} vs {k:+.2f}){obj_note} — they win by playing the mode.")
            elif k - o >= 0.1:
                a.append(f"KDA links to their wins more than Objective Score does "
                         f"({k:+.2f} vs {o:+.2f}) — they win through gunfights.")
        players_out.append(card)
    return {'players': players_out}


def build_player_moments(df: pd.DataFrame) -> dict:
    if df.empty or 'player_gamertag' not in df.columns or 'date' not in df.columns:
        return {}
    
    working = df.copy()
    ensure_datetime(working)
    working = working.dropna(subset=['date'])
    if working.empty:
        return {}
    
    if 'date_local' not in working.columns:
        try:
            working['date_local'] = working['date'].dt.tz_convert(APP_TIMEZONE)
        except Exception:
            working['date_local'] = working['date']
    if 'date_str' not in working.columns:
        working['date_str'] = working['date_local'].dt.strftime('%Y-%m-%d')
    
    if 'kda' not in working.columns:
        kills = numeric_series(working, 'kills')
        deaths = numeric_series(working, 'deaths')
        assists = numeric_series(working, 'assists')
        working['kda'] = kills + assists / 3 - deaths
    if 'win_rate' not in working.columns and 'outcome' in working.columns:
        outcome_lower = working['outcome'].astype(str).str.lower()
        working['win_rate'] = (outcome_lower == 'win').astype(float) * 100
    if 'obj_score' not in working.columns:
        working['obj_score'] = objective_score_series(working)
    if 'dmg_diff' not in working.columns:
        working['dmg_diff'] = numeric_series(working, 'damage_dealt') - numeric_series(working, 'damage_taken')
    if 'dmg_min' not in working.columns:
        damage_dealt = numeric_series(working, 'damage_dealt')
        duration = numeric_series(working, 'duration')
        duration_min = duration / 60.0
        working['dmg_min'] = 0.0
        nonzero = duration_min > 0
        working.loc[nonzero, 'dmg_min'] = damage_dealt[nonzero] / duration_min[nonzero]
    
    daily_player = (
        working.groupby(['date_str', 'player_gamertag'])
        .agg(
            win_rate=('win_rate', 'mean'),
            kda=('kda', 'mean'),
            obj_score=('obj_score', 'mean'),
            dmg_min=('dmg_min', 'mean'),
            dmg_diff=('dmg_diff', 'mean')
        )
        .reset_index()
    )
    
    heroics_by_player = {}
    for date_str, group in daily_player.groupby('date_str'):
        if group.empty:
            continue
        temp = group.copy()
        temp['kda_rank'] = temp['kda'].rank(method='min', ascending=False)
        temp['dmg_rank'] = temp['dmg_diff'].rank(method='min', ascending=False)
        heroics = temp[(temp['kda_rank'] <= 3) & (temp['dmg_rank'] <= 3)]
        for _, row in heroics.iterrows():
            heroics_by_player.setdefault(row['player_gamertag'], []).append((date_str, row['kda']))
    
    moments = {}
    limits = {
        'tilt': 3,
        'tilt_window': 10,
        'clutch': 4,
        'carry': 5,
        'heroic': 5,
        'objective': 5,
        'silent': 5,
        'momentum': 3,
        'rivalry': 6
    }
    
    for player in unique_sorted(working['player_gamertag']):
        player_df = working[working['player_gamertag'] == player].sort_values('date')
        if player_df.empty:
            continue
        
        daily_stats = daily_player[daily_player['player_gamertag'] == player].set_index('date_str')
        events = []
        counts = {key: 0 for key in limits}
        seen = set()
        
        def lookup_value(date_key: str, stat: str, fallback: float | None) -> float | None:
            if date_key in daily_stats.index and stat in daily_stats.columns:
                val = daily_stats.at[date_key, stat]
                if pd.notna(val):
                    return float(val)
            if fallback is None or pd.isna(fallback):
                return None
            return float(fallback)
        
        def add_event(date_key: str, stat: str, label: str, event_type: str, value: float | None) -> None:
            if not date_key or event_type not in limits:
                return
            if counts[event_type] >= limits[event_type]:
                return
            if value is None or pd.isna(value):
                return
            key = (date_key, stat, label, event_type)
            if key in seen:
                return
            seen.add(key)
            events.append({
                'date': date_key,
                'stat': stat,
                'label': label,
                'type': event_type,
                'value': float(value)
            })
            counts[event_type] += 1
        
        outcomes = player_df['outcome'].astype(str).str.lower().tolist() if 'outcome' in player_df.columns else []
        dates = player_df['date_str'].tolist()
        for i in range(len(outcomes) - 2):
            if counts['tilt'] >= limits['tilt']:
                break
            if outcomes[i] == outcomes[i + 1] == outcomes[i + 2] == 'loss' and (i == 0 or outcomes[i - 1] != 'loss'):
                date_key = dates[i]
                value = lookup_value(date_key, 'win_rate', None)
                add_event(date_key, 'win_rate', 'Tilt start (3L)', 'tilt', value)
                for j in range(1, 6):
                    if i + j >= len(dates):
                        break
                    date_window = dates[i + j]
                    value_window = lookup_value(date_window, 'win_rate', None)
                    add_event(date_window, 'win_rate', 'Tilt window', 'tilt_window', value_window)
        
        if 'team_score' in player_df.columns and 'enemy_team_score' in player_df.columns and 'outcome' in player_df.columns:
            team_score = pd.to_numeric(player_df['team_score'], errors='coerce').fillna(0)
            enemy_score = pd.to_numeric(player_df['enemy_team_score'], errors='coerce').fillna(0)
            close_mask = (team_score - enemy_score).abs() <= 5
            close_df = player_df[close_mask].copy()
            if not close_df.empty:
                close_outcome = close_df['outcome'].astype(str).str.lower()
                close_df = close_df[close_outcome.isin(['win', 'loss'])]
                if not close_df.empty:
                    close_df['win_flag'] = (close_df['outcome'].astype(str).str.lower() == 'win').astype(int)
                    daily_close = close_df.groupby('date_str')['win_flag'].agg(['mean', 'count']).reset_index()
                    daily_close['win_rate'] = daily_close['mean'] * 100
                    daily_close['date_dt'] = pd.to_datetime(daily_close['date_str'], errors='coerce')
                    daily_close = daily_close.dropna(subset=['date_dt']).sort_values('date_dt')
                    daily_close = daily_close.set_index('date_dt')
                    daily_close['rolling'] = daily_close['win_rate'].rolling('30D', min_periods=1).mean()
                    daily_close = daily_close.reset_index()
                    for _, row in daily_close.iterrows():
                        if counts['clutch'] >= limits['clutch']:
                            break
                        if row['count'] < 2:
                            continue
                        diff = row['win_rate'] - row['rolling']
                        if diff >= 20:
                            label = f'Clutch spike (+{diff:.0f}%)'
                            value = lookup_value(row['date_str'], 'win_rate', row['win_rate'])
                            add_event(row['date_str'], 'win_rate', label, 'clutch', value)
        
        if 'team_damage_dealt' in player_df.columns and 'damage_dealt' in player_df.columns:
            team_damage = pd.to_numeric(player_df['team_damage_dealt'], errors='coerce').fillna(0)
            damage = pd.to_numeric(player_df['damage_dealt'], errors='coerce').fillna(0)
            share = pd.Series(0.0, index=player_df.index)
            nonzero = team_damage > 0
            share.loc[nonzero] = damage[nonzero] / team_damage[nonzero]
            carry_df = player_df[share >= 0.45].copy()
            if not carry_df.empty:
                carry_df['share'] = share.loc[carry_df.index]
                carry_df = carry_df.sort_values('share', ascending=False).head(limits['carry'])
                for _, row in carry_df.iterrows():
                    label = f'Carry day ({row["share"] * 100:.0f}% team dmg)'
                    dmg_min = row.get('dmg_min')
                    value = lookup_value(row['date_str'], 'dmg_min', dmg_min)
                    add_event(row['date_str'], 'dmg_min', label, 'carry', value)
        
        for date_key, kda_val in heroics_by_player.get(player, [])[:limits['heroic']]:
            value = lookup_value(date_key, 'kda', kda_val)
            add_event(date_key, 'kda', 'Heroics day (top 3 KDA + dmg diff)', 'heroic', value)
        
        obj_scores = pd.to_numeric(player_df.get('obj_score', 0), errors='coerce').fillna(0)
        obj_median = obj_scores[obj_scores > 0].median() if not obj_scores.empty else 0
        if obj_median and obj_median > 0:
            anchor_df = player_df[obj_scores >= (2 * obj_median)].copy()
            if not anchor_df.empty:
                anchor_df['obj_score'] = obj_scores.loc[anchor_df.index]
                anchor_df = anchor_df.sort_values('obj_score', ascending=False).head(limits['objective'])
                for _, row in anchor_df.iterrows():
                    label = f'Objective anchor ({row["obj_score"]:.0f})'
                    value = lookup_value(row['date_str'], 'obj_score', row['obj_score'])
                    add_event(row['date_str'], 'obj_score', label, 'objective', value)
        
        if 'callout_assists' in player_df.columns:
            kda_vals = pd.to_numeric(player_df.get('kda', 0), errors='coerce').fillna(0)
            threshold = kda_vals.quantile(0.9) if not kda_vals.empty else None
            if threshold is not None:
                silent_df = player_df[(pd.to_numeric(player_df['callout_assists'], errors='coerce').fillna(0) <= 0) & (kda_vals >= threshold)].copy()
                if not silent_df.empty:
                    silent_df['kda'] = kda_vals.loc[silent_df.index]
                    silent_df = silent_df.sort_values('kda', ascending=False).head(limits['silent'])
                    for _, row in silent_df.iterrows():
                        label = f'Silent assassin (KDA {row["kda"]:.2f})'
                        value = lookup_value(row['date_str'], 'kda', row['kda'])
                        add_event(row['date_str'], 'kda', label, 'silent', value)
        
        if outcomes:
            session_ids = []
            session = 0
            last_ts = None
            for ts in player_df['date']:
                if last_ts is not None and ts - last_ts > pd.Timedelta(minutes=SESSION_GAP_MINUTES):
                    session += 1
                session_ids.append(session)
                last_ts = ts
            session_df_all = player_df.copy()
            session_df_all['session_id'] = session_ids
            for _, session_df in session_df_all.groupby('session_id'):
                if counts['momentum'] >= limits['momentum']:
                    break
                if len(session_df) < 6:
                    continue
                session_df = session_df.sort_values('date')
                mid = len(session_df) // 2
                first_half = session_df.iloc[:mid]
                second_half = session_df.iloc[mid:]
                first_wins = (first_half['outcome'].astype(str).str.lower() == 'win').sum()
                second_wins = (second_half['outcome'].astype(str).str.lower() == 'win').sum()
                first_rate = first_wins / len(first_half) * 100 if len(first_half) else 0
                second_rate = second_wins / len(second_half) * 100 if len(second_half) else 0
                if first_rate < 40 and second_rate > 60:
                    date_key = session_df.iloc[0]['date_str']
                    label = f'Momentum flip ({first_rate:.0f}% -> {second_rate:.0f}%)'
                    value = lookup_value(date_key, 'win_rate', None)
                    add_event(date_key, 'win_rate', label, 'momentum', value)
        
        if 'map' in player_df.columns and 'outcome' in player_df.columns:
            overall_games = len(player_df)
            if overall_games:
                overall_wins = (player_df['outcome'].astype(str).str.lower() == 'win').sum()
                overall_win_pct = overall_wins / overall_games * 100
                map_df = player_df.copy()
                map_df['_map_name'] = map_df['map'].map(normalize_map_name)
                map_stats = []
                for map_name, group in map_df.groupby('_map_name'):
                    if not map_name:
                        continue
                    games = len(group)
                    if games < 5:
                        continue
                    wins = (group['outcome'].astype(str).str.lower() == 'win').sum()
                    win_pct = wins / games * 100 if games else 0
                    diff = win_pct - overall_win_pct
                    if abs(diff) >= 20:
                        map_stats.append((map_name, diff))
                map_stats.sort(key=lambda item: abs(item[1]), reverse=True)
                for map_name, diff in map_stats[:2]:
                    map_rows = map_df[map_df['_map_name'] == map_name].sort_values('date').head(3)
                    for _, row in map_rows.iterrows():
                        label = f'Rivalry map {map_name} ({diff:+.0f}%)'
                        value = lookup_value(row['date_str'], 'win_rate', None)
                        add_event(row['date_str'], 'win_rate', label, 'rivalry', value)
        
        if events:
            moments[player] = events
    
    return moments


def build_lineup_stats(df: pd.DataFrame, stack_size: int, min_games: int = 5, limit: int = 15) -> list[dict]:
    if df.empty or 'match_id' not in df.columns or 'player_gamertag' not in df.columns:
        return []
    if 'team_id' not in df.columns:
        return []
    if stack_size < 2 or stack_size > 4:
        return []
    
    working = df.copy()
    lineup_totals = {}
    
    grouped = working.groupby(['match_id', 'team_id'])
    for _, group in grouped:
        players = unique_sorted(group['player_gamertag'])
        if len(players) < stack_size:
            continue
        outcome = str(group['outcome'].iloc[0]).strip().lower() if 'outcome' in group.columns else ''
        win_flag = 1 if outcome == 'win' else 0
        
        per_player = {}
        for player in players:
            player_rows = group[group['player_gamertag'] == player]
            if player_rows.empty:
                continue
            kills = numeric_series(player_rows, 'kills').sum()
            deaths = numeric_series(player_rows, 'deaths').sum()
            assists = numeric_series(player_rows, 'assists').sum()
            kda = safe_kda(kills, assists, deaths)
            
            fired = numeric_series(player_rows, 'shots_fired').sum()
            hit = numeric_series(player_rows, 'shots_hit').sum()
            if fired > 0:
                accuracy = hit / fired * 100
            else:
                accuracy = pd.to_numeric(player_rows.get('accuracy', 0), errors='coerce').fillna(0).mean()
                if accuracy <= 1:
                    accuracy *= 100
            
            obj_score = objective_score_series(player_rows).sum()
            dmg_diff = numeric_series(player_rows, 'damage_dealt').sum() - numeric_series(player_rows, 'damage_taken').sum()
            score = score_series(player_rows).sum()
            
            per_player[player] = {
                'kda': kda,
                'accuracy': accuracy,
                'obj_score': obj_score,
                'dmg_diff': dmg_diff,
                'score': score
            }
        
        if len(per_player) < stack_size:
            continue
        
        for lineup in combinations(sorted(per_player.keys()), stack_size):
            metrics = [per_player[player] for player in lineup]
            entry = lineup_totals.setdefault(lineup, {
                'games': 0,
                'wins': 0,
                'kda': 0.0,
                'accuracy': 0.0,
                'obj_score': 0.0,
                'dmg_diff': 0.0,
                'score': 0.0
            })
            entry['games'] += 1
            entry['wins'] += win_flag
            entry['kda'] += sum(item['kda'] for item in metrics) / stack_size
            entry['accuracy'] += sum(item['accuracy'] for item in metrics) / stack_size
            entry['obj_score'] += sum(item['obj_score'] for item in metrics) / stack_size
            entry['dmg_diff'] += sum(item['dmg_diff'] for item in metrics) / stack_size
            entry['score'] += sum(item['score'] for item in metrics) / stack_size
    
    rows = []
    for lineup, totals in lineup_totals.items():
        games = totals['games']
        if games < min_games:
            continue
        win_pct = totals['wins'] / games * 100 if games else 0
        rows.append({
            'players': list(lineup),
            'lineup': ' + '.join(lineup),
            'games': format_int(games),
            'wins': format_int(totals['wins']),
            'win_pct': format_float(win_pct, 1),
            'kda': format_float(totals['kda'] / games if games else 0, 2),
            'accuracy': format_float(totals['accuracy'] / games if games else 0, 1),
            'obj_score': format_float(totals['obj_score'] / games if games else 0, 1),
            'dmg_diff': format_signed(totals['dmg_diff'] / games if games else 0, 0),
            'score': format_float(totals['score'] / games if games else 0, 0)
        })
    
    add_heatmap_classes(rows, {
        'win_pct': True,
        'kda': True,
        'accuracy': True,
        'obj_score': True,
        'dmg_diff': True,
        'score': True
    })
    add_composite_grades(rows, {
        'win_pct': True,
        'kda': True,
        'accuracy': True,
        'obj_score': True,
        'dmg_diff': True,
        'score': True
    }, 'Lineup grade')
    
    rows.sort(key=lambda r: (
        to_number(r.get('win_pct')) or 0,
        to_number(r.get('kda')) or 0,
        to_number(r.get('games')) or 0
    ), reverse=True)
    
    return rows[:limit]


def build_player_hover_data(df: pd.DataFrame) -> dict:
    if df.empty or 'player_gamertag' not in df.columns:
        return {}
    
    now = time.time()
    cached = PLAYER_HOVER_CACHE.get('payload')
    if cached and now - PLAYER_HOVER_CACHE['last_ts'] < PLAYER_HOVER_CACHE_TTL:
        return cached
    
    working = df.copy()
    if 'date' in working.columns:
        ensure_datetime(working)
        working = working.dropna(subset=['date'])
    if working.empty:
        return {}
    
    payload = {}
    for player in unique_sorted(working['player_gamertag']):
        player_df = working[working['player_gamertag'] == player]
        if player_df.empty:
            continue
        games = len(player_df)
        outcomes = player_df['outcome'].astype(str).str.lower() if 'outcome' in player_df.columns else pd.Series()
        wins = (outcomes == 'win').sum() if not outcomes.empty else 0
        win_pct = wins / games * 100 if games else 0
        
        kills = numeric_series(player_df, 'kills').sum()
        deaths = numeric_series(player_df, 'deaths').sum()
        assists = numeric_series(player_df, 'assists').sum()
        kda = safe_kda(kills / games if games else 0, assists / games if games else 0, deaths / games if games else 0)
        
        player_df = player_df.sort_values('date', ascending=True)
        csr_vals = extract_csr_values(player_df)
        current_csr = csr_vals.iloc[-1] if not csr_vals.empty else None
        last_match = player_df['date'].max() if 'date' in player_df.columns else None
        
        payload[player.lower()] = {
            'player': player,
            'games': format_int(games),
            'win_pct': format_float(win_pct, 1),
            'kda': format_float(kda, 2),
            'csr': format_float(current_csr, 1) if current_csr is not None and not pd.isna(current_csr) else '-',
            'last_match': format_date(last_match)
        }
    
    PLAYER_HOVER_CACHE['payload'] = payload
    PLAYER_HOVER_CACHE['last_ts'] = now
    return payload


def load_insights_cache() -> dict | None:
    try:
        if not INSIGHTS_CACHE_PATH.exists():
            return None
        with open(INSIGHTS_CACHE_PATH, 'r') as file:
            data = json.load(file)
        if not isinstance(data, dict):
            return None
        created_ts = data.get('created_ts')
        payload = data.get('payload')
        version = data.get('version')
        if not isinstance(created_ts, (int, float)) or not isinstance(payload, dict):
            return None
        if version not in (None, INSIGHTS_CACHE_VERSION):
            return None
        if INSIGHTS_CACHE_DISK_TTL > 0:
            age = time.time() - float(created_ts)
            if age > INSIGHTS_CACHE_DISK_TTL:
                return None
        return ensure_insights_payload_grades(payload)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning('Failed to load insights cache: %s', exc)
        return None


def save_insights_cache(payload: dict) -> None:
    try:
        data = {
            'created_ts': time.time(),
            'version': INSIGHTS_CACHE_VERSION,
            'payload': payload
        }
        with open(INSIGHTS_CACHE_PATH, 'w') as file:
            json.dump(data, file)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning('Failed to save insights cache: %s', exc)
        return None


def ensure_insights_payload_grades(payload: dict) -> dict:
    grade_configs = {
        'clutch_rows': ({'clutch_index': True}, 'Clutch grade'),
        'role_rows': ({'slayer_score': True, 'obj_score': True, 'support_score': True}, 'Role grade'),
        'momentum_rows': ({'csr_delta': True}, 'Momentum grade'),
        'veto_rows': ({'best_win_pct': True, 'worst_win_pct': False}, 'Map veto grade'),
        'consistency_rows': ({'consistency': True}, 'Consistency grade'),
        'change_rows': ({'win_delta': True, 'kda_delta': True}, 'Change grade'),
        'lineup2_rows': ({
            'win_pct': True,
            'kda': True,
            'accuracy': True,
            'obj_score': True,
            'dmg_diff': True,
            'score': True
        }, 'Lineup grade'),
        'lineup3_rows': ({
            'win_pct': True,
            'kda': True,
            'accuracy': True,
            'obj_score': True,
            'dmg_diff': True,
            'score': True
        }, 'Lineup grade'),
        'lineup4_rows': ({
            'win_pct': True,
            'kda': True,
            'accuracy': True,
            'obj_score': True,
            'dmg_diff': True,
            'score': True
        }, 'Lineup grade')
    }
    for key, (columns, label) in grade_configs.items():
        rows = payload.get(key)
        if isinstance(rows, list):
            add_composite_grades(rows, columns, label)
    return payload


def get_insights_payload(ranked_df: pd.DataFrame) -> dict:
    if ranked_df.empty:
        return {
            'clutch_rows': [],
            'role_rows': [],
            'momentum_rows': [],
            'veto_rows': [],
            'consistency_rows': [],
            'notable_rows': [],
            'change_rows': [],
            'lineup2_rows': [],
            'lineup3_rows': [],
            'lineup4_rows': []
        }
    
    now = time.time()
    cached = INSIGHTS_CACHE.get('payload')
    if cached and now - INSIGHTS_CACHE['last_ts'] < INSIGHTS_CACHE_TTL:
        return cached

    def _compute():
        payload = {
            'clutch_rows': build_clutch_index(ranked_df),
            'role_rows': build_role_heatmap(ranked_df),
            'momentum_rows': build_momentum_rows(ranked_df),
            'veto_rows': build_map_veto_hints(ranked_df, MAP_VETO_MIN_GAMES),
            'consistency_rows': build_consistency_rows(ranked_df),
            'notable_rows': build_notable_games(ranked_df),
            'change_rows': build_change_summary(ranked_df),
            'lineup2_rows': build_lineup_stats(ranked_df, 2),
            'lineup3_rows': build_lineup_stats(ranked_df, 3),
            'lineup4_rows': build_lineup_stats(ranked_df, 4)
        }
        INSIGHTS_CACHE['payload'] = payload
        INSIGHTS_CACHE['last_ts'] = time.time()
        save_insights_cache(payload)
        return payload

    # This build takes 40-100s — NEVER run it on a request when anything exists
    # to serve: stale memory copy → serve + refresh in background (single-flight).
    if cached:
        _spawn_page_rebuild('_insights', _compute, None)
        return cached

    disk_payload = load_insights_cache()
    if disk_payload:
        INSIGHTS_CACHE['payload'] = disk_payload
        INSIGHTS_CACHE['last_ts'] = now
        return disk_payload

    return _compute()  # true first-ever build only


def parse_date_bound(value: str, is_end: bool) -> pd.Timestamp | None:
    if not value:
        return None
    ts = pd.to_datetime(value, errors='coerce')
    if pd.isna(ts):
        return None
    if ts.tzinfo is None:
        try:
            ts = ts.tz_localize(APP_TIMEZONE)
        except Exception:
            ts = ts.tz_localize('UTC')
    if is_end:
        ts = ts + pd.Timedelta(days=1)
    try:
        return ts.tz_convert('UTC')
    except Exception:
        return ts


def apply_date_range(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    if df.empty or 'date' not in df.columns:
        return df
    start_ts = parse_date_bound(start, False)
    end_ts = parse_date_bound(end, True)
    date_series = pd.to_datetime(df['date'], errors='coerce', utc=True)
    mask = date_series.notna()
    if start_ts is not None:
        mask &= date_series >= start_ts
    if end_ts is not None:
        mask &= date_series < end_ts
    return df[mask]


def summarize_stats(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}
    games = len(df)
    outcomes = df['outcome'].astype(str).str.lower() if 'outcome' in df.columns else pd.Series()
    wins = (outcomes == 'win').sum() if not outcomes.empty else 0
    win_rate = wins / games * 100 if games else 0
    
    total_kills = numeric_series(df, 'kills').sum()
    total_deaths = numeric_series(df, 'deaths').sum()
    total_assists = numeric_series(df, 'assists').sum()
    kills_pg = total_kills / games if games else 0
    deaths_pg = total_deaths / games if games else 0
    assists_pg = total_assists / games if games else 0
    kda = safe_kda(kills_pg, assists_pg, deaths_pg)
    
    fired = numeric_series(df, 'shots_fired').sum()
    hit = numeric_series(df, 'shots_hit').sum()
    if fired > 0:
        accuracy = hit / fired * 100
    else:
        accuracy = numeric_series(df, 'accuracy').mean()
        if accuracy <= 1:
            accuracy *= 100
    
    damage_dealt = numeric_series(df, 'damage_dealt').sum()
    damage_taken = numeric_series(df, 'damage_taken').sum()
    dmg_diff_pg = (damage_dealt - damage_taken) / games if games else 0
    duration = numeric_series(df, 'duration').sum()
    dmg_per_min = damage_dealt / (duration / 60) if duration > 0 else 0
    
    score_total = score_series(df).sum()
    score_pg = score_total / games if games else 0
    
    obj_scores = objective_score_series(df)
    obj_score_pg = obj_scores.sum() / games if not obj_scores.empty and games else 0
    
    return {
        'games': games,
        'win_rate': win_rate,
        'kda': kda,
        'kills_pg': kills_pg,
        'deaths_pg': deaths_pg,
        'assists_pg': assists_pg,
        'accuracy': accuracy,
        'dmg_per_min': dmg_per_min,
        'dmg_diff_pg': dmg_diff_pg,
        'score_pg': score_pg,
        'obj_score_pg': obj_score_pg
    }


def build_session_compare(
    df: pd.DataFrame,
    player: str,
    start_a: str,
    end_a: str,
    start_b: str,
    end_b: str
) -> list[dict]:
    if df.empty:
        return []
    working = df
    if player and player != 'all' and 'player_gamertag' in working.columns:
        working = working[working['player_gamertag'] == player]
    if working.empty:
        return []
    
    range_a = apply_date_range(working, start_a, end_a)
    range_b = apply_date_range(working, start_b, end_b)
    
    stats_a = summarize_stats(range_a)
    stats_b = summarize_stats(range_b)
    
    def safe(value):
        return value if value is not None else 0
    
    rows = []
    for key, label, fmt, delta_fmt in [
        ('games', 'Games', lambda v: format_int(v), lambda v: format_signed(v, 0)),
        ('win_rate', 'Win %', lambda v: format_float(v, 1), lambda v: format_signed(v, 1)),
        ('kda', 'KDA', lambda v: format_float(v, 2), lambda v: format_signed(v, 2)),
        ('kills_pg', 'Kills/Game', lambda v: format_float(v, 1), lambda v: format_signed(v, 1)),
        ('deaths_pg', 'Deaths/Game', lambda v: format_float(v, 1), lambda v: format_signed(v, 1)),
        ('assists_pg', 'Assists/Game', lambda v: format_float(v, 1), lambda v: format_signed(v, 1)),
        ('accuracy', 'Accuracy %', lambda v: format_float(v, 1), lambda v: format_signed(v, 1)),
        ('dmg_per_min', 'Damage/Min', lambda v: format_float(v, 0), lambda v: format_signed(v, 0)),
        ('dmg_diff_pg', 'Damage Diff/Game', lambda v: format_signed(v, 0), lambda v: format_signed(v, 0)),
        ('score_pg', 'Score/Game', lambda v: format_float(v, 0), lambda v: format_signed(v, 0)),
        ('obj_score_pg', 'Obj Score/Game', lambda v: format_float(v, 1), lambda v: format_signed(v, 1))
    ]:
        a_val = safe(stats_a.get(key)) if stats_a else 0
        b_val = safe(stats_b.get(key)) if stats_b else 0
        delta = b_val - a_val
        rows.append({
            'stat': label,
            'a': fmt(a_val),
            'b': fmt(b_val),
            'delta': delta_fmt(delta)
        })
    
    return rows


def build_clutch_index(df: pd.DataFrame) -> list[dict]:
    if df.empty or 'player_gamertag' not in df.columns:
        return []
    
    if 'team_score' not in df.columns or 'enemy_team_score' not in df.columns:
        return []
    
    working = df.copy()
    team_score = pd.to_numeric(working['team_score'], errors='coerce').fillna(0)
    enemy_score = pd.to_numeric(working['enemy_team_score'], errors='coerce').fillna(0)
    working['score_diff'] = (team_score - enemy_score).abs()
    close_games = working[working['score_diff'] <= 5]
    
    rows = []
    for player in unique_sorted(working['player_gamertag']):
        player_df = working[working['player_gamertag'] == player]
        if player_df.empty:
            continue
        close_df = close_games[close_games['player_gamertag'] == player]
        close_count = len(close_df)
        if close_count == 0:
            continue
        
        close_outcomes = close_df['outcome'].astype(str).str.lower() if 'outcome' in close_df.columns else pd.Series()
        close_wins = (close_outcomes == 'win').sum() if not close_outcomes.empty else 0
        close_win_pct = close_wins / close_count * 100
        
        close_stats = summarize_stats(close_df)
        overall_stats = summarize_stats(player_df)
        clutch_index = close_win_pct - overall_stats.get('win_rate', 0)
        
        rows.append({
            'player': player,
            'close_games': format_int(close_count),
            'close_win_pct': format_float(close_win_pct, 1),
            'close_kda': format_float(close_stats.get('kda', 0), 2),
            'clutch_index': format_signed(clutch_index, 1)
        })
    
    add_heatmap_classes(rows, {
        'close_games': True,
        'close_win_pct': True,
        'close_kda': True,
        'clutch_index': True
    })
    add_composite_grades(rows, {'clutch_index': True}, 'Clutch grade')
    rows.sort(key=lambda x: to_number(x.get('clutch_index')) or 0, reverse=True)
    return rows


def build_role_heatmap(df: pd.DataFrame) -> list[dict]:
    if df.empty or 'player_gamertag' not in df.columns:
        return []
    
    rows = []
    raw_scores = []
    for player in unique_sorted(df['player_gamertag']):
        player_df = df[df['player_gamertag'] == player]
        stats = summarize_stats(player_df)
        if not stats:
            continue
        
        games = stats.get('games', 1)
        kills_pg = stats.get('kills_pg', 0)
        dmg_pg = numeric_series(player_df, 'damage_dealt').sum() / games
        slayer_score = kills_pg + (dmg_pg / 1000)
        
        obj_score = stats.get('obj_score_pg', 0)
        assists_pg = stats.get('assists_pg', 0)
        callouts_pg = numeric_series(player_df, 'callout_assists').sum() / games
        support_score = assists_pg + callouts_pg
        
        raw_scores.append((player, slayer_score, obj_score, support_score))
    
    if not raw_scores:
        return []
    
    slayer_vals = [score[1] for score in raw_scores]
    obj_vals = [score[2] for score in raw_scores]
    support_vals = [score[3] for score in raw_scores]
    
    def normalize(value, values):
        min_val = min(values)
        max_val = max(values)
        if max_val == min_val:
            return 0.5
        return (value - min_val) / (max_val - min_val)
    
    for player, slayer_score, obj_score, support_score in raw_scores:
        role_map = {
            'Slayer': normalize(slayer_score, slayer_vals),
            'Objective': normalize(obj_score, obj_vals),
            'Support': normalize(support_score, support_vals)
        }
        role = max(role_map, key=role_map.get)
        
        rows.append({
            'player': player,
            'slayer_score': format_float(slayer_score, 2),
            'obj_score': format_float(obj_score, 2),
            'support_score': format_float(support_score, 2),
            'role': role
        })
    
    add_heatmap_classes(rows, {
        'slayer_score': True,
        'obj_score': True,
        'support_score': True
    })
    add_composite_grades(rows, {
        'slayer_score': True,
        'obj_score': True,
        'support_score': True
    }, 'Role grade')
    return rows


def build_momentum_rows(df: pd.DataFrame, limit: int = 10) -> list[dict]:
    if df.empty or 'date' not in df.columns:
        return []
    
    working = df.copy()
    ensure_datetime(working)
    working = working.dropna(subset=['date'])
    
    rows = []
    for player in unique_sorted(working['player_gamertag']):
        player_df = working[working['player_gamertag'] == player].sort_values('date', ascending=False)
        if player_df.empty:
            continue
        
        recent = player_df.head(limit)
        results = []
        for _, row in recent.iterrows():
            outcome = str(row.get('outcome', '')).lower()
            if outcome == 'win':
                results.append({'label': 'W', 'class': 'streak-win'})
            elif outcome == 'loss':
                results.append({'label': 'L', 'class': 'streak-loss'})
            else:
                results.append({'label': '-', 'class': 'streak-tie'})
        
        pre = pd.to_numeric(recent.get('pre_match_csr', 0), errors='coerce')
        post = pd.to_numeric(recent.get('post_match_csr', 0), errors='coerce')
        pre = pre[pre > 0]
        post = post[post > 0]
        csr_delta = 0
        if not pre.empty and not post.empty:
            csr_delta = post.iloc[0] - pre.iloc[-1]
        
        rows.append({
            'player': player,
            'recent_results': results,
            'csr_delta': format_signed(csr_delta, 0),
            'last_played': format_date(recent['date'].max())
        })
    
    add_composite_grades(rows, {'csr_delta': True}, 'Momentum grade')
    return rows


def build_map_veto_hints(df: pd.DataFrame, min_games: int = MAP_VETO_MIN_GAMES) -> list[dict]:
    if df.empty or 'map' not in df.columns:
        return []
    
    working = add_normalized_map_column(df)
    active = get_active_map_set()
    rows = []
    for player in unique_sorted(working['player_gamertag']):
        player_df = working[working['player_gamertag'] == player]
        if player_df.empty:
            continue

        entries = []
        for map_name in unique_sorted(player_df['_map_normalized']):
            if not map_name or _map_hidden(map_name, active):
                continue
            map_df = player_df[player_df['_map_normalized'] == map_name]
            games = len(map_df)
            if games < min_games:
                continue
            outcomes = map_df['outcome'].astype(str).str.lower() if 'outcome' in map_df.columns else pd.Series()
            wins = (outcomes == 'win').sum() if not outcomes.empty else 0
            win_pct = wins / games * 100 if games else 0
            entries.append({'map': map_name, 'games': games, 'win_pct': win_pct})
        
        if not entries:
            continue
        
        entries.sort(key=lambda x: x['win_pct'], reverse=True)
        best = entries[0]
        worst = entries[-1]
        
        rows.append({
            'player': player,
            'best_map': best['map'],
            'best_win_pct': format_float(best['win_pct'], 1),
            'best_games': format_int(best['games']),
            'worst_map': worst['map'],
            'worst_win_pct': format_float(worst['win_pct'], 1),
            'worst_games': format_int(worst['games'])
        })
    
    add_heatmap_classes(rows, {'best_win_pct': True, 'worst_win_pct': False})
    add_composite_grades(rows, {'best_win_pct': True, 'worst_win_pct': False}, 'Map veto grade')
    return rows


def build_consistency_rows(df: pd.DataFrame) -> list[dict]:
    if df.empty or 'player_gamertag' not in df.columns or 'date' not in df.columns:
        return []
    
    working = df.copy()
    ensure_datetime(working)
    working = working.dropna(subset=['date'])
    if working.empty:
        return []
    
    now = working['date'].max()
    rows = []
    for player in unique_sorted(working['player_gamertag']):
        player_df = working[working['player_gamertag'] == player]
        if player_df.empty:
            continue
        
        stats = {}
        for days, label in [(30, '30d'), (90, '90d')]:
            cutoff = now - pd.Timedelta(days=days)
            window_df = player_df[player_df['date'] >= cutoff]
            if window_df.empty:
                stats[f'csr_std_{label}'] = 0
                stats[f'kda_std_{label}'] = 0
                continue
            csr_vals = extract_csr_values(window_df)
            if csr_vals.empty:
                csr_vals = pd.to_numeric(window_df.get('pre_match_csr', 0), errors='coerce').dropna()
            kda_vals = pd.to_numeric(window_df.get('kda', pd.Series()), errors='coerce')
            if kda_vals.dropna().empty:
                kills = numeric_series(window_df, 'kills')
                deaths = numeric_series(window_df, 'deaths')
                assists = numeric_series(window_df, 'assists')
                kda_vals = kills + assists / 3 - deaths
            kda_vals = kda_vals.dropna()
            stats[f'csr_std_{label}'] = float(csr_vals.std(ddof=0)) if not csr_vals.empty else 0
            stats[f'kda_std_{label}'] = float(kda_vals.std(ddof=0)) if not kda_vals.empty else 0
        
        total_std = stats['csr_std_30d'] + stats['kda_std_30d'] + stats['csr_std_90d'] + stats['kda_std_90d']
        consistency = 100 / (1 + total_std) if total_std >= 0 else 0
        
        rows.append({
            'player': player,
            'csr_std_30d': format_float(stats['csr_std_30d'], 2),
            'kda_std_30d': format_float(stats['kda_std_30d'], 2),
            'csr_std_90d': format_float(stats['csr_std_90d'], 2),
            'kda_std_90d': format_float(stats['kda_std_90d'], 2),
            'consistency': format_float(consistency, 1)
        })
    
    add_heatmap_classes(rows, {
        'csr_std_30d': False,
        'kda_std_30d': False,
        'csr_std_90d': False,
        'kda_std_90d': False,
        'consistency': True
    })
    # Grade on the headline consistency score ONLY, so the grade column tracks
    # the score the table is sorted by (the std-dev columns are just detail).
    add_composite_grades(rows, {'consistency': True}, 'Consistency grade')
    rows.sort(key=lambda x: to_number(x.get('consistency')) or 0, reverse=True)
    return rows


def build_notable_games(df: pd.DataFrame, limit: int = NOTABLE_GAMES_LIMIT) -> list[dict]:
    if df.empty or 'date' not in df.columns:
        return []
    
    working = df.copy()
    ensure_datetime(working)
    working = working.dropna(subset=['date'])
    if working.empty:
        return []
    
    kills = numeric_series(working, 'kills')
    deaths = numeric_series(working, 'deaths')
    assists = numeric_series(working, 'assists')
    kda_series = kills + assists / 3 - deaths
    
    medals = numeric_series(working, 'medal_count')
    damage_diff = numeric_series(working, 'damage_dealt') - numeric_series(working, 'damage_taken')
    
    picks = []
    seen = set()
    
    def add_top(series: pd.Series, label: str, formatter, top_n: int = 5) -> None:
        if series.empty:
            return
        sorted_series = series.sort_values(ascending=False).head(top_n)
        for idx, value in sorted_series.items():
            row = working.loc[idx]
            match_id = row.get('match_id')
            key = match_id or (row.get('date'), row.get('player_gamertag'), label)
            if key in seen:
                continue
            seen.add(key)
            row_kda = (safe_float(row.get('kills')) + safe_float(row.get('assists')) / 3
                       - safe_float(row.get('deaths')))
            gg = compute_match_grade(
                kda=row_kda, accuracy=row.get('accuracy'),
                dmg_dealt=row.get('damage_dealt'), dmg_taken=row.get('damage_taken'),
                outcome=row.get('outcome'),
            ) or {}
            picks.append({
                'date': format_date(row.get('date')),
                'player': row.get('player_gamertag', ''),
                'map': row.get('map', ''),
                'mode': row.get('game_type', ''),
                'reason': f'{label} {formatter(value)}',
                'grade': gg.get('grade', ''),
                'grade_class': gg.get('grade_class', ''),
                'grade_tip': gg.get('grade_tip', ''),
            })
    
    add_top(kda_series, 'KDA', lambda v: format_float(v, 2), top_n=5)
    add_top(medals, 'Medals', lambda v: format_int(v), top_n=5)
    add_top(damage_diff, 'Damage Swing', lambda v: format_signed(v, 0), top_n=5)
    
    picks.sort(key=lambda row: row.get('date', ''), reverse=True)
    return picks[:limit]


def build_change_summary(df: pd.DataFrame, days: int = 7) -> list[dict]:
    if df.empty or 'date' not in df.columns:
        return []
    
    working = df.copy()
    ensure_datetime(working)
    working = working.dropna(subset=['date'])
    if working.empty:
        return []
    
    now = working['date'].max()
    recent_start = now - pd.Timedelta(days=days)
    prev_start = now - pd.Timedelta(days=days * 2)
    
    rows = []
    for player in unique_sorted(working['player_gamertag']):
        player_df = working[working['player_gamertag'] == player]
        recent_df = player_df[player_df['date'] >= recent_start]
        prev_df = player_df[(player_df['date'] < recent_start) & (player_df['date'] >= prev_start)]
        
        recent_stats = summarize_stats(recent_df)
        prev_stats = summarize_stats(prev_df)
        
        if not recent_stats and not prev_stats:
            continue
        
        win_delta = recent_stats.get('win_rate', 0) - prev_stats.get('win_rate', 0)
        kda_delta = recent_stats.get('kda', 0) - prev_stats.get('kda', 0)
        
        rows.append({
            'player': player,
            'recent_games': format_int(recent_stats.get('games', 0)),
            'prev_games': format_int(prev_stats.get('games', 0)),
            'win_delta': format_signed(win_delta, 1),
            'kda_delta': format_signed(kda_delta, 2),
            'win_delta_heat': 'heat-good' if win_delta > 0 else 'heat-poor' if win_delta < 0 else '',
            'kda_delta_heat': 'heat-good' if kda_delta > 0 else 'heat-poor' if kda_delta < 0 else ''
        })
    
    add_composite_grades(rows, {'win_delta': True, 'kda_delta': True}, 'Change grade')
    rows.sort(key=lambda r: abs(to_number(r.get('win_delta')) or 0) + abs(to_number(r.get('kda_delta')) or 0), reverse=True)
    return rows


def build_player_match_history(df: pd.DataFrame, limit: int = 20) -> list[dict]:
    if df.empty or 'date' not in df.columns:
        return []
    
    working = df.copy()
    ensure_datetime(working)
    working = working.dropna(subset=['date']).sort_values('date', ascending=False)
    if limit:
        working = working.head(limit)
    
    score_vals = score_series(working)
    if score_vals.empty:
        score_vals = pd.Series(0.0, index=working.index)
    
    rows = []
    for idx, row in working.iterrows():
        kills = safe_float(row.get('kills', 0))
        deaths = safe_float(row.get('deaths', 0))
        assists = safe_float(row.get('assists', 0))
        kda = safe_kda(kills, assists, deaths)
        
        fired = safe_float(row.get('shots_fired', 0))
        hit = safe_float(row.get('shots_hit', 0))
        accuracy = hit / fired * 100 if fired > 0 else safe_float(row.get('accuracy', 0))
        if accuracy <= 1:
            accuracy *= 100
        
        pre_csr = row.get('pre_match_csr')
        post_csr = row.get('post_match_csr')
        pre_val = safe_float(pre_csr)
        post_val = safe_float(post_csr)
        csr_delta = post_val - pre_val if pre_val and post_val else 0

        game_grade = compute_match_grade(
            kda=kda, accuracy=accuracy,
            dmg_dealt=row.get('damage_dealt'), dmg_taken=row.get('damage_taken'),
            outcome=row.get('outcome'),
        ) or {}

        rows.append({
            'match_id': row.get('match_id', ''),
            'grade': game_grade.get('grade', ''),
            'grade_class': game_grade.get('grade_class', ''),
            'grade_tip': game_grade.get('grade_tip', ''),
            'grade_score': game_grade.get('grade_score'),
            'date': format_date(row.get('date')),
            'game_type': row.get('game_type', ''),
            'map': row.get('map', ''),
            'outcome': str(row.get('outcome', '')).title(),
            'outcome_class': outcome_class(row.get('outcome', '')),
            'kills': format_int(kills),
            'deaths': format_int(deaths),
            'assists': format_int(assists),
            'kda': format_float(kda, 2),
            'kda_min': format_float(safe_float(row.get('kda/min')), 2),
            'obj_min': format_float(safe_float(row.get('obj/min')), 1),
            'accuracy': format_float(accuracy, 1),
            'pre_csr': format_optional_int(pre_csr),
            'post_csr': format_optional_int(post_csr),
            'csr_delta': format_signed(csr_delta, 0),
            'damage_dealt': format_int(row.get('damage_dealt', 0)),
            'damage_taken': format_int(row.get('damage_taken', 0)),
            'dmg_diff': format_signed(
                safe_float(row.get('damage_dealt', 0)) - safe_float(row.get('damage_taken', 0)), 0
            ),
            'shots_fired': format_int(fired),
            'shots_hit': format_int(hit),
            'headshots': format_int(row.get('headshot_kills', 0)),
            'score': format_int(score_vals.loc[idx] if idx in score_vals.index else 0),
            'medals': format_int(row.get('medal_count', 0)),
            'avg_life': format_float(row.get('average_life_duration', 0), 1)
        })
    
    add_heatmap_classes(rows, {
        'kills': True, 'deaths': False, 'assists': True,
        'kda': True, 'accuracy': True,
        'damage_dealt': True, 'damage_taken': False, 'dmg_diff': True,
        'score': True, 'medals': True, 'avg_life': True,
        'csr_delta': True
    })
    # Per-match Grade column is the absolute Game Grade computed per row above
    # (compute_match_grade), not a percentile relative to the player's own recent
    # games — so a strong game reads strong even in a great session.
    return rows


def build_player_map_summary(df: pd.DataFrame) -> list[dict]:
    if df.empty or 'map' not in df.columns:
        return []
    
    working = add_normalized_map_column(df)
    active = get_active_map_set()
    rows = []
    for map_name in unique_sorted(working['_map_normalized']):
        if not map_name or _map_hidden(map_name, active):
            continue
        map_df = working[working['_map_normalized'] == map_name]
        games = len(map_df)
        if games == 0:
            continue
        outcomes = map_df['outcome'].astype(str).str.lower() if 'outcome' in map_df.columns else pd.Series()
        wins = (outcomes == 'win').sum() if not outcomes.empty else 0

        kills = numeric_series(map_df, 'kills')
        deaths = numeric_series(map_df, 'deaths')
        assists = numeric_series(map_df, 'assists')
        kda_series = kills + assists / 3 - deaths

        rows.append({
            'map': map_name,
            'games': format_int(games),
            'win_pct': format_float(wins / games * 100 if games else 0, 1),
            'kda': format_float(kda_series.mean() if games else 0, 2),
            'kda_min': format_float(kda_per_min(map_df), 2),
            'obj_min': _obj_dash(obj_per_min(map_df), 1),
        })
    
    add_heatmap_classes(rows, {'win_pct': True, 'kda': True})
    add_composite_grades(rows, {'win_pct': True}, 'Solo map grade')
    rows.sort(key=lambda x: (to_number(x.get('win_pct')) or 0, to_number(x.get('games')) or 0), reverse=True)
    return rows[:10]


def build_teammate_stats(df: pd.DataFrame, player: str) -> list[dict]:
    if df.empty or 'match_id' not in df.columns or 'player_gamertag' not in df.columns:
        return []
    
    player_df = df[df['player_gamertag'] == player]
    if player_df.empty:
        return []
    
    match_players = df.groupby('match_id')['player_gamertag'].apply(set).to_dict()
    totals = {}
    
    for _, row in player_df.iterrows():
        match_id = row.get('match_id')
        if not match_id or match_id not in match_players:
            continue
        teammates = match_players[match_id] - {player}
        if not teammates:
            continue
        outcome = str(row.get('outcome', '')).lower()
        win = 1 if outcome == 'win' else 0
        kills = safe_float(row.get('kills', 0))
        deaths = safe_float(row.get('deaths', 0))
        assists = safe_float(row.get('assists', 0))
        for teammate in teammates:
            entry = totals.setdefault(teammate, {'games': 0, 'wins': 0, 'kills': 0, 'deaths': 0, 'assists': 0})
            entry['games'] += 1
            entry['wins'] += win
            entry['kills'] += kills
            entry['deaths'] += deaths
            entry['assists'] += assists
    
    rows = []
    for teammate, data in totals.items():
        games = data['games']
        if games == 0:
            continue
        win_pct = data['wins'] / games * 100
        kda = safe_kda(data['kills'] / games, data['assists'] / games, data['deaths'] / games)
        rows.append({
            'teammate': teammate,
            'games': format_int(games),
            'win_pct': format_float(win_pct, 1),
            'kda': format_float(kda, 2)
        })
    
    add_heatmap_classes(rows, {'win_pct': True, 'kda': True})
    add_composite_grades(rows, {'win_pct': True}, 'Teammate grade')
    rows.sort(key=lambda x: (to_number(x.get('win_pct')) or 0, to_number(x.get('games')) or 0), reverse=True)
    return rows


def build_player_csr_history(df: pd.DataFrame, player: str) -> list[dict]:
    if df.empty or 'date' not in df.columns or 'player_gamertag' not in df.columns:
        return []
    
    ranked_df = _ranked_only(df)
    ranked_df = ranked_df[ranked_df['player_gamertag'] == player]
    if ranked_df.empty:
        return []
    
    ensure_datetime(ranked_df)
    ranked_df = ranked_df.dropna(subset=['date'])
    if ranked_df.empty:
        return []
    
    try:
        ranked_df['date_local'] = ranked_df['date'].dt.tz_convert(APP_TIMEZONE)
    except Exception:
        ranked_df['date_local'] = ranked_df['date']
    
    ranked_df['date_str'] = ranked_df['date_local'].dt.strftime('%Y-%m-%d')
    ranked_df['csr_value'] = extract_csr_values(ranked_df)
    ranked_df = ranked_df.dropna(subset=['csr_value'])
    if ranked_df.empty:
        return []
    
    daily = ranked_df.groupby('date_str')['csr_value'].last().reset_index()
    daily['date_key'] = pd.to_datetime(daily['date_str'], errors='coerce')
    daily = daily.sort_values('date_key')
    
    return [
        {'date': row['date_str'], 'csr': float(row['csr_value'])}
        for _, row in daily.iterrows()
    ]


def build_30day_overview(df: pd.DataFrame) -> dict:
    if df.empty or 'date' not in df.columns:
        return {}
    working = df.copy()
    ensure_datetime(working)
    working = working.dropna(subset=['date'])
    if working.empty:
        return {}
    now = working['date'].max()
    cutoff = now - pd.Timedelta(days=30)
    working = working[working['date'] >= cutoff]
    if working.empty:
        return {}
    
    rows = {}
    for player in unique_sorted(working['player_gamertag']):
        player_df = working[working['player_gamertag'] == player]
        stats = summarize_stats(player_df)
        if not stats:
            continue
        pre = pd.to_numeric(player_df.get('pre_match_csr', 0), errors='coerce').fillna(0)
        post = pd.to_numeric(player_df.get('post_match_csr', 0), errors='coerce').fillna(0)
        deltas = post - pre
        deltas = deltas[(pre > 0) & (post > 0)]
        avg_csr_change = deltas.mean() if not deltas.empty else 0
        
        rows[player] = {
            'games': stats.get('games', 0),
            'win_pct': stats.get('win_rate', 0),
            'kda': stats.get('kda', 0),
            'accuracy': stats.get('accuracy', 0),
            'avg_csr_change': avg_csr_change
        }
    
    if not rows:
        return {}
    
    def apply_heat(key, higher_better):
        values = [row.get(key, 0) for row in rows.values()]
        for row in rows.values():
            row[f'{key}_heat'] = get_heatmap_class(row.get(key), values, higher_better)
    
    apply_heat('win_pct', True)
    apply_heat('kda', True)
    apply_heat('accuracy', True)
    
    for player, row in rows.items():
        row.update({
            'games': format_int(row.get('games', 0)),
            'win_pct': format_float(row.get('win_pct', 0), 1),
            'kda': format_float(row.get('kda', 0), 2),
            'accuracy': format_float(row.get('accuracy', 0), 1),
            'avg_csr_change': format_signed(row.get('avg_csr_change', 0), 0)
        })
    
    return rows


def build_30day_comparison(df: pd.DataFrame, player: str) -> dict:
    if df.empty or 'date' not in df.columns or not player:
        return {}
    player_df = df[df['player_gamertag'] == player] if 'player_gamertag' in df.columns else pd.DataFrame()
    if player_df.empty:
        return {}
    ensure_datetime(player_df)
    player_df = player_df.dropna(subset=['date'])
    if player_df.empty:
        return {}
    now = player_df['date'].max()
    last_start = now - pd.Timedelta(days=30)
    prev_start = now - pd.Timedelta(days=60)
    
    last_df = player_df[player_df['date'] >= last_start]
    prev_df = player_df[(player_df['date'] < last_start) & (player_df['date'] >= prev_start)]
    
    last_stats = summarize_stats(last_df)
    prev_stats = summarize_stats(prev_df)
    
    win_diff = last_stats.get('win_rate', 0) - prev_stats.get('win_rate', 0)
    kda_diff = last_stats.get('kda', 0) - prev_stats.get('kda', 0)
    
    if win_diff > 1 or kda_diff > 0.1:
        trend = 'up'
    elif win_diff < -1 or kda_diff < -0.1:
        trend = 'down'
    else:
        trend = 'stable'
    
    return {
        'trend': trend,
        'win_pct_diff': format_signed(win_diff, 1),
        'kda_diff': format_signed(kda_diff, 2),
        'win_pct_class': 'heat-good' if win_diff > 0 else 'heat-poor' if win_diff < 0 else '',
        'kda_class': 'heat-good' if kda_diff > 0 else 'heat-poor' if kda_diff < 0 else ''
    }


def build_weapon_rows(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    
    ranked_df = _ranked_only(df)
    if ranked_df.empty:
        return []
    
    rows = []
    for player in unique_sorted(ranked_df['player_gamertag']):
        player_df = ranked_df[ranked_df['player_gamertag'] == player]
        if player_df.empty:
            continue
        
        games = len(player_df)
        fired = numeric_series(player_df, 'shots_fired').sum()
        hit = numeric_series(player_df, 'shots_hit').sum()
        accuracy = hit / fired * 100 if fired > 0 else 0
        
        kills = numeric_series(player_df, 'kills').sum()
        headshots = numeric_series(player_df, 'headshot_kills').sum()
        hs_pct = headshots / kills * 100 if kills > 0 else 0
        
        rows.append({
            'player': player,
            'shots_fired': format_int(fired),
            'shots_hit': format_int(hit),
            'accuracy': format_float(accuracy, 1),
            'headshots': format_int(headshots),
            'hs_pct': format_float(hs_pct, 1),
            'melee': format_int(numeric_series(player_df, 'melee_kills').sum()),
            'grenades': format_int(numeric_series(player_df, 'grenade_kills').sum()),
            'power': format_int(numeric_series(player_df, 'power_weapon_kills').sum()),
            'sniper_kills': format_int(sniper_series(player_df).sum()),
            'snipe_medals': format_int(safe_col_sum(player_df, 'medal_snipe')),
            'no_scope_medals': format_int(safe_col_sum(player_df, 'medal_no_scope')),
            # Per-game headline values (totals kept above for grading/heatmap).
            'sniper_kills_pg': format_float(sniper_series(player_df).sum() / games, 2) if games else '0',
            'sniper_kills_total': format_int(sniper_series(player_df).sum()),
            'snipe_medals_pg': format_float(safe_col_sum(player_df, 'medal_snipe') / games, 2) if games else '0',
            'snipe_medals_total': format_int(safe_col_sum(player_df, 'medal_snipe')),
            'no_scope_medals_pg': format_float(safe_col_sum(player_df, 'medal_no_scope') / games, 2) if games else '0',
            'no_scope_medals_total': format_int(safe_col_sum(player_df, 'medal_no_scope')),
            'headshots_pg': format_float(headshots / games, 2) if games else '0',
            'headshots_total': format_int(headshots),
            'melee_pg': format_float(numeric_series(player_df, 'melee_kills').sum() / games, 2) if games else '0',
            'melee_total': format_int(numeric_series(player_df, 'melee_kills').sum()),
            'grenades_pg': format_float(numeric_series(player_df, 'grenade_kills').sum() / games, 2) if games else '0',
            'grenades_total': format_int(numeric_series(player_df, 'grenade_kills').sum()),
            'power_pg': format_float(numeric_series(player_df, 'power_weapon_kills').sum() / games, 2) if games else '0',
            'power_total': format_int(numeric_series(player_df, 'power_weapon_kills').sum()),
        })
    
    add_heatmap_classes(rows, {
        'accuracy': True,
        'headshots': True,
        'hs_pct': True,
        'melee': True,
        'grenades': True,
        'power': True,
        'sniper_kills': True,
        'snipe_medals': True,
        'no_scope_medals': True
    })
    add_composite_grades(rows, {
        'accuracy': True, 'headshots': True, 'hs_pct': True,
        'melee': True, 'grenades': True, 'power': True,
        'sniper_kills': True,
    }, 'Gunplay grade')
    
    return rows


def build_weapon_accuracy_trend(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}
    trend_df = apply_trend_range(normalize_trend_df(df), '30')
    if trend_df.empty:
        return {}
    
    working = trend_df.copy()
    fired = pd.to_numeric(working.get('shots_fired', 0), errors='coerce').fillna(0)
    hit = pd.to_numeric(working.get('shots_hit', 0), errors='coerce').fillna(0)
    working['accuracy_pct'] = 0.0
    nonzero = fired > 0
    working.loc[nonzero, 'accuracy_pct'] = hit[nonzero] / fired[nonzero] * 100
    if not nonzero.any() and 'accuracy' in working.columns:
        acc = pd.to_numeric(working['accuracy'], errors='coerce').fillna(0)
        if acc.max() <= 1:
            acc = acc * 100
        working['accuracy_pct'] = acc
    
    return build_trend_data(working, 'accuracy_pct', 'accuracy')

def add_trend_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """Add derived columns used by the trends charts."""
    if df.empty:
        return df

    working = df.copy()

    if 'outcome' in working.columns:
        outcome_lower = working['outcome'].astype(str).str.lower()
        working['win_rate'] = (outcome_lower == 'win').astype(float) * 100

    obj_score = extract_objective_score(working)
    if not obj_score.empty:
        working['obj_score'] = obj_score

    if 'dmg/min' in working.columns:
        working['dmg_min'] = pd.to_numeric(working['dmg/min'], errors='coerce').fillna(0)
    elif 'damage_dealt' in working.columns and 'duration' in working.columns:
        damage_dealt = pd.to_numeric(working['damage_dealt'], errors='coerce').fillna(0)
        duration = pd.to_numeric(working['duration'], errors='coerce').fillna(0)
        duration_min = duration / 60.0
        working['dmg_min'] = 0.0
        nonzero = duration_min > 0
        working.loc[nonzero, 'dmg_min'] = damage_dealt[nonzero] / duration_min[nonzero]

    if 'dmg_difference' in working.columns:
        working['dmg_diff'] = pd.to_numeric(
            working['dmg_difference'], errors='coerce'
        ).fillna(0)
    elif 'damage_dealt' in working.columns and 'damage_taken' in working.columns:
        damage_dealt = pd.to_numeric(working['damage_dealt'], errors='coerce').fillna(0)
        damage_taken = pd.to_numeric(working['damage_taken'], errors='coerce').fillna(0)
        working['dmg_diff'] = damage_dealt - damage_taken

    if 'max_killing_spree' in working.columns:
        working['max_spree'] = pd.to_numeric(
            working['max_killing_spree'], errors='coerce'
        ).fillna(0)

    if 'duration' in working.columns:
        working['duration_min'] = pd.to_numeric(
            working['duration'], errors='coerce'
        ).fillna(0) / 60.0

    if 'kills' in working.columns:
        working['kills_pg'] = pd.to_numeric(working['kills'], errors='coerce').fillna(0)
    if 'deaths' in working.columns:
        working['deaths_pg'] = pd.to_numeric(working['deaths'], errors='coerce').fillna(0)

    return working


def build_leaderboard(df: pd.DataFrame, category: str, limit: int = 10) -> list:
    """Build leaderboard for a specific category."""
    if df.empty:
        return []
    
    rows = []
    
    if category == 'csr':
        csr_overview = build_csr_overview(df)
        for row in csr_overview:
            csr_val = to_number(row.get('current_csr'))
            if csr_val:
                rows.append({
                    'rank': 0,
                    'player': row['player'],
                    'value': row['current_csr'],
                    'csr': row['current_csr'],
                    'context': ''
                })
        rows.sort(key=lambda x: to_number(x['value']) or 0, reverse=True)
    
    elif category == 'kda':
        for player in unique_sorted(df['player_gamertag']):
            player_df = df[df['player_gamertag'] == player]
            kills = pd.to_numeric(player_df.get('kills', 0), errors='coerce').fillna(0).sum()
            deaths = pd.to_numeric(player_df.get('deaths', 0), errors='coerce').fillna(0).sum()
            assists = pd.to_numeric(player_df.get('assists', 0), errors='coerce').fillna(0).sum()
            games = len(player_df)
            
            kda = safe_kda(kills / games if games else 0, assists / games if games else 0, deaths / games if games else 0)
            
            rows.append({
                'rank': 0,
                'player': player,
                'value': format_float(kda, 2),
                'kda': format_float(kda, 2),
                'context': f'{games} games'
            })
        
        rows.sort(key=lambda x: to_number(x['value']) or 0, reverse=True)
    
    elif category == 'win_rate':
        for player in unique_sorted(df['player_gamertag']):
            player_df = df[df['player_gamertag'] == player]
            games = len(player_df)
            outcomes = player_df['outcome'].astype(str).str.lower() if 'outcome' in player_df.columns else pd.Series()
            wins = (outcomes == 'win').sum() if not outcomes.empty else 0
            
            if games:
                rows.append({
                    'rank': 0,
                    'player': player,
                    'value': format_float(wins / games * 100, 1),
                    'win_rate': format_float(wins / games * 100, 1),
                    'context': f'{wins}-{games - wins}'
                })
        
        rows.sort(key=lambda x: to_number(x['value']) or 0, reverse=True)

    elif category == 'accuracy':
        for player in unique_sorted(df['player_gamertag']):
            player_df = df[df['player_gamertag'] == player]
            games = len(player_df)
            if games == 0:
                continue
            fired = pd.to_numeric(player_df.get('shots_fired', 0), errors='coerce').fillna(0).sum()
            hit = pd.to_numeric(player_df.get('shots_hit', 0), errors='coerce').fillna(0).sum()
            if fired > 0:
                accuracy = hit / fired * 100
            else:
                acc = pd.to_numeric(player_df.get('accuracy', 0), errors='coerce').fillna(0)
                accuracy = acc.mean()
                if accuracy <= 1:
                    accuracy *= 100
            rows.append({
                'rank': 0,
                'player': player,
                'value': format_float(accuracy, 1),
                'accuracy': format_float(accuracy, 1),
                'context': f'{games} games'
            })
        
        rows.sort(key=lambda x: to_number(x['value']) or 0, reverse=True)

    elif category == 'streak':
        for player in unique_sorted(df['player_gamertag']):
            player_df = df[df['player_gamertag'] == player]
            if player_df.empty:
                continue
            win_streak, _ = compute_best_streaks(player_df)
            rows.append({
                'rank': 0,
                'player': player,
                'value': win_streak,
                'streak': format_int(win_streak),
                'context': ''
            })
        
        rows.sort(key=lambda x: to_number(x['value']) or 0, reverse=True)

    elif category == 'kills':
        for player in unique_sorted(df['player_gamertag']):
            player_df = df[df['player_gamertag'] == player]
            if player_df.empty:
                continue
            kills = pd.to_numeric(player_df.get('kills', 0), errors='coerce').fillna(0).sum()
            rows.append({
                'rank': 0,
                'player': player,
                'value': format_int(kills),
                'kills': format_int(kills),
                'context': ''
            })
        
        rows.sort(key=lambda x: to_number(x['value']) or 0, reverse=True)

    elif category == 'games':
        for player in unique_sorted(df['player_gamertag']):
            player_df = df[df['player_gamertag'] == player]
            games = len(player_df)
            rows.append({
                'rank': 0,
                'player': player,
                'value': format_int(games),
                'games': format_int(games),
                'context': ''
            })
        
        rows.sort(key=lambda x: to_number(x['value']) or 0, reverse=True)

    elif category == 'csr_gained':
        for player in unique_sorted(df['player_gamertag']):
            player_df = df[df['player_gamertag'] == player]
            if player_df.empty or 'date' not in player_df.columns:
                continue
            player_df = player_df.copy()
            ensure_datetime(player_df)
            player_df = player_df.dropna(subset=['date']).sort_values('date', ascending=True)
            if player_df.empty:
                continue
            csr_vals = extract_csr_values(player_df)
            if csr_vals.empty or len(csr_vals) < 2:
                continue
            gain = float(csr_vals.iloc[-1] - csr_vals.iloc[0])
            if gain <= 0:
                continue
            rows.append({
                'rank': 0,
                'player': player,
                'value': gain,
                'csr_gained': format_int(gain),
                'context': ''
            })
        
        rows.sort(key=lambda x: to_number(x['value']) or 0, reverse=True)

    elif category in ('snipes', 'headshots', 'power_weapons', 'medals'):
        # Combat counting boards — total + per-game. Reads whatever df is passed
        # (the route hands these the medal-inclusive df so medal_* cols exist).
        col = {
            'snipes': 'medal_snipe', 'headshots': 'headshot_kills',
            'power_weapons': 'power_weapon_kills', 'medals': 'medal_count',
        }[category]
        # The snipes board is the true sniper-kill count = Snipe + No-Scope.
        present = (col in df.columns) or (
            category == 'snipes' and any(c in df.columns for c in _SNIPER_MEDAL_COLS))
        if present:
            for player in unique_sorted(df['player_gamertag']):
                player_df = df[df['player_gamertag'] == player]
                games = len(player_df)
                if games == 0:
                    continue
                if category == 'snipes':
                    total = sniper_series(player_df).sum()
                else:
                    total = pd.to_numeric(player_df.get(col, 0), errors='coerce').fillna(0).sum()
                if total <= 0:
                    continue
                per_game = total / games
                rows.append({
                    'rank': 0,
                    'player': player,
                    # Per-game is the headline; total shown in parens as context.
                    'value': per_game,
                    category: f'{format_float(per_game, 2)}/g',
                    'context': f'({format_int(total)})',
                })
            rows.sort(key=lambda x: to_number(x['value']) or 0, reverse=True)

    # Add rank numbers
    for idx, row in enumerate(rows[:limit], 1):
        row['rank'] = idx

    return rows[:limit]


# Initialize at module level
ENGINE = get_engine()


def _wait_for_db(engine, timeout: int | None = None, interval: float | None = None) -> bool:
    """Block until Postgres is reachable before doing any startup DDL.

    On this box Docker's embedded DNS occasionally can't resolve ``halodb`` for
    a few seconds (network/DNS hiccup). Connecting immediately at import time
    would then raise and kill the whole web process; ``restart: always`` bounces
    it, and every bounce is a "502 Bad Gateway" window behind the proxy. Retrying
    here keeps the process alive through the blip instead of crash-looping.
    """
    timeout = int(timeout if timeout is not None else os.getenv('HALO_DB_WAIT_TIMEOUT', '120'))
    interval = float(interval if interval is not None else os.getenv('HALO_DB_WAIT_INTERVAL', '3.0'))
    deadline = time.time() + timeout
    attempt = 0
    while True:
        attempt += 1
        try:
            with engine.connect() as conn:
                conn.execute(text('SELECT 1'))
            if attempt > 1:
                print(f"✅ DB reachable after {attempt} attempts", flush=True)
            return True
        except Exception as exc:  # DNS failure, DB not up yet, etc.
            if time.time() >= deadline:
                print(f"⚠️ DB still unreachable after {timeout}s "
                      f"({attempt} attempts): {exc}", flush=True)
                return False
            print(f"⏳ DB not ready (attempt {attempt}): {exc}; "
                  f"retrying in {interval}s", flush=True)
            time.sleep(interval)


_wait_for_db(ENGINE)
try:
    ensure_indexes(ENGINE)
    ensure_app_tables(ENGINE)
except Exception as exc:
    # Don't let a transient DB/DNS blip during startup DDL crash the process
    # into a restart loop — the tables/indexes are created lazily on first use
    # anyway, and the next deploy/restart will retry.
    logger.warning('startup table/index init failed (continuing): %s', exc)

# Silence "database has no actual collation version" warnings from PostgreSQL.
# Alpine's musl libc doesn't report collation versions, so REFRESH fails.
# Clear the stale recorded version so PG stops checking on every connection.
try:
    with ENGINE.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(
            "UPDATE pg_database SET datcollversion = NULL "
            "WHERE datname = current_database() AND datcollversion IS NOT NULL"
        ))
except SQLAlchemyError as exc:
    logger.warning('Failed to clear database collation version: %s', exc)
    pass  # harmless if it fails (e.g. permissions)

cache = DataCache(ENGINE)
# Medal-inclusive cache: same rows as ``cache`` but also carries every
# ``medal_*`` column. Loaded lazily on first medal-route hit so the common
# (non-medal) pages never pay the memory cost of ~185 medal columns.
medal_cache = DataCache(ENGINE, include_medals=True)


def medal_df() -> pd.DataFrame:
    """Return the medal-inclusive dataframe for the four medal routes.

    Identical row set to ``cache.get()`` but with the per-medal columns
    present, so any per-player/per-match medal aggregate matches what the old
    single full-column cache produced.
    """
    return medal_cache.get()


count_cache = DbCountCache(ENGINE)


def refresh_match_caches_for_count(current_count: int) -> None:
    """Keep the rendered pages in step with the DB row count.

    The browser polls a tiny version endpoint. When that endpoint notices a new
    match row, refresh the common dataframe immediately so the reload/partial
    refresh that follows renders the new game instead of one stale response.
    """
    count_cache.set(current_count)
    if current_count > 0 and current_count != cache.last_count:
        cache.force_reload()


INSIGHTS_CACHE = {
    'last_ts': 0.0,
    'payload': None
}
PLAYER_HOVER_CACHE = {
    'last_ts': 0.0,
    'payload': {}
}
INDEX_PAGE_CACHE = {
    'last_ts': 0.0,
    'last_count': -1,
    'payload': None
}
INDEX_CACHE_TTL = int(os.getenv('HALO_INDEX_CACHE_TTL', '120'))
PAGE_CACHE_TTL = int(os.getenv('HALO_PAGE_CACHE_TTL', '300'))
# name → {key → {'ts', 'count', 'payload'}} (key=None for un-keyed pages).
PAGE_CACHES: dict = {}
PAGE_CACHE_MAX_KEYS = int(os.getenv('HALO_PAGE_CACHE_MAX_KEYS', '24'))
_PAGE_REBUILDING: set = set()          # {(name, key)} single-flight guard
_PAGE_REBUILD_LOCK = threading.Lock()
# Cap concurrent background rebuilds so a new game doesn't stampede the CPU.
_PAGE_REBUILD_SEM = threading.BoundedSemaphore(int(os.getenv('HALO_PAGE_REBUILD_CONCURRENCY', '3')))


def kda_per_min(df) -> float:
    """True per-minute KDA rate over a group: sum(per-game KDA) / sum(minutes).
    Normalizes for game length (a +6 KDA in an 8-min stomp beats +6 in a 13-min
    grind). `duration` is numeric seconds."""
    if df is None or df.empty or 'duration' not in df.columns or 'kda' not in df.columns:
        return 0.0
    mins = float(numeric_series(df, 'duration').sum()) / 60.0
    if mins <= 0:
        return 0.0
    return float(numeric_series(df, 'kda').sum()) / mins


def obj_per_min(df) -> float:
    """True per-minute objective-score rate over a group — OBJECTIVE-MODE games
    only, so slayer games can't dilute the rate (the grading arrays are built
    from objective-mode games, and mixing populations tanked mixed sessions)."""
    if df is None or df.empty or 'duration' not in df.columns:
        return 0.0
    try:
        obj = objective_score_series(df)
        mask = obj > 0
        if not bool(mask.any()):
            return 0.0
        mins = float(numeric_series(df, 'duration')[mask].sum()) / 60.0
        if mins <= 0:
            return 0.0
        return float(obj[mask].sum()) / mins
    except Exception:
        return 0.0


def clean_mode(mode) -> str:
    """Shorten a verbose game_type for display: 'Ranked:Slayer' → 'Slayer',
    'Assault:Neutral Bomb Ranked' → 'Neutral Bomb'. Keeps session cards readable
    instead of truncating 'Lattice - Ranked · Ranked:Slayer'."""
    s = str(mode or '').strip()
    if ':' in s:
        s = s.split(':', 1)[1].strip()
    s = re.sub(r'\bRanked\b', '', s, flags=re.IGNORECASE).strip(' -·')
    return s or str(mode or '').strip()


def _page_cache_put(name: str, key, payload, count: int) -> None:
    store = PAGE_CACHES.setdefault(name, {})
    store[key] = {'ts': time.time(), 'count': count, 'payload': payload}
    # Bound keyed stores (player pages, ranges, sids) — evict oldest.
    while len(store) > PAGE_CACHE_MAX_KEYS:
        oldest = min(store, key=lambda k: store[k]['ts'])
        store.pop(oldest, None)


def _spawn_page_rebuild(name: str, build_fn, key) -> None:
    """Single-flight background rebuild — the request that noticed staleness
    already got the stale payload; this refreshes the cache for the next one."""
    tag = (name, key)
    with _PAGE_REBUILD_LOCK:
        if tag in _PAGE_REBUILDING:
            return
        _PAGE_REBUILDING.add(tag)

    def _run():
        try:
            with _PAGE_REBUILD_SEM:
                payload = build_fn()
                cnt = count_cache.get()
                if cnt > 0:  # same DB-blip poisoning guard as the inline path
                    _page_cache_put(name, key, payload, cnt)
        except Exception as exc:
            logger.warning('page rebuild %s failed: %s', name, exc)
        finally:
            with _PAGE_REBUILD_LOCK:
                _PAGE_REBUILDING.discard(tag)

    threading.Thread(target=_run, daemon=True, name=f'rebuild-{name}').start()


def get_cached_page_payload(name: str, build_fn, key=None) -> dict:
    """Page-payload cache with STALE-WHILE-REVALIDATE: a request never waits for
    a rebuild if any previous payload exists for this (name, key) — it gets the
    stale copy instantly and a background thread refreshes the cache. Only a
    truly cold cache (first ever build) runs build_fn inline."""
    now = time.time()
    current_count = count_cache.get()
    entry = PAGE_CACHES.get(name, {}).get(key)
    if entry is not None:
        fresh = (entry['count'] == current_count
                 and now - entry['ts'] < PAGE_CACHE_TTL)
        if not fresh:
            _spawn_page_rebuild(name, build_fn, key)
        return entry['payload']
    payload = build_fn()
    # Don't memoize a payload built while the DB count is 0 — that means the DB
    # was unreachable (startup / mid-restart DNS blip), so build_fn ran on an
    # empty df and would otherwise cache an EMPTY page for the full TTL (the
    # recurring "section vanished after deploy" poisoning). Rebuild until real.
    if current_count > 0:
        _page_cache_put(name, key, payload, current_count)
    return payload


def resolve_player_name(player_name: str, all_players: list[str]) -> str | None:
    requested = (player_name or '').strip().lower()
    if not requested:
        return None
    for player in all_players:
        if str(player).strip().lower() == requested:
            return player
    for player in all_players:
        if requested in str(player).strip().lower():
            return player
    return None


def build_summary_table(df: pd.DataFrame) -> list:
    """Build summary table comparing 30-day stats to lifetime stats."""
    if df.empty or 'date' not in df.columns:
        return []

    ranked_df = _ranked_only(df)
    
    ensure_datetime(ranked_df)
    ranked_df = ranked_df.dropna(subset=['date'])
    
    if ranked_df.empty:
        return []

    summary_rows = []
    for player in unique_sorted(ranked_df['player_gamertag']):
        player_df = ranked_df[ranked_df['player_gamertag'] == player]
        if player_df.empty:
            continue

        # Lifetime stats
        lifetime_games = len(player_df)
        lifetime_outcomes = player_df['outcome'].astype(str).str.lower() if 'outcome' in player_df.columns else pd.Series()
        lifetime_wins = (lifetime_outcomes == 'win').sum() if not lifetime_outcomes.empty else 0
        lifetime_win_pct = lifetime_wins / lifetime_games * 100 if lifetime_games > 0 else 0
        lifetime_stats = calculate_player_stats(player_df, lifetime_games)
        lifetime_stats['win_pct'] = lifetime_win_pct

        # 30-day stats
        max_date = player_df['date'].max()
        cutoff_date = max_date - pd.Timedelta(days=30)
        recent_df = player_df[player_df['date'] >= cutoff_date]
        recent_games = len(recent_df)
        recent_outcomes = recent_df['outcome'].astype(str).str.lower() if 'outcome' in recent_df.columns else pd.Series()
        recent_wins = (recent_outcomes == 'win').sum() if not recent_outcomes.empty else 0
        recent_win_pct = recent_wins / recent_games * 100 if recent_games > 0 else 0
        recent_stats = calculate_player_stats(recent_df, recent_games)
        recent_stats['win_pct'] = recent_win_pct

        stats_to_compare = [
            {'key': 'kda', 'name': 'KDA', 'format': '{:.2f}'},
            {'key': 'win_pct', 'name': 'Win %', 'format': '{:.1f}%'},
            {'key': 'accuracy', 'name': 'Accuracy', 'format': '{:.1f}%'},
            {'key': 'dmg_per_min', 'name': 'DMG/min', 'format': '{:.0f}'},
        ]

        for stat in stats_to_compare:
            recent_val = recent_stats.get(stat['key'], 0)
            lifetime_val = lifetime_stats.get(stat['key'], 0)
            trend = recent_val - lifetime_val
            trend_class = 'heat-excellent' if trend > 0 else 'heat-poor' if trend < 0 else ''
            
            summary_rows.append({
                'player': player,
                'stat': stat['name'],
                'recent': stat['format'].format(recent_val),
                'lifetime': stat['format'].format(lifetime_val),
                'trend': f'{"+" if trend > 0 else ""}{stat["format"].format(trend)}',
                'trend_class': trend_class,
            })

    summary_rows.sort(key=lambda x: to_number(x['trend']), reverse=True)
    return summary_rows


def build_match_details(df: pd.DataFrame, match_id: str) -> dict:
    """Build detailed view for a single match."""
    if df.empty or 'match_id' not in df.columns:
        return {}

    match_df = df[df['match_id'] == match_id]
    if match_df.empty:
        return {}

    # Get the first row for general match info
    match_info = match_df.iloc[0]

    # Scoreboard
    scoreboard = []
    for _, row in match_df.iterrows():
        sb_row = format_player_stats_row(row['player_gamertag'], 1, 1 if row['outcome'] == 'win' else 0, row)
        grade = compute_match_grade(
            kda=row.get('kda'), accuracy=row.get('accuracy'),
            dmg_dealt=row.get('damage_dealt'), dmg_taken=row.get('damage_taken'),
            outcome=row.get('outcome'),
        ) or {}
        sb_row['grade'] = grade.get('grade', '')
        sb_row['grade_class'] = grade.get('grade_class', '')
        sb_row['grade_tip'] = grade.get('grade_tip', '')
        scoreboard.append(sb_row)

    # Medals
    medal_cols = [col for col in match_df.columns if str(col).startswith('medal_')]
    medals = []
    for col in medal_cols:
        total = match_df[col].sum()
        if total > 0:
            medals.append({'name': col.replace('medal_', '').replace('_', ' ').title(), 'count': total})
    
    return {
        'match_id': match_id,
        'map': match_info.get('map'),
        'game_type': match_info.get('game_type'),
        'playlist': match_info.get('playlist'),
        'date': format_date(match_info.get('date')),
        'duration': match_info.get('duration'),
        'scoreboard': scoreboard,
        'medals': medals,
    }


def build_player_analysis(df: pd.DataFrame) -> list:
    """Build player analysis table combining trends and outliers."""
    if df.empty or 'date' not in df.columns:
        return []

    ranked_df = _ranked_only(df)
    
    ensure_datetime(ranked_df)
    ranked_df = ranked_df.dropna(subset=['date'])
    
    if ranked_df.empty:
        return []

    analysis_rows = []
    # Compute outlier spotlight ONCE, not per-player
    outlier_highlights = build_outlier_spotlight(df)
    outlier_by_player = {r['player']: r.get('highlights', []) for r in outlier_highlights}

    for player in unique_sorted(ranked_df['player_gamertag']):
        player_df = ranked_df[ranked_df['player_gamertag'] == player]
        if player_df.empty:
            continue

        # Lifetime stats
        lifetime_games = len(player_df)
        lifetime_stats = calculate_player_stats(player_df, lifetime_games)

        # 30-day stats
        max_date = player_df['date'].max()
        cutoff_date = max_date - pd.Timedelta(days=30)
        recent_df = player_df[player_df['date'] >= cutoff_date]
        recent_games = len(recent_df)
        recent_stats = calculate_player_stats(recent_df, recent_games)

        player_outliers = outlier_by_player.get(player, [])

        analysis_rows.append({
            'player': player,
            'kda_recent': '{:.2f}'.format(recent_stats.get('kda', 0)),
            'kda_lifetime': '{:.2f}'.format(lifetime_stats.get('kda', 0)),
            'kda_trend': recent_stats.get('kda', 0) - lifetime_stats.get('kda', 0),
            'win_pct_recent': '{:.1f}%'.format(recent_stats.get('win_pct', 0)),
            'win_pct_lifetime': '{:.1f}%'.format(lifetime_stats.get('win_pct', 0)),
            'win_pct_trend': recent_stats.get('win_pct', 0) - lifetime_stats.get('win_pct', 0),
            'outliers': player_outliers
        })

    analysis_rows.sort(key=lambda x: x['kda_trend'], reverse=True)
    return analysis_rows


INDEX_SLOW_TTL = 300  # aggregate stats (timeframe tables, analysis) barely move per game
INDEX_SLOW_CACHE = {'ts': 0.0, 'count': -1, 'payload': None}


def _index_slow_aggregates(df: pd.DataFrame, count: int) -> dict:
    """The heavy, slow-moving parts of the dashboard (all-time / multi-timeframe
    aggregates, full session list). These barely change from one game
    to the next, so they get a 5-minute time cache that is NOT invalidated by
    every new game — that's what kept initial load at 7-10s during active play."""
    e = INDEX_SLOW_CACHE
    if e['payload'] is not None and count > 0 and time.time() - e['ts'] < INDEX_SLOW_TTL:
        return e['payload']
    # If the df is empty (DB blip mid-restart while count_cache still has a stale
    # non-zero count), DON'T cache — otherwise we'd pin empty session_list/solo
    # cards for the full 5-min TTL even after the DB recovers.
    _ok = df is not None and not df.empty
    slow = {
        'session_list': build_session_list(df, mode='squad'),
        'csr_overview_trends': build_csr_trends(apply_trend_range(normalize_trend_df(df), '90')),
        'ranked_arena_rows': build_ranked_arena_summary(df),
        'ranked_arena_30day_rows': _build_ranked_arena_period(df, 30),
        'ranked_arena_90day_rows': _build_ranked_arena_period(df, 90),
        'ranked_arena_180day_rows': _build_ranked_arena_period(df, 180),
        'ranked_arena_1y_rows': _build_ranked_arena_period(df, 365),
        'ranked_arena_2y_rows': _build_ranked_arena_period(df, 730),
        'ranked_arena_lifetime_rows': _build_ranked_arena_period(df, None),
        'player_analysis_rows': build_player_analysis(df),
        'summary_rows': build_summary_table(df),
        'players_list': unique_sorted(df['player_gamertag']) if not df.empty and 'player_gamertag' in df.columns else [],
        'playlists': unique_sorted(df['playlist']) if not df.empty and 'playlist' in df.columns else [],
        'modes': unique_sorted(df['game_type']) if not df.empty and 'game_type' in df.columns else [],
    }
    if count > 0 and _ok:
        e.update(payload=slow, ts=time.time(), count=count)
    return slow


def _build_index_base_payload(df: pd.DataFrame) -> dict:
    """Compute the expensive parts of the index page. Split into a FAST tier
    (rebuilds per new game, ~1.4s) and a SLOW tier (5-min cache, ~5s but rare).
    STALE-WHILE-REVALIDATE: a stale payload is served instantly while a
    background thread rebuilds — the dashboard never blocks on a new game."""
    now = time.time()
    current_count = count_cache.get()

    cached = INDEX_PAGE_CACHE.get('payload')
    fresh = (cached
             and INDEX_PAGE_CACHE['last_count'] == current_count
             and now - INDEX_PAGE_CACHE['last_ts'] < INDEX_CACHE_TTL)
    if cached and not fresh:
        _spawn_page_rebuild('_index_base', lambda: _index_base_build(cache.get()), None)
        return cached
    if cached:
        return cached
    return _index_base_build(df)


def _index_base_build(df: pd.DataFrame) -> dict:
    """The actual index build; stores into INDEX_PAGE_CACHE (inline or from the
    SWR rebuild thread)."""
    current_count = count_cache.get()
    # Fast tier: the live-ish, per-game stuff (report card + grade chart + CSR).
    fast = {
        'squad_report_card': build_squad_report_card(df, mode='squad'),
        # Cross-flag strip: every player whose latest SOLO session is newer
        # than the last squad night — surfaced on the squad dash so a fresh
        # solo grind is never invisible (full solo content stays on /solo).
        'recent_solo_strip': build_recent_solo_strip(df),
        'grade_timeline': build_grade_timeline(df, mode='squad'),
        'squad_skill_perf': build_squad_skill_performance(df),
        'csr_overview_rows': build_csr_overview(df),
    }
    payload = {**fast, **_index_slow_aggregates(df, current_count)}
    if current_count > 0:
        INDEX_PAGE_CACHE['payload'] = payload
        INDEX_PAGE_CACHE['last_count'] = current_count
        INDEX_PAGE_CACHE['last_ts'] = time.time()
    return payload


def _match_grade_for_row(row) -> dict:
    """Absolute Game Grade for a single cache/df match row (raw values)."""
    kills = safe_float(row.get('kills', 0))
    deaths = safe_float(row.get('deaths', 0))
    assists = safe_float(row.get('assists', 0))
    kda = safe_kda(kills, assists, deaths)
    fired = safe_float(row.get('shots_fired', 0))
    hit = safe_float(row.get('shots_hit', 0))
    accuracy = hit / fired * 100 if fired > 0 else safe_float(row.get('accuracy', 0))
    return compute_match_grade(
        kda=kda, accuracy=accuracy, dmg_dealt=row.get('damage_dealt'),
        dmg_taken=row.get('damage_taken'), outcome=row.get('outcome'),
    ) or {}


def build_session_mvp(df: pd.DataFrame) -> dict | None:
    """MVP of the most-recent squad session: the single best-graded game plus the
    player with the highest average Game Grade across that session."""
    if df is None or df.empty or 'date' not in df.columns:
        return None
    work = df.copy()
    ensure_datetime(work)
    work = work.dropna(subset=['date']).sort_values('date', ascending=False)
    if work.empty:
        return None

    # Squad matches only (2+ tracked players together) — a solo night's "MVP"
    # would trivially be the lone player, so the panel skips solo sessions.
    if 'match_id' in work.columns and 'player_gamertag' in work.columns:
        ppm = work.groupby('match_id')['player_gamertag'].nunique()
        squad_ids = set(ppm[ppm >= 2].index)
        if not squad_ids:
            return None
        work = work[work['match_id'].isin(squad_ids)]

    sess = latest_session_rows(work)

    best = None
    per_player = {}
    for idx, row in sess.iterrows():
        g = _match_grade_for_row(row)
        score = g.get('grade_score')
        if score is None:
            continue
        p = row.get('player_gamertag', '')
        per_player.setdefault(p, []).append(score)
        kills = safe_float(row.get('kills', 0))
        deaths = safe_float(row.get('deaths', 0))
        assists = safe_float(row.get('assists', 0))
        cand = {
            'player': p, 'grade': g.get('grade'), 'grade_class': g.get('grade_class'),
            'score': score, 'kills': format_int(kills), 'deaths': format_int(deaths),
            'assists': format_int(assists), 'kda': format_float(safe_kda(kills, assists, deaths), 2),
            'map': normalize_map_name(row.get('map')), 'match_id': row.get('match_id', ''),
            'date': format_date(row.get('date')),
        }
        if best is None or score > best['score']:
            best = cand
    if best is None:
        return None

    top = None
    for p, scores in per_player.items():
        avg = sum(scores) / len(scores)
        if top is None or avg > top['avg']:
            g = grade_from_percentile(avg)
            top = {'player': p, 'avg': round(avg), 'games': len(scores),
                   'grade': g, 'grade_class': grade_class(g)}
    return {'best_game': best, 'top_player': top, 'games': int(len(sess))}


def build_headlines(df: pd.DataFrame) -> list:
    """Auto-generated, plain-English squad insight cards for the dashboard.

    Rule-based (no LLM dependency) so it's instant and always renders. Each card
    is {icon, title, detail, tone} where tone ∈ good|bad|neutral."""
    cards = []
    if df is None or df.empty or 'date' not in df.columns:
        return cards
    ranked = _ranked_only(df)
    if ranked.empty:
        return cards
    ensure_datetime(ranked)
    ranked = ranked.dropna(subset=['date']).sort_values('date', ascending=False)
    if ranked.empty:
        return cards

    # --- Most-recent session window ---------------------------------------
    sess = latest_session_rows(ranked)

    # Session record + net CSR (per distinct match, outcome is shared by squad).
    sess_by_match = sess.drop_duplicates(subset=['match_id']) if 'match_id' in sess.columns else sess
    outcomes = sess_by_match['outcome'].astype(str).str.lower() if 'outcome' in sess_by_match.columns else pd.Series(dtype=str)
    s_w = int((outcomes == 'win').sum())
    s_l = int((outcomes == 'loss').sum())
    net_csr = 0.0
    if 'post_match_csr' in sess.columns and 'pre_match_csr' in sess.columns:
        pre = pd.to_numeric(sess['pre_match_csr'], errors='coerce')
        post = pd.to_numeric(sess['post_match_csr'], errors='coerce')
        # net per player then summed → squad swing
        deltas = (post - pre).dropna()
        net_csr = float(deltas.sum())
    if s_w or s_l:
        tone = 'good' if s_w >= s_l else 'bad'
        csr_bit = f" · {net_csr:+.0f} CSR" if net_csr else ""
        cards.append({
            'icon': '🎮', 'title': f"Last session: {s_w}-{s_l}{csr_bit}",
            'detail': f"{len(sess_by_match)} ranked games",
            'tone': tone,
        })

    # --- Hot hand (best avg Game Grade this session) ----------------------
    grades_by_player = {}
    for _, row in sess.iterrows():
        sc = _match_grade_for_row(row).get('grade_score')
        if sc is not None:
            grades_by_player.setdefault(row.get('player_gamertag', ''), []).append(sc)
    if grades_by_player:
        hot = max(grades_by_player.items(), key=lambda kv: sum(kv[1]) / len(kv[1]))
        avg = round(sum(hot[1]) / len(hot[1]))
        if len(hot[1]) >= 2:
            cards.append({
                'icon': '🔥', 'title': f"{hot[0]} is hot",
                'detail': f"{grade_from_percentile(avg)} avg grade ({avg}/100) over {len(hot[1])} games",
                'tone': 'good',
            })

    # --- Tilt / streak alerts (per player current streak) -----------------
    streak_alert = None
    for player in unique_sorted(ranked['player_gamertag']):
        p_recent = ranked[ranked['player_gamertag'] == player].head(8)
        streak = 0
        for oc in p_recent['outcome'].astype(str).str.lower():
            if oc not in ('win', 'loss'):
                break
            if streak == 0:
                streak = 1 if oc == 'win' else -1
            elif (oc == 'win') == (streak > 0):
                streak += 1 if streak > 0 else -1
            else:
                break
        if abs(streak) >= 3 and (streak_alert is None or abs(streak) > abs(streak_alert[1])):
            streak_alert = (player, streak)
    if streak_alert:
        p, st = streak_alert
        if st > 0:
            cards.append({'icon': '📈', 'title': f"{p} on a {st}-win streak", 'detail': 'Riding momentum', 'tone': 'good'})
        else:
            cards.append({'icon': '⚠️', 'title': f"{p} is tilting", 'detail': f"{abs(st)} losses in a row", 'tone': 'bad'})

    # --- Map of the week (last 7 days, ≥3 games) --------------------------
    now = pd.Timestamp.now(tz='UTC')
    week = ranked[ranked['date'] >= now - pd.Timedelta(days=7)]
    if not week.empty and 'map' in week.columns:
        wk_by_match = week.drop_duplicates(subset=['match_id']) if 'match_id' in week.columns else week
        map_stats = {}
        for m, mdf in wk_by_match.groupby(wk_by_match['map'].map(normalize_map_name)):
            if not m:
                continue
            g = len(mdf)
            if g < 3:
                continue
            w = int((mdf['outcome'].astype(str).str.lower() == 'win').sum())
            map_stats[m] = (w / g * 100, g)
        if map_stats:
            best_map = max(map_stats.items(), key=lambda kv: kv[1][0])
            cards.append({
                'icon': '🗺️', 'title': f"Best map this week: {best_map[0]}",
                'detail': f"{best_map[1][0]:.0f}% wins over {best_map[1][1]} games",
                'tone': 'good' if best_map[1][0] >= 50 else 'neutral',
            })

    # --- Squad form (last 20 distinct ranked games) -----------------------
    recent = ranked.drop_duplicates(subset=['match_id']).head(20) if 'match_id' in ranked.columns else ranked.head(20)
    if not recent.empty and 'outcome' in recent.columns:
        oc = recent['outcome'].astype(str).str.lower()
        w = int((oc == 'win').sum())
        n = int((oc.isin(['win', 'loss'])).sum())
        if n:
            cards.append({
                'icon': '📊', 'title': f"Recent form: {w}-{n - w} ({w / n * 100:.0f}%)",
                'detail': f"Last {n} ranked games",
                'tone': 'good' if w / n >= 0.5 else 'bad',
            })

    return cards[:5]


_MEDAL_STYLES = {
    'Sniper': ['Snipe', 'No_Scope', 'Counter_snipe', 'Sharpshooter', 'Last_Shot', 'Quigley'],
    'Slayer': ['Killing_Spree', 'Killing_Frenzy', 'Killtacular', 'Killtrocity', 'Overkill',
               'Extermination', 'Perfect', 'Double_Kill', 'Triple_Kill', 'Warrior', 'Rifleman'],
    'Assassin': ['Ninja', 'Back_Smack', 'Bulltrue', 'From_the_Grave', 'Reversal'],
    'Grenadier': ['Grenadier', 'Nade_Shot', 'Remote_Detonation', 'Cluster_Luck', 'Boom_Block'],
    'Objective': ['Flag', 'Skull', 'Hill', 'Zone', 'Stronghold', 'Goal_Line', 'Clock_Stop',
                  'Straight_Balling', 'Hail_Mary', 'Fastball', 'Joust', 'Hold_This', 'Secure_Line',
                  'Signal_Block', 'Call_Blocked', 'Hang_Up', 'Special_Delivery', 'Yard_Sale',
                  'Stopped_Short', 'Harpoon', 'Pull'],
    'Support': ['Guardian_Angel', 'Bodyguard', 'Spotter', 'Shot_Caller', 'Clear_Reception', 'Off_the_Rack'],
}


def build_player_medal_fingerprint(pmf: pd.DataFrame) -> dict | None:
    """Top medals (per game) + a playstyle tag for one player, from medal_df rows."""
    if pmf is None or pmf.empty:
        return None
    games = len(pmf)
    medal_cols = [c for c in pmf.columns
                  if c.startswith('medal_') and not c.startswith('medal_id_') and c != 'medal_count']
    rates = {}
    for c in medal_cols:
        total = float(pd.to_numeric(pmf[c], errors='coerce').fillna(0).sum())
        if total > 0:
            rates[c] = {'name': c[len('medal_'):].replace('_', ' ').strip(),
                        'total': int(total), 'per_game': total / games if games else 0}
    if not rates:
        return None
    top = sorted(rates.values(), key=lambda r: -r['per_game'])[:6]
    style_scores = {}
    for style, keys in _MEDAL_STYLES.items():
        score = 0.0
        for c, info in rates.items():
            short = c[len('medal_'):]
            if any(k.lower() in short.lower() for k in keys):
                score += info['per_game']
        style_scores[style] = score
    tag = max(style_scores, key=style_scores.get) if any(v > 0 for v in style_scores.values()) else 'All-rounder'
    return {
        'tag': tag,
        'top': [{'name': t['name'], 'per_game': round(t['per_game'], 2), 'total': t['total']} for t in top],
        'games': games,
    }


_COACH_TIPS = {
    'Slaying': "Slaying is your weak link — pick cleaner fights with a teammate, trade up, and avoid 1vX peeks.",
    'Gunplay': "Accuracy is dragging you down — slow your first shot, pre-aim common angles, and fight at your strong range.",
    'Impact': "You take about as much damage as you deal — play off teammates, stop over-peeking, and reset when low.",
    'Survival': "You're dying too fast — widen your spacing, hold power positions, and disengage when outnumbered.",
    'Medals': "Low impact-play volume — contest power weapons, set up grenades, and chase multikills off team shots.",
}


def build_player_coach_tip(report_card: dict | None) -> dict | None:
    """Instant rule-based 'fix this tonight' tip from the weakest report-card category."""
    if not report_card or not report_card.get('categories'):
        return None
    weakest = min(report_card['categories'], key=lambda c: c.get('score', 100))
    return {
        'category': weakest['label'],
        'grade': weakest['grade'],
        'grade_class': weakest['grade_class'],
        'score': weakest['score'],
        'tip': _COACH_TIPS.get(weakest['label'], 'Keep grinding — consistency is the next step.'),
    }


def build_player_achievements(player_df: pd.DataFrame) -> list:
    """Earnable badges from a player's match history (ranked-focused). Each is
    {icon, name, desc, earned, detail}. Earned ones light up; the rest show how
    close you are."""
    if player_df is None or player_df.empty:
        return []
    pdf = player_df
    if 'playlist' in pdf.columns:
        r = _ranked_only(pdf)
        if not r.empty:
            pdf = r

    kills = numeric_series(pdf, 'kills')
    deaths = numeric_series(pdf, 'deaths')
    csr = numeric_series(pdf, 'post_match_csr')
    acc = pd.to_numeric(pdf['accuracy'], errors='coerce').dropna() if 'accuracy' in pdf.columns else pd.Series(dtype=float)
    acc = acc * 100 if (not acc.empty and acc.max() <= 1.0) else acc
    games = len(pdf)
    max_kills = int(kills.max()) if len(kills) else 0
    peak_csr = int(csr.max()) if len(csr) else 0
    max_acc = float(acc.max()) if len(acc) else 0.0
    best_win = compute_best_streaks(pdf)[0]
    best_grade = 0
    for _, row in pdf.iterrows():
        sc = _match_grade_for_row(row).get('grade_score')
        if sc is not None and sc > best_grade:
            best_grade = sc
    untouchable = bool(((kills >= 12) & (deaths <= 2)).any()) if len(kills) else False

    defs = [
        ('🏆', 'Onyx', 'Reach 1500 CSR', peak_csr >= 1500, f'peak {peak_csr}'),
        ('💣', '20 Bomb', '20+ kills in a game', max_kills >= 20, f'best {max_kills}'),
        ('⭐', 'S-Tier Game', 'Earn an S game grade', best_grade >= 90, f'best {best_grade}/100'),
        ('🎯', 'Sharpshooter', '60%+ accuracy in a game', max_acc >= 60, f'best {max_acc:.0f}%'),
        ('🛡️', 'Untouchable', '12+ kills, ≤2 deaths', untouchable, 'clean game' if untouchable else 'not yet'),
        ('🔥', 'Hot Streak', '7-game win streak', best_win >= 7, f'best {best_win}W'),
        ('🎖️', 'Veteran', '500+ ranked games', games >= 500, f'{games} games'),
    ]
    return [{'icon': i, 'name': n, 'desc': d, 'earned': bool(e), 'detail': det}
            for (i, n, d, e, det) in defs]


def build_player_scouting_extras(df: pd.DataFrame, player_name: str) -> dict:
    """Data-unlock readouts that use rich columns the UI mostly ignores:
    a squad-relative play ROLE and strength-of-opposition from enemy_team_*."""
    out = {'role': None, 'role_detail': None, 'opp': None}
    if df is None or df.empty or 'player_gamertag' not in df.columns:
        return out
    ranked = _ranked_only(df)

    # --- Role: per-game rate per dimension, ranked vs the squad ------------
    dims = {
        'Slayer': ['kills'],
        'Flag Runner': ['capture_the_flag_stats_flag_captures',
                        'capture_the_flag_stats_flag_grabs',
                        'capture_the_flag_stats_flag_returns'],
        'Zone Anchor': ['zones_stats_stronghold_secures',
                        'zones_stats_stronghold_scoring_ticks'],
        'Skull Keeper': ['oddball_stats_time_as_skull_carrier'],
        'Support': ['assists', 'callout_assists'],
    }
    players = unique_sorted(ranked['player_gamertag']) if not ranked.empty else []
    if len(players) >= 1:
        # per-player per-game rate for each dimension
        rates = {p: {} for p in players}
        for p in players:
            p_df = ranked[ranked['player_gamertag'] == p]
            g = max(len(p_df), 1)
            for dim, cols in dims.items():
                total = sum(float(numeric_series(p_df, c).sum()) for c in cols)
                rates[p][dim] = total / g
        # for THIS player, find the dimension where they rank highest vs squad
        me = rates.get(player_name)
        if me:
            best_dim, best_pct = None, -1.0
            for dim in dims:
                vals = [rates[p][dim] for p in players]
                mine = rates[player_name][dim]
                if len([v for v in vals if v > 0]) < 1:
                    continue
                below = sum(1 for v in vals if v < mine)
                eq = sum(1 for v in vals if v == mine)
                pct = (below + 0.5 * eq) / len(vals) * 100
                if pct > best_pct:
                    best_pct, best_dim = pct, dim
            if best_dim:
                out['role'] = best_dim
                out['role_detail'] = f"top {100 - int(best_pct)}% of the squad in this role" \
                    if best_pct >= 50 else "squad's go-to elsewhere"

    # --- Strength of opposition (enemy_team_* aggregates) ------------------
    p_df = ranked[ranked['player_gamertag'] == player_name]
    if not p_df.empty and 'enemy_team_kills' in p_df.columns:
        ek = numeric_series(p_df, 'enemy_team_kills')
        ed = numeric_series(p_df, 'enemy_team_deaths')
        ea = numeric_series(p_df, 'enemy_team_assists')
        # team KDA per enemy player (teams are 4)
        enemy_kda = (ek + ea / 3 - ed) / 4.0
        avg = float(enemy_kda.mean()) if len(enemy_kda) else 0.0
        # squad-wide baseline for a label
        all_ek = numeric_series(ranked, 'enemy_team_kills')
        all_ed = numeric_series(ranked, 'enemy_team_deaths')
        all_ea = numeric_series(ranked, 'enemy_team_assists')
        base = float(((all_ek + all_ea / 3 - all_ed) / 4.0).mean()) if len(all_ek) else avg
        if avg >= base + 0.5:
            label, tone = 'Tough lobbies', 'bad'
        elif avg <= base - 0.5:
            label, tone = 'Soft lobbies', 'good'
        else:
            label, tone = 'Average lobbies', 'neutral'
        out['opp'] = {'avg_enemy_kda': round(avg, 2), 'label': label, 'tone': tone}
    return out


def build_player_grade_trend(player_df: pd.DataFrame, max_points: int = 30) -> list:
    """Average Game Grade per session over time for one player (oldest→newest),
    so the player page can chart their form as a line."""
    if player_df is None or player_df.empty or 'date' not in player_df.columns:
        return []
    work = player_df.copy()
    ensure_datetime(work)
    work = work.dropna(subset=['date']).sort_values('date')
    if work.empty:
        return []

    sessions = []
    current = []
    last_ts = None
    for _, row in work.iterrows():
        ts = row['date']
        if last_ts is not None and (ts - last_ts).total_seconds() / 60.0 > SESSION_GAP_MINUTES:
            sessions.append(current)
            current = []
        current.append(row)
        last_ts = ts
    if current:
        sessions.append(current)

    points = []
    for sess in sessions:
        scores = [s for s in (_match_grade_for_row(r).get('grade_score') for r in sess) if s is not None]
        if not scores:
            continue
        points.append({
            'date': format_date(sess[-1]['date']),
            'score': round(sum(scores) / len(scores)),
            'games': len(scores),
        })
    return points[-max_points:]


# ---------------------------------------------------------------------------
# Squad Scoreboard + Combat Report  (2026-06-24 "make it cooler / surface data")
# ---------------------------------------------------------------------------
#
# The app already stores a mountain of per-match combat data (power-weapon
# kills, headshots, snipes/no-scopes/perfect medals, grenade & melee kills,
# callouts, killing sprees) that was barely summarised anywhere. These two
# builders turn that into (a) a friendly per-player "scoreboard" so you can see
# how everyone did at a glance, and (b) squad medal/weapon leaderboards.

# CSR tier bands (mirrors stats.csr_to_tier) → label + colour class.
_SB_TIER_BANDS = [
    (1500, 'Onyx', 'tier-onyx'),
    (1200, 'Diamond', 'tier-diamond'),
    (900,  'Platinum', 'tier-platinum'),
    (600,  'Gold', 'tier-gold'),
    (300,  'Silver', 'tier-silver'),
    (1,    'Bronze', 'tier-bronze'),
]


def _sb_tier(csr_val):
    """Return (label, css_class) for a numeric CSR, e.g. (1547, …) → ('Onyx 1547','tier-onyx')."""
    try:
        val = int(float(csr_val))
    except (TypeError, ValueError):
        return ('Unranked', 'tier-none')
    if val <= 0:
        return ('Unranked', 'tier-none')
    if val >= 1500:
        return (f'Onyx {val}', 'tier-onyx')
    for base, name, cls in _SB_TIER_BANDS:
        if val >= base:
            sub = min((val - base) // 50 + 1, 6)
            return (f'{name} {sub}', cls)
    return ('Unranked', 'tier-none')


def _first_col(df, *candidates):
    """Return the first candidate column name that exists in df, else None."""
    for c in candidates:
        if c in df.columns:
            return c
    return None


def latest_arena_csr(p_df):
    """A player's CURRENT rank = the post-match CSR of their most recent
    **Ranked Arena** game (the playlist the rank is based on). The spnkr
    ``current_csr_value`` standing column is currently null, and the last
    ranked game overall can be Ranked Doubles/Slayer/etc. with a separate CSR,
    so neither is reliable. Falls back to any ranked game only if there are no
    Arena games at all."""
    if p_df is None or p_df.empty or 'post_match_csr' not in p_df.columns:
        return None
    sub = p_df
    if 'playlist' in p_df.columns:
        arena = p_df[p_df['playlist'].astype(str).str.contains('Arena', case=False, na=False)]
        if not arena.empty:
            sub = arena
    if 'date' in sub.columns:
        sub = sub.sort_values('date')
    pv = pd.to_numeric(sub['post_match_csr'], errors='coerce')
    pv = pv[pv > 0].dropna()
    return float(pv.iloc[-1]) if not pv.empty else None


# A "Snipe" medal is a scoped sniper-rifle kill; "No Scope" is the unscoped one.
# Together they are a player's true sniper-kill count — reporting medal_snipe
# alone undercounts MnK players who rack up no-scopes. Counter-Snipe is left out
# of the sum because it co-awards on top of a Snipe/No-Scope (would double count).
_SNIPER_MEDAL_COLS = ('medal_snipe', 'medal_no_scope')


def sniper_series(df: pd.DataFrame) -> pd.Series:
    """Per-row total sniper kills (Snipe + No-Scope medals)."""
    total = None
    for c in _SNIPER_MEDAL_COLS:
        if c in df.columns:
            s = numeric_series(df, c)
            total = s if total is None else total.add(s, fill_value=0)
    if total is None:
        return pd.Series(0.0, index=df.index)
    return total


# Combat "signature" categories. Each is a per-game (or pct) rate; the squad
# leader earns a crown and everyone gets tagged with their strongest tendency.
# (extractor returns a per-player total; we derive the rate from games.)
_SIG_CATS = [
    {'key': 'snipe',   'label': 'Sniper',      'emoji': '🎯', 'cols': _SNIPER_MEDAL_COLS,     'rate': 'pg', 'desc': 'Sniper kills (snipe + no-scope)'},
    {'key': 'power',   'label': 'Power Hungry','emoji': '💥', 'cols': ('power_weapon_kills',), 'rate': 'pg', 'desc': 'Power-weapon kills'},
    {'key': 'head',    'label': 'Headhunter',  'emoji': '💀', 'cols': ('headshot_kills',),     'rate': 'hs', 'desc': 'Headshot rate'},
    {'key': 'nade',    'label': 'Grenadier',   'emoji': '🧨', 'cols': ('grenade_kills',),      'rate': 'pg', 'desc': 'Grenade kills'},
    {'key': 'melee',   'label': 'Brawler',     'emoji': '🔨', 'cols': ('melee_kills',),        'rate': 'pg', 'desc': 'Melee kills'},
    {'key': 'callout', 'label': 'Shotcaller',  'emoji': '📢', 'cols': ('callout_assists',),    'rate': 'pg', 'desc': 'Callout assists'},
    {'key': 'spree',   'label': 'Spree King',  'emoji': '🔥', 'cols': ('max_killing_spree',),  'rate': 'avg','desc': 'Avg best spree'},
    {'key': 'acc',     'label': 'Deadeye',     'emoji': '✨', 'cols': ('shots_hit',),          'rate': 'acc','desc': 'Accuracy'},
]

# Pretty names for the "top medal" / leaderboard medal columns.
_MEDAL_PRETTY = {
    'medal_snipe': 'Snipe', 'medal_no_scope': 'No Scope', 'medal_perfect': 'Perfect',
    'medal_ninja': 'Ninja', 'medal_killjoy': 'Killjoy', 'medal_killing_spree': 'Killing Spree',
    'medal_double_kill': 'Double Kill', 'medal_triple_kill': 'Triple Kill',
    'medal_overkill': 'Overkill', 'medal_killtacular': 'Killtacular',
    'medal_killing_frenzy': 'Killing Frenzy', 'medal_counter_snipe': 'Counter Snipe',
    'medal_marksman': 'Marksman', 'medal_grenadier': 'Grenadier', 'medal_nade_shot': 'Nade Shot',
    'medal_back_smack': 'Back Smack', 'medal_bulltrue': 'Bulltrue', 'medal_360': '360',
    'medal_gunslinger': 'Gunslinger', 'medal_pancake': 'Pancake',
}


def _medal_label(col):
    if col in _MEDAL_PRETTY:
        return _MEDAL_PRETTY[col]
    return col.replace('medal_', '').replace('_', ' ').title()


def _ranked_only(df):
    """The single ranked-playlist filter + copy boundary for all builders.

    The data layer now loads Ranked Arena rows only (load_dataframe WHERE
    playlist ILIKE 'Ranked Arena'), so the filter normally passes every row —
    the fast path skips the re-index and just hands back a defensive copy."""
    if df is None or df.empty:
        return df
    if 'playlist' not in df.columns:
        return df.copy()
    mask = df['playlist'].astype(str).str.contains('Ranked', case=False, na=False)
    return df.copy() if bool(mask.all()) else df[mask].copy()


SCOREBOARD_RANGES = {'season': None, '30': 30, '60': 60, '90': 90, '180': 180, '1y': 365, '2y': 730, 'lifetime': None}
SCOREBOARD_LABELS = {'season': 'Season', '30': '30d', '60': '60d', '90': '90d', '180': '180d',
                     '1y': '1y', '2y': '2y', 'lifetime': 'All'}


SCOREBOARD_STACKS = {'all': 'All games', '4': '4-stack', '3': '3-stack', '2': '2-stack', 'solo': 'Solo'}


def _stack_slice(mdf: pd.DataFrame, stack: str) -> pd.DataFrame:
    """Filter to games by how many tracked players queued together: exactly N,
    or 'solo' (a lone tracked player). 'all' = no filter."""
    if mdf is None or mdf.empty or stack == 'all' or 'match_id' not in mdf.columns or 'player_gamertag' not in mdf.columns:
        return mdf
    ppm = mdf.groupby('match_id')['player_gamertag'].nunique()
    if stack == 'solo':
        ids = set(ppm[ppm == 1].index)
    else:
        try:
            ids = set(ppm[ppm == int(stack)].index)
        except (TypeError, ValueError):
            return mdf
    sliced = mdf[mdf['match_id'].isin(ids)]
    return sliced if not sliced.empty else mdf


def _scoreboard_slice(mdf: pd.DataFrame, rng: str) -> pd.DataFrame:
    """Filter the scoreboard frame to a time range. 'season' = the latest season
    (season of the most recent match); day-windows for the rest; lifetime = all."""
    if mdf is None or mdf.empty:
        return mdf
    d = mdf.copy()
    if 'date' in d.columns:
        ensure_datetime(d)
    if rng == 'season' and 'season_id' in d.columns and d['season_id'].notna().any():
        dd = d.dropna(subset=['date']) if 'date' in d.columns else d
        if not dd.empty:
            latest_season = dd.sort_values('date')['season_id'].dropna().iloc[-1]
            sliced = d[d['season_id'].astype(str) == str(latest_season)]
            if not sliced.empty:
                return sliced
    days = SCOREBOARD_RANGES.get(rng)
    if days and 'date' in d.columns:
        cutoff = pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=days)
        sliced = d[d['date'] >= cutoff]
        return sliced if not sliced.empty else d
    return d  # lifetime / fallback


def build_combat_report(mdf: pd.DataFrame) -> dict:
    """Per-player combat scoreboard + squad medal/weapon leaderboards.

    Reads the medal-inclusive dataframe so the rich per-medal / weapon columns
    are available (the lean cache drops them). Degrades to an empty report on
    any missing data rather than raising.
    """
    empty = {'scoreboard': [], 'leaders': [], 'has_data': False}
    if mdf is None or mdf.empty or 'player_gamertag' not in mdf.columns:
        return empty

    rdf = _ranked_only(mdf)
    if rdf.empty:
        rdf = mdf.copy()
    if 'date' in rdf.columns:
        ensure_datetime(rdf)

    # Current CSR per player (rich columns → fallback to post_match_csr).
    csr_by_player: dict[str, float] = {}
    try:
        for s in fetch_csr_standings(ENGINE):
            v = s.get('current_csr_value')
            if v is not None:
                csr_by_player[s['player_gamertag']] = float(v)
    except Exception:
        pass

    players = unique_sorted(rdf['player_gamertag'])
    rows: list[dict] = []
    # Stash per-player totals so signatures + leaderboards can reuse them.
    totals: dict[str, dict] = {}

    for player in players:
        p_df = rdf[rdf['player_gamertag'] == player]
        games = len(p_df)
        if games == 0:
            continue
        if 'date' in p_df.columns:
            p_df = p_df.sort_values('date')

        outcomes = p_df['outcome'].astype(str).str.lower() if 'outcome' in p_df.columns else pd.Series(dtype=str)
        wins = int((outcomes == 'win').sum())
        decided = int(outcomes.isin(['win', 'loss']).sum())
        win_pct = wins / decided * 100 if decided else 0.0

        # Last-10 form pips (most recent last).
        recent_outcomes = list(outcomes.tail(10))
        wl_form = [('W' if o == 'win' else 'L') for o in recent_outcomes if o in ('win', 'loss')]

        k = numeric_series(p_df, 'kills').sum()
        d = numeric_series(p_df, 'deaths').sum()
        a = numeric_series(p_df, 'assists').sum()
        kpg, dpg, apg = k / games, d / games, a / games
        kda = safe_kda(kpg, apg, dpg)

        fired = numeric_series(p_df, 'shots_fired').sum()
        hit = numeric_series(p_df, 'shots_hit').sum()
        accuracy = hit / fired * 100 if fired > 0 else 0.0

        pw = numeric_series(p_df, 'power_weapon_kills').sum() if 'power_weapon_kills' in p_df.columns else 0.0
        hs = numeric_series(p_df, 'headshot_kills').sum() if 'headshot_kills' in p_df.columns else 0.0
        gren = numeric_series(p_df, 'grenade_kills').sum() if 'grenade_kills' in p_df.columns else 0.0
        melee = numeric_series(p_df, 'melee_kills').sum() if 'melee_kills' in p_df.columns else 0.0
        callout = numeric_series(p_df, 'callout_assists').sum() if 'callout_assists' in p_df.columns else 0.0
        snipes = sniper_series(p_df).sum()
        spree_avg = float(numeric_series(p_df, 'max_killing_spree').mean() or 0.0) if 'max_killing_spree' in p_df.columns else 0.0
        hs_pct = hs / k * 100 if k > 0 else 0.0

        # Top medal (by total count) for this player, excluding raw id columns.
        top_medal = ('—', 0)
        medal_cols = [c for c in p_df.columns if c.startswith('medal_') and not c.startswith('medal_id') and c != 'medal_count']
        best = 0.0
        for c in medal_cols:
            tot = numeric_series(p_df, c).sum()
            if tot > best:
                best = tot
                top_medal = (_medal_label(c), int(tot))

        # Current rank = last Ranked Arena CSR (see latest_arena_csr). Only fall
        # back to the spnkr standings snapshot if there's no Arena CSR at all.
        csr_val = latest_arena_csr(p_df)
        if csr_val is None:
            csr_val = csr_by_player.get(player)
        tier_label, tier_class = _sb_tier(csr_val)

        totals[player] = {
            'games': games, 'snipe': snipes, 'power': pw, 'head': hs,
            'nade': gren, 'melee': melee, 'callout': callout,
            'spree': spree_avg * games, 'acc': hit, 'fired': fired,
            'kills': k, 'hs_pct': hs_pct,
        }

        rows.append({
            'player': player,
            'css': get_player_class(player),
            'games': games,
            'win_pct': format_pct(win_pct),
            'win_pct_num': round(win_pct),
            'wins': wins,
            'losses': decided - wins,
            'wl_form': wl_form,
            'kda': format_float(kda, 2),
            'kda_min': format_float(kda_per_min(p_df), 2),
            'obj_min': _obj_dash(obj_per_min(p_df), 1),
            'kda_num': round(kda, 2),
            'kpg': format_float(kpg, 1),
            'dpg': format_float(dpg, 1),
            'apg': format_float(apg, 1),
            'accuracy': format_pct(accuracy),
            'csr': int(csr_val) if csr_val else '—',
            'csr_num': csr_val or 0,
            'tier_label': tier_label,
            'tier_class': tier_class,
            'pw_pg': format_float(pw / games, 1),
            'hs_pct': format_pct(hs_pct),
            'snipes': format_int(snipes),
            'gren_pg': format_float(gren / games, 1),
            'spree': format_float(spree_avg, 1),
            'callout_pg': format_float(callout / games, 1),
            'top_medal': top_medal[0],
            'top_medal_count': format_int(top_medal[1]),
        })

    if not rows:
        return empty

    # ── Signatures: rank each player per category, leader gets the crown ──
    def _rate(player, cat):
        t = totals.get(player, {})
        g = t.get('games', 0) or 1
        if cat['rate'] == 'hs':
            return t.get('hs_pct', 0.0)
        if cat['rate'] == 'acc':
            fired = t.get('fired', 0)
            return (t.get('acc', 0) / fired * 100) if fired else 0.0
        if cat['rate'] == 'avg':
            return t.get(cat['key'], 0.0) / g  # spree stored as avg*games
        return t.get(cat['key'], 0.0) / g

    cat_values = {c['key']: {p: _rate(p, c) for p in totals} for c in _SIG_CATS}
    cat_leader = {}
    for c in _SIG_CATS:
        vals = cat_values[c['key']]
        if vals and max(vals.values()) > 0:
            cat_leader[c['key']] = max(vals, key=vals.get)

    # Each player's signature = the category in which they rank highest
    # (by share of the squad-max for that category), so everyone gets a flavour.
    cat_max = {c['key']: (max(cat_values[c['key']].values()) or 1) for c in _SIG_CATS}
    for row in rows:
        p = row['player']
        best_cat, best_share = None, -1.0
        for c in _SIG_CATS:
            share = cat_values[c['key']].get(p, 0) / cat_max[c['key']]
            if share > best_share:
                best_share, best_cat = share, c
        if best_cat and best_share > 0:
            row['sig_label'] = best_cat['label']
            row['sig_emoji'] = best_cat['emoji']
            row['sig_leader'] = cat_leader.get(best_cat['key']) == p
        else:
            row['sig_label'] = ''
            row['sig_emoji'] = ''
            row['sig_leader'] = False

    # Overall grade = the ABSOLUTE report-card grade (identical to the player
    # page), so a card's letter matches what you see when you click into it.
    # Squad-relative percentile grading is reserved for explicit ranking tables.
    for row in rows:
        try:
            rc = build_player_report_card(rdf[rdf['player_gamertag'] == row['player']])
            if rc and rc.get('overall'):
                row['grade'] = rc['overall']['grade']
                row['grade_class'] = rc['overall']['grade_class']
                row['grade_tip'] = f"Overall skill grade (absolute, same as player page) · {rc['games']} ranked games"
            else:
                row.setdefault('grade', '—')
                row.setdefault('grade_class', '')
        except Exception:
            row.setdefault('grade', '—')
            row.setdefault('grade_class', '')

    # Sort cards by CSR (how everyone is doing, best first).
    rows.sort(key=lambda r: r['csr_num'], reverse=True)

    # ── Squad leaderboards (medals + weapons) ──
    leader_specs = [
        ('🎯', 'Sniper Kills', _SNIPER_MEDAL_COLS, 'count'),
        ('💥', 'Power Weapon Kills', 'power_weapon_kills', 'pg'),
        ('💀', 'Headshot Kills', 'headshot_kills', 'pg'),
        ('🚫', 'No-Scopes', 'medal_no_scope', 'count'),
        ('🥷', 'Ninjas', 'medal_ninja', 'count'),
        ('🛡️', 'Killjoys', 'medal_killjoy', 'count'),
        ('✨', 'Perfect Kills', 'medal_perfect', 'count'),
        ('🧨', 'Grenade Kills', 'grenade_kills', 'pg'),
        ('🔨', 'Melee Kills', 'melee_kills', 'pg'),
        ('📢', 'Callout Assists', 'callout_assists', 'pg'),
    ]
    leaders = []
    for emoji, label, col, mode in leader_specs:
        cols = col if isinstance(col, tuple) else (col,)
        if not any(c in rdf.columns for c in cols):
            continue
        lrows = []
        for player in players:
            p_df = rdf[rdf['player_gamertag'] == player]
            g = len(p_df)
            if g == 0:
                continue
            total = sum(numeric_series(p_df, c).sum() for c in cols if c in p_df.columns)
            if total <= 0:
                continue
            minutes = float(numeric_series(p_df, 'duration').sum()) / 60.0 if 'duration' in p_df.columns else 0.0
            per_min = (total / minutes) if minutes > 0 else 0.0
            lrows.append({
                'player': player, 'css': get_player_class(player),
                'total': int(total), 'total_fmt': format_int(total),
                'per_game': format_float(total / g, 2), 'val': total,
                'pg_val': total / g,
                'per_min': format_float(per_min, 2), 'min_val': per_min,
            })
        if not lrows:
            continue
        # Per-game is the headline metric everywhere now, so rank by it.
        lrows.sort(key=lambda x: x['pg_val'], reverse=True)
        for i, lr in enumerate(lrows):
            lr['rank'] = i + 1
            lr['is_leader'] = (i == 0)
        leaders.append({
            'emoji': emoji, 'label': label, 'mode': mode, 'rows': lrows,
        })

    return {'scoreboard': rows, 'leaders': leaders, 'has_data': True}


# Notable medals worth shouting about in a session recap (col → pretty label).
_NOTABLE_MEDALS = [
    ('medal_snipe', '🎯', 'Snipe'),
    ('medal_no_scope', '🚫', 'No-Scope'),
    ('medal_perfect', '✨', 'Perfect'),
    ('medal_ninja', '🥷', 'Ninja'),
    ('medal_killjoy', '🛡️', 'Killjoy'),
    ('medal_extermination', '☠️', 'Extermination'),
    ('medal_overkill', '💢', 'Overkill'),
    ('medal_killtacular', '🔥', 'Killtacular'),
    ('medal_killtrocity', '⚡', 'Killtrocity'),
    ('medal_killing_frenzy', '😤', 'Killing Frenzy'),
    ('medal_double_kill', '✌️', 'Double Kill'),
    ('medal_triple_kill', '🎰', 'Triple Kill'),
    ('medal_counter_snipe', '🎯', 'Counter-Snipe'),
    ('medal_back_smack', '🔪', 'Back Smack'),
    ('medal_bulltrue', '🐂', 'Bulltrue'),
    ('medal_nade_shot', '🧨', 'Nade Shot'),
    ('medal_360', '🌀', '360'),
    ('medal_grenadier', '💣', 'Grenadier'),
]


def _latest_squad_session(rdf: pd.DataFrame):
    """Return (session_df, date_str, game_count, players) for the most recent
    SQUAD session — only matches with 2+ tracked players together. Solo games
    (and solo grinds) are excluded entirely. Works off the medal-inclusive df."""
    if rdf.empty or 'match_id' not in rdf.columns or 'date' not in rdf.columns:
        return None
    rdf = rdf.copy()
    ensure_datetime(rdf)
    rdf = rdf.dropna(subset=['date'])
    if rdf.empty:
        return None
    # Restrict to SQUAD matches (2+ tracked players together) BEFORE clustering, so
    # the highlights always cover the latest genuine squad night — never a solo
    # grind, and never the solo games that bookend a squad session.
    ppm = rdf.groupby('match_id')['player_gamertag'].nunique()
    squad_ids = set(ppm[ppm >= 2].index)
    if not squad_ids:
        return None
    pool = rdf[rdf['match_id'].isin(squad_ids)]
    match_times = pool.groupby('match_id')['date'].max().sort_values(ascending=False)
    if match_times.empty:
        return None
    session_ids = latest_session_match_ids(match_times)
    session_df = pool[pool['match_id'].isin(session_ids)].copy()
    players = list(session_df['player_gamertag'].unique())
    if not session_ids:
        return None
    return (session_df, format_date(session_df['date'].max()),
            session_df['match_id'].nunique(), players)


def _cluster_squad_sessions(rdf: pd.DataFrame):
    """Return a list of play sessions (each a list of match_ids, oldest→newest),
    clustering ALL tracked matches by SESSION_GAP_MINUTES gaps. Solo-only nights
    (a single tracked player) form their own sessions too — they're tagged solo
    vs squad downstream from the per-match player counts."""
    if rdf.empty or 'match_id' not in rdf.columns or 'date' not in rdf.columns:
        return [], None
    work = rdf.copy()
    ensure_datetime(work)
    work = work.dropna(subset=['date'])
    if work.empty:
        return [], None
    mt = work.groupby('match_id')['date'].max().sort_values()
    ids = list(mt.index)
    times = [pd.Timestamp(t) for t in mt.values]
    gap = pd.Timedelta(minutes=SESSION_GAP_MINUTES)
    sessions, cur = [], [ids[0]]
    for i in range(1, len(ids)):
        if (times[i] - times[i - 1]) <= gap:
            cur.append(ids[i])
        else:
            sessions.append(cur)
            cur = [ids[i]]
    sessions.append(cur)
    return sessions, work


def build_squad_sessions(rdf: pd.DataFrame, max_sessions: int = 60) -> list:
    """Summary of every recent squad session (2+ tracked players), newest first:
    date, games, W-L, win%, squad avg KDA, and avg CSR change across players."""
    sessions, work = _cluster_squad_sessions(rdf)
    if not sessions:
        return []
    out = []
    for sess_ids in sessions:
        sdf = work[work['match_id'].isin(sess_ids)]
        if sdf.empty:
            continue
        mview = sdf.drop_duplicates('match_id')
        oc = mview['outcome'].astype(str).str.lower() if 'outcome' in mview.columns else pd.Series(dtype=str)
        wins = int((oc == 'win').sum())
        losses = int((oc == 'loss').sum())
        games = int(mview['match_id'].nunique())
        decided = wins + losses
        win_pct = wins / decided * 100 if decided else 0.0
        kda_vals = sdf.apply(lambda r: safe_kda(r.get('kills', 0), r.get('assists', 0), r.get('deaths', 0)), axis=1)
        avg_kda = float(kda_vals.mean()) if not kda_vals.empty else 0.0
        # Perfect-kill medals per player-game across the session.
        perfect_total = int(numeric_series(sdf, 'medal_perfect').sum())
        player_games = len(sdf)
        perfect_pg = perfect_total / player_games if player_games else 0.0
        # Avg CSR change across players (last post − first pre within the session).
        csr_deltas = []
        for _p, g in sdf.groupby('player_gamertag'):
            g = g.sort_values('date')
            post = pd.to_numeric(g.get('post_match_csr'), errors='coerce')
            pre = pd.to_numeric(g.get('pre_match_csr'), errors='coerce')
            post = post[post > 0]
            pre = pre[pre > 0]
            if not post.empty and not pre.empty:
                csr_deltas.append(float(post.iloc[-1]) - float(pre.iloc[0]))
        avg_csr_delta = sum(csr_deltas) / len(csr_deltas) if csr_deltas else None
        anchor_mid = mview.sort_values('date')['match_id'].iloc[-1]
        end_date = sdf['date'].max()
        # Squad vs solo: a session counts as "squad" if any match had 2+ tracked
        # players together; otherwise it's a solo night.
        per_match_players = sdf.groupby('match_id')['player_gamertag'].nunique()
        squad_games = int((per_match_players >= 2).sum())
        kind = 'squad' if squad_games > 0 else 'solo'
        out.append({
            'sid': str(anchor_mid),
            'date': format_date(end_date),
            'date_sort': end_date.isoformat(),
            'games': games,
            'wins': wins,
            'losses': losses,
            'record': f'{wins}-{losses}',
            'win_pct': round(win_pct, 0),
            'win_pct_str': f'{win_pct:.0f}%',
            'avg_kda': format_float(avg_kda, 2),
            'avg_kda_num': round(avg_kda, 2),
            'perfect_pg': format_float(perfect_pg, 2),
            'perfect_total': perfect_total,
            'kind': kind,
            'squad_games': squad_games,
            'players': sorted(sdf['player_gamertag'].unique()),
            'csr_delta': format_signed(avg_csr_delta, 0) if avg_csr_delta is not None else '—',
            'csr_delta_num': round(avg_csr_delta, 0) if avg_csr_delta is not None else None,
        })
    out.sort(key=lambda s: s['date_sort'], reverse=True)
    return out[:max_sessions]


def build_session_detail(rdf: pd.DataFrame, sid: str) -> dict | None:
    """Per-player table + game-by-game rundown for the squad session that
    contains match_id ``sid``."""
    sessions, work = _cluster_squad_sessions(rdf)
    if not sessions or work is None:
        return None
    target = next((s for s in sessions if sid in [str(x) for x in s]), None)
    if target is None:
        return None
    sdf = work[work['match_id'].isin(target)].copy()
    if sdf.empty:
        return None
    mview = sdf.drop_duplicates('match_id')
    oc = mview['outcome'].astype(str).str.lower() if 'outcome' in mview.columns else pd.Series(dtype=str)
    wins, losses = int((oc == 'win').sum()), int((oc == 'loss').sum())
    decided = wins + losses
    mapcol = _first_col(sdf, 'map')
    modecol = _first_col(sdf, 'game_type')
    hill_games = _hill_game_count(sdf)
    obj_thresholds = _objective_thresholds(rdf)

    # Per-player session stats.
    player_rows = []
    for p, g in sdf.groupby('player_gamertag'):
        po = g['outcome'].astype(str).str.lower() if 'outcome' in g.columns else pd.Series(dtype=str)
        k = numeric_series(g, 'kills').sum()
        d = numeric_series(g, 'deaths').sum()
        a = numeric_series(g, 'assists').sum()
        perf = numeric_series(g, 'medal_perfect').sum()
        hill = numeric_series(g, HILL_TIME_COL).sum()
        games = len(g)
        player_rows.append({
            'player': p, 'css': get_player_class(p),
            'games': games,
            'record': f"{int((po == 'win').sum())}-{int((po == 'loss').sum())}",
            'kda': format_float(safe_kda(k / games, a / games, d / games) if games else 0, 2),
            'kda_min': format_float(kda_per_min(g), 2),
            'obj_min': _obj_dash(obj_per_min(g), 1),
            'kills': format_int(k), 'deaths': format_int(d), 'assists': format_int(a),
            'perfect_pg': format_float(perf / games if games else 0, 2),
            'perfect_total': int(perf),
            'hill_pg_secs': (hill / hill_games) if hill_games else 0.0,
            'hill_pg': format_mmss(hill / hill_games) if hill_games else '',
            'hill_total': format_mmss(hill),
            'hill_games': hill_games,
        })
    add_composite_grades(player_rows, {'kda': True}, 'Session grade')
    player_rows.sort(key=lambda r: to_number(r['kda']) or 0, reverse=True)

    # Game-by-game (chronological, oldest first).
    games_list = []
    ordered = mview.sort_values('date')
    for gi, (_, mrow) in enumerate(ordered.iterrows(), 1):
        mid = mrow.get('match_id')
        g = sdf[sdf['match_id'] == mid]
        ts = pd.to_datetime(mrow.get('date'), utc=True, errors='coerce')
        time_str = ''
        if pd.notna(ts):
            try:
                ts = ts.tz_convert(APP_TIMEZONE)
            except Exception:
                pass
            time_str = ts.strftime('%I:%M %p').lstrip('0')
        outcome = str(mrow.get('outcome', '')).lower()
        prows = []
        for _, row in g.iterrows():
            kda = safe_kda(row.get('kills', 0), row.get('assists', 0), row.get('deaths', 0))
            prows.append({'player': row.get('player_gamertag', '?'), 'css': get_player_class(row.get('player_gamertag', '')),
                          'kda': format_float(kda, 1), 'kda_num': kda,
                          'kda_min': format_float(safe_float(row.get('kda/min')), 2),
                          'obj_min': format_float(safe_float(row.get('obj/min')), 1),
                          'objs': _objective_chips(row, obj_thresholds),
                          'line': f"{int(safe_float(row.get('kills', 0)))}/{int(safe_float(row.get('deaths', 0)))}/{int(safe_float(row.get('assists', 0)))}"})
        prows.sort(key=lambda x: x['kda_num'], reverse=True)
        games_list.append({
            'num': gi, 'time': time_str, 'match_id': mid,
            'map': normalize_map_name(mrow.get(mapcol)) if mapcol else '',
            'mode': clean_mode(mrow.get(modecol)) if modecol else '',
            'result': 'Win' if outcome == 'win' else ('Loss' if outcome == 'loss' else '—'),
            'result_class': 'outcome-win' if outcome == 'win' else ('outcome-loss' if outcome == 'loss' else ''),
            'players': prows,
        })

    win_pct = wins / decided * 100 if decided else 0
    per_match_players = sdf.groupby('match_id')['player_gamertag'].nunique()
    squad_games = int((per_match_players >= 2).sum())
    return {
        'sid': sid,
        'date': format_date(sdf['date'].max()),
        'games': int(mview['match_id'].nunique()),
        'record': f'{wins}-{losses}',
        'win_pct_str': f'{win_pct:.0f}%',
        'kind': 'squad' if squad_games > 0 else 'solo',
        'squad_games': squad_games,
        'players': player_rows,
        'session_games': games_list,
        'hill_games': hill_games,
        'has_hill': hill_games > 0 and any(r.get('hill_pg_secs', 0) > 0 for r in player_rows),
    }


def build_session_highlights(mdf: pd.DataFrame) -> dict:
    """Standout single-game feats, medal haul and outlier ('crazy good') games
    from the most recent squad session. Degrades to empty, never raises."""
    empty = {'has_data': False, 'superlatives': [], 'objective_standouts': [],
             'medal_haul': [], 'medal_haul_by_player': [], 'crazy_games': [],
             'map_summary': [], 'mode_summary': [], 'opponent_threats': [],
             'session_games': []}
    if mdf is None or mdf.empty or 'player_gamertag' not in mdf.columns:
        return empty
    rdf_all = _ranked_only(mdf)
    if rdf_all.empty:
        return empty
    sess = _latest_squad_session(rdf_all)
    if not sess:
        return empty
    session_df, date_str, game_count, players = sess
    # Squad night if any match had 2+ tracked players together; else solo grind.
    _ses_ppm = session_df.groupby('match_id')['player_gamertag'].nunique()
    session_kind = 'squad' if int((_ses_ppm >= 2).sum()) > 0 else 'solo'
    # Combined sniper-kill column (Snipe + No-Scope) for the sniping superlative.
    session_df = session_df.copy()
    session_df['sniper_kills'] = sniper_series(session_df)

    mapcol = _first_col(session_df, 'map')

    def _map_of(row):
        return normalize_map_name(row.get(mapcol)) if mapcol else ''

    # ── Single-game superlatives ──
    superlatives = []

    def _top_feat(emoji, label, col, suffix, win_only=False, lowest=False, min_val=1, max_val=None):
        if col not in session_df.columns:
            return
        sub = session_df
        if win_only and 'outcome' in sub.columns:
            sub = sub[sub['outcome'].astype(str).str.lower() == 'win']
        if sub.empty:
            return
        vals = pd.to_numeric(sub[col], errors='coerce')
        if vals.dropna().empty:
            return
        idx = vals.idxmin() if lowest else vals.idxmax()
        v = vals.loc[idx]
        if pd.isna(v) or (not lowest and v < min_val):
            return
        # For "lowest is best" feats (fewest deaths) only celebrate genuinely
        # strong games — a session min of 16 deaths is not a lockdown.
        if lowest and max_val is not None and v > max_val:
            return
        row = sub.loc[idx]
        unit = suffix.rstrip('s') if int(v) == 1 and suffix.endswith('s') else suffix
        superlatives.append({
            'emoji': emoji, 'label': label,
            'player': row.get('player_gamertag', '?'),
            'css': get_player_class(row.get('player_gamertag', '')),
            'value': f"{int(v)}{unit}",
            'detail': _map_of(row),
            'match_id': row.get('match_id', ''),
        })

    # KDA superlative computed separately (derived stat).
    kda_series = session_df.apply(
        lambda r: safe_kda(r.get('kills', 0), r.get('assists', 0), r.get('deaths', 0)), axis=1)
    if not kda_series.empty and kda_series.max() > 0:
        idx = kda_series.idxmax()
        row = session_df.loc[idx]
        superlatives.append({
            'emoji': '💥', 'label': 'Best Game (KDA)',
            'player': row.get('player_gamertag', '?'),
            'css': get_player_class(row.get('player_gamertag', '')),
            'value': format_float(kda_series.loc[idx], 1),
            'detail': f"{int(safe_float(row.get('kills',0)))}/"
                      f"{int(safe_float(row.get('deaths',0)))}/"
                      f"{int(safe_float(row.get('assists',0)))} · {_map_of(row)}",
            'match_id': row.get('match_id', ''),
        })

    _top_feat('⚔️', 'Most Kills', 'kills', ' kills', min_val=1)
    _top_feat('🎯', 'Most Snipes (1 game)', 'sniper_kills', ' snipes', min_val=1)
    _top_feat('🔥', 'Biggest Spree', 'max_killing_spree', ' spree', min_val=2)
    _top_feat('💢', 'Power Trip', 'power_weapon_kills', ' pwr kills', min_val=2)
    _top_feat('🧨', 'Grenade Master', 'grenade_kills', ' nade kills', min_val=2)
    _top_feat('📢', 'Most Callouts', 'callout_assists', ' callouts', min_val=2)
    _top_feat('🛡️', 'Lockdown (fewest deaths, win)', 'deaths', ' deaths', win_only=True, lowest=True, max_val=10)

    # ── Objective standouts (auto-detect any objective stat, single best game,
    #    only surfaced when it was genuinely good vs the last-year history) ──
    objective_standouts = []
    for col, emoji, label, kind in OBJECTIVE_FEAT_CATALOG:
        if col not in session_df.columns:
            continue
        sess_vals = pd.to_numeric(session_df[col], errors='coerce')
        sess_vals = sess_vals.dropna()
        if sess_vals.empty:
            continue
        idx = sess_vals.idxmax()
        v = float(sess_vals.loc[idx])
        # Absolute floor so trivial values (1 grab, 5s on a flag) never show.
        floor = 30.0 if kind == 'time' else 2.0
        if v < floor:
            continue
        # "Really good" gate: must be top-10% historically for that stat among
        # games where it actually happened (non-zero). Needs enough history to
        # be meaningful; with thin history we fall back to the absolute floor.
        hist_vals = pd.to_numeric(rdf_all[col], errors='coerce') if col in rdf_all.columns else pd.Series(dtype=float)
        hist_vals = hist_vals[hist_vals > 0].dropna()
        pct = None
        if len(hist_vals) >= 15:
            # Gate and display use the SAME percentile so "top X%" is never
            # contradicted by the threshold. Must be genuinely top-10%.
            pct = _pct_rank(v, hist_vals.values)
            if pct < 90:
                continue
        row = session_df.loc[idx]
        detail_bits = []
        if pct is not None:
            detail_bits.append(f"top {max(1, round(100 - pct))}%")
        mp = _map_of(row)
        if mp:
            detail_bits.append(mp)
        objective_standouts.append({
            'emoji': emoji, 'label': label,
            'player': row.get('player_gamertag', '?'),
            'css': get_player_class(row.get('player_gamertag', '')),
            'value': format_mmss(v) if kind == 'time' else f"{int(v)}",
            'detail': ' · '.join(detail_bits),
            'match_id': row.get('match_id', ''),
            '_rank': pct if pct is not None else 90.0,
            '_v': v,
        })
    # Best (most elite vs history) first; cap so the section stays scannable.
    objective_standouts.sort(key=lambda x: (x['_rank'], x['_v']), reverse=True)
    objective_standouts = objective_standouts[:8]
    for o in objective_standouts:
        o.pop('_rank', None)
        o.pop('_v', None)

    # ── Session medal haul (squad-wide totals of notable medals) ──
    medal_haul = []
    for col, emoji, label in _NOTABLE_MEDALS:
        if col not in session_df.columns:
            continue
        total = int(pd.to_numeric(session_df[col], errors='coerce').fillna(0).sum())
        if total > 0:
            medal_haul.append({'emoji': emoji, 'label': label, 'count': total})
    medal_haul.sort(key=lambda m: m['count'], reverse=True)
    medal_haul = medal_haul[:10]

    # ── Per-player medal haul this session (who earned what) ──
    medal_haul_by_player = []
    for player in players:
        p_df = session_df[session_df['player_gamertag'] == player]
        if p_df.empty:
            continue
        pmedals = []
        ptotal = 0
        for col, emoji, label in _NOTABLE_MEDALS:
            if col not in p_df.columns:
                continue
            c = int(pd.to_numeric(p_df[col], errors='coerce').fillna(0).sum())
            if c > 0:
                pmedals.append({'emoji': emoji, 'label': label, 'count': c})
                ptotal += c
        if not pmedals:
            continue
        pmedals.sort(key=lambda m: m['count'], reverse=True)
        medal_haul_by_player.append({
            'player': player, 'css': get_player_class(player),
            'total': ptotal, 'medals': pmedals,
        })
    medal_haul_by_player.sort(key=lambda x: x['total'], reverse=True)

    # ── Map & game-type summaries (squad shares one result per match) ──
    match_view = session_df.drop_duplicates('match_id')

    def _breakdown_by(col, clean=None):
        out = []
        if col not in match_view.columns:
            return out
        for name, grp in match_view.groupby(col):
            label = clean(name) if clean else str(name).strip()
            if not label:
                continue
            g = len(grp)
            w = int((grp['outcome'].astype(str).str.lower() == 'win').sum())
            wp = w / g * 100 if g else 0
            out.append({
                'name': label, 'games': g, 'wins': w, 'losses': g - w,
                'record': f'{w}-{g - w}', 'win_pct': format_pct(wp), 'win_pct_num': round(wp),
                'heat': 'heat-excellent' if wp >= 55 else ('heat-poor' if wp < 45 else ''),
            })
        out.sort(key=lambda x: (x['games'], x['win_pct_num']), reverse=True)
        return out

    map_summary = _breakdown_by(_first_col(match_view, 'map'), normalize_map_name)
    mode_summary = _breakdown_by(_first_col(match_view, 'game_type'), clean_mode)

    # ── Opponents who wrecked us this session (recurring threats) ──
    # Pull the full lobby for the session matches and find enemies who beat the
    # squad in multiple games and/or posted a high KDA against us.
    opponent_threats = []
    try:
        smids = [str(m) for m in match_view['match_id'].dropna().unique()]
        if smids:
            sql = text("""
                SELECT mp.gamertag,
                       COUNT(*) AS games,
                       SUM(CASE WHEN mp.outcome = 'win' THEN 1 ELSE 0 END) AS beat_us,
                       AVG(mp.kda) AS avg_kda,
                       SUM(mp.kills) AS kills, SUM(mp.deaths) AS deaths, SUM(mp.assists) AS assists
                FROM halo_match_players mp
                WHERE mp.is_tracked = FALSE AND mp.match_id IN :ids
                GROUP BY mp.gamertag
            """).bindparams(bindparam('ids', expanding=True))
            with ENGINE.connect() as conn:
                rows = [dict(r._mapping) for r in conn.execute(sql, {'ids': smids})]
            for r in rows:
                games = int(r['games'] or 0)
                beat_us = int(r['beat_us'] or 0)
                avg_kda = float(r['avg_kda'] or 0.0)
                # "Wrecked us" = faced 2+ times AND beat the squad in 2+ of them.
                if games >= 2 and beat_us >= 2:
                    opponent_threats.append({
                        'gamertag': r['gamertag'] or 'Unknown',
                        'games': games, 'beat_us': beat_us,
                        'squad_record': f'{games - beat_us}-{beat_us}',
                        'avg_kda': format_float(avg_kda, 1), 'avg_kda_num': round(avg_kda, 1),
                        'kills': int(r['kills'] or 0), 'deaths': int(r['deaths'] or 0),
                        'assists': int(r['assists'] or 0),
                    })
            opponent_threats.sort(key=lambda o: (o['beat_us'], o['avg_kda_num']), reverse=True)
            opponent_threats = opponent_threats[:6]
    except Exception as exc:
        logger.warning('session opponent threats failed: %s', exc)

    # ── Game-by-game rundown (every match in the session, chronological) ──
    session_games = []
    ordered_view = match_view.sort_values('date', ascending=True) if 'date' in match_view.columns else match_view
    mapcol2 = _first_col(session_df, 'map')
    modecol = _first_col(session_df, 'game_type')
    obj_thresholds = _objective_thresholds(rdf_all)
    for gi, (_, mrow) in enumerate(ordered_view.iterrows(), 1):
        mid = mrow.get('match_id')
        g = session_df[session_df['match_id'] == mid]
        if g.empty:
            continue
        outcome = str(mrow.get('outcome', '')).lower()
        ts = pd.to_datetime(mrow.get('date'), utc=True, errors='coerce')
        time_str = ''
        if pd.notna(ts):
            try:
                ts = ts.tz_convert(APP_TIMEZONE)
            except Exception:
                pass
            time_str = ts.strftime('%I:%M %p').lstrip('0')
        prows = []
        for _, row in g.iterrows():
            kda = safe_kda(row.get('kills', 0), row.get('assists', 0), row.get('deaths', 0))
            grade = _match_grade_for_row(row) or {}
            prows.append({
                'player': row.get('player_gamertag', '?'),
                'css': get_player_class(row.get('player_gamertag', '')),
                'kills': int(safe_float(row.get('kills', 0))),
                'deaths': int(safe_float(row.get('deaths', 0))),
                'assists': int(safe_float(row.get('assists', 0))),
                'kda': format_float(kda, 1), 'kda_num': kda,
                'grade': grade.get('grade', ''), 'grade_class': grade.get('grade_class', ''),
                'objs': _objective_chips(row, obj_thresholds),
            })
        prows.sort(key=lambda x: x['kda_num'], reverse=True)
        if prows:
            prows[0]['mvp'] = True
        session_games.append({
            'num': gi, 'time': time_str, 'match_id': mid,
            'map': normalize_map_name(mrow.get(mapcol2, '')) if mapcol2 else '',
            'mode': clean_mode(mrow.get(modecol, '')) if modecol else '',
            'outcome': outcome,
            'result': 'Win' if outcome == 'win' else ('Loss' if outcome == 'loss' else '—'),
            'result_class': 'outcome-win' if outcome == 'win' else ('outcome-loss' if outcome == 'loss' else ''),
            'players': prows,
        })

    # ── "Crazy good" outlier games vs each player's own ranked average ──
    # Rich per-game detail. Each session game is scored by how far above the
    # player's OWN ranked career KDA it sits (z-score); we surface the best
    # ones with full combat lines so you can relive the standout games.
    def _game_detail(row, mean, std, kda):
        z = (kda - mean) / std if std > 0 else 0.0
        k = int(safe_float(row.get('kills', 0)))
        d = int(safe_float(row.get('deaths', 0)))
        a = int(safe_float(row.get('assists', 0)))
        fired = safe_float(row.get('shots_fired', 0))
        hit = safe_float(row.get('shots_hit', 0))
        acc = hit / fired * 100 if fired > 0 else safe_float(row.get('accuracy', 0))
        grade = _match_grade_for_row(row) or {}
        outcome = str(row.get('outcome', '')).lower()
        # "% above their average" is the intuitive headline; tier + sort by it so
        # the biggest games read as the hottest. Fall back to σ language when the
        # career mean is tiny (additive KDA near 0 makes a % unstable/misleading).
        if mean >= 1.0:
            pct_above = (kda - mean) / mean * 100
            tag = f"+{pct_above:.0f}% vs their avg"
            rank_score = pct_above
            if pct_above >= 120:
                tag_kind = 'fire'
            elif pct_above >= 45:
                tag_kind = 'good'
            else:
                tag_kind = 'ok'
        else:
            rank_score = z * 50
            if z >= 1.5:
                tag, tag_kind = f"{z:.1f}σ over their avg", 'fire'
            elif z >= 0.8:
                tag, tag_kind = "well above their avg", 'good'
            else:
                tag, tag_kind = "above their average", 'ok'
        # standout combat extras for this game (only show non-zero); singularise.
        extras = []
        for col, emoji, lbl in [
            ('medal_snipe', '🎯', 'snipes'), ('power_weapon_kills', '💥', 'pwr'),
            ('headshot_kills', '💀', 'HS'), ('max_killing_spree', '🔥', 'spree'),
            ('grenade_kills', '🧨', 'nades'), ('callout_assists', '📢', 'calls'),
        ]:
            if col in row.index:
                val = int(safe_float(row.get(col, 0)))
                if val > 0:
                    show = lbl[:-1] if val == 1 and lbl.endswith('s') else lbl
                    extras.append({'emoji': emoji, 'val': val, 'lbl': show})
        # best medal of this game
        top_medal = None
        best = 0.0
        for c in row.index:
            if c.startswith('medal_') and not c.startswith('medal_id') and c != 'medal_count':
                tot = safe_float(row.get(c, 0))
                if tot > best:
                    best, top_medal = tot, (_medal_label(c), int(tot))
        return {
            'player': row.get('player_gamertag', '?'),
            'css': get_player_class(row.get('player_gamertag', '')),
            'kda': format_float(kda, 1), 'avg': format_float(mean, 1),
            'kills': k, 'deaths': d, 'assists': a,
            'accuracy': format_pct(acc),
            'result': 'Win' if outcome == 'win' else ('Loss' if outcome == 'loss' else '—'),
            'result_class': 'outcome-win' if outcome == 'win' else ('outcome-loss' if outcome == 'loss' else ''),
            'map': _map_of(row),
            'z': z, 'rank_score': rank_score, 'tag': tag, 'tag_kind': tag_kind,
            'extras': extras,
            'top_medal': f"{top_medal[0]} ×{top_medal[1]}" if top_medal else '',
            'match_id': row.get('match_id', ''),
            'grade': grade.get('grade', ''), 'grade_class': grade.get('grade_class', ''),
        }

    scored = []
    best_overall = None  # fallback: best raw KDA game of the session
    for player in players:
        career = pd.to_numeric(
            rdf_all[rdf_all['player_gamertag'] == player].apply(
                lambda r: safe_kda(r.get('kills', 0), r.get('assists', 0), r.get('deaths', 0)),
                axis=1), errors='coerce').dropna()
        if len(career) < 5:
            continue
        mean, std = float(career.mean()), float(career.std() or 0)
        if std <= 0:
            continue
        p_sess = session_df[session_df['player_gamertag'] == player]
        for _, row in p_sess.iterrows():
            kda = safe_kda(row.get('kills', 0), row.get('assists', 0), row.get('deaths', 0))
            detail = _game_detail(row, mean, std, kda)
            if best_overall is None or kda > best_overall[0]:
                best_overall = (kda, detail)
            if kda > mean:  # a genuinely above-average game for them
                scored.append(detail)
    scored.sort(key=lambda c: c['rank_score'], reverse=True)
    crazy = scored[:6]
    # Always show at least the session's single best game if nothing qualified.
    if not crazy and best_overall is not None:
        crazy = [best_overall[1]]

    return {
        'has_data': bool(superlatives or medal_haul or crazy or objective_standouts),
        'session_date': date_str, 'game_count': game_count,
        'kind': session_kind,
        'player_count': len(players),
        'superlatives': superlatives, 'objective_standouts': objective_standouts,
        'medal_haul': medal_haul,
        'medal_haul_by_player': medal_haul_by_player, 'crazy_games': crazy,
        'map_summary': map_summary, 'mode_summary': mode_summary,
        'opponent_threats': opponent_threats, 'session_games': session_games,
    }


@app.route('/')
def index():
    # When the squad is streaming, send fresh visitors straight to the live board.
    # Only on a bare "/" hit — any query arg (?stay=1, ?sid=, filters, the "🏠
    # Dashboard" nav link) opts out. Toggle with HALO_LIVE_REDIRECT (default on);
    # relies on the same Twitch signal as the Live Now banner ([[HALO_LIVE_ON_STREAM]]).
    if (not request.args
            and os.getenv('HALO_LIVE_REDIRECT', 'true').strip().lower() not in ('0', 'false', 'no', 'off')
            and _live_streaming_gamertags()):
        return redirect(url_for('live'))

    df = cache.get()

    player = request.args.get('player', 'all')
    playlist = request.args.get('playlist', 'all')
    mode = request.args.get('mode', 'all')
    filtered = apply_filters(df, player, playlist, mode)

    # Heavy computations — cached until data changes
    base = _build_index_base_payload(df)

    # Session picker on the dashboard: pick any session (or step with the arrows)
    # to populate the report card with that night instead of the latest.
    session_list = base.get('session_list', [])
    sel_sid = request.args.get('sid', '')
    squad_card = base['squad_report_card']
    if sel_sid:
        _sc = get_cached_page_payload('dash_session_card',
                                      lambda: build_squad_report_card(df, mode='squad', anchor_match_id=sel_sid),
                                      key=sel_sid)
        if _sc and _sc.get('rows'):
            squad_card = _sc
    _sids = [s['sid'] for s in session_list]
    _cur = _sids.index(sel_sid) if sel_sid in _sids else 0
    prev_sid = _sids[_cur + 1] if 0 <= _cur + 1 < len(_sids) else ''
    next_sid = _sids[_cur - 1] if _cur - 1 >= 0 else ''
    cur_session = session_list[_cur] if 0 <= _cur < len(session_list) else None

    recent_solo_strip = base.get('recent_solo_strip') or []

    # Lightweight per-request work
    map_rows = build_breakdown(filtered, 'map')
    playlist_rows = build_breakdown(filtered, 'playlist')
    cards = build_cards(filtered)

    # Outlier spotlight (only expensive on non-default range)
    outlier_range = request.args.get('outliers', 'all')
    outlier_rows = build_outlier_spotlight(df, outlier_range)
    outlier_ranges = [
        {'key': '30', 'label': '30D', 'active': outlier_range == '30'},
        {'key': '90', 'label': '90D', 'active': outlier_range == '90'},
        {'key': '365', 'label': '1Y', 'active': outlier_range == '365'},
        {'key': 'all', 'label': 'Lifetime', 'active': outlier_range == 'all'}
    ]

    status = load_status()
    last_update = status.get('last_update')

    try:
        streak_strip = get_cached_page_payload('streak_strip', lambda: build_streak_strip(df))
    except Exception as exc:
        logger.warning('streak strip failed: %s', exc)
        streak_strip = {'squad': None, 'players': []}

    # Live-now banner — time-sensitive, computed per request (cheap, uncached).
    try:
        live_now = build_live_now(df)
    except Exception as exc:
        logger.warning('live now failed: %s', exc)
        live_now = {'live': False}

    try:
        session_mvp = get_cached_page_payload('session_mvp', lambda: build_session_mvp(df))
    except Exception as exc:
        logger.warning('session mvp failed: %s', exc)
        session_mvp = None

    try:
        headlines = get_cached_page_payload('headlines', lambda: build_headlines(df))
    except Exception as exc:
        logger.warning('headlines failed: %s', exc)
        headlines = []

    sb_range = request.args.get('sb', 'lifetime')      # default: ALL games
    if sb_range not in SCOREBOARD_RANGES:
        sb_range = 'lifetime'
    sb_stack = request.args.get('sbs', 'all')           # default: any stack size
    if sb_stack not in SCOREBOARD_STACKS:
        sb_stack = 'all'
    try:
        combat_report = get_cached_page_payload(
            f'combat_report_{sb_range}_{sb_stack}',
            lambda: build_combat_report(_stack_slice(_scoreboard_slice(medal_df(), sb_range), sb_stack)))
    except Exception as exc:
        logger.warning('combat report failed: %s', exc)
        combat_report = {'scoreboard': [], 'leaders': [], 'has_data': False}

    try:
        session_highlights = get_cached_page_payload('session_highlights', lambda: build_session_highlights(medal_df()))
    except Exception as exc:
        logger.warning('session highlights failed: %s', exc)
        session_highlights = {'has_data': False, 'superlatives': [], 'medal_haul': [], 'crazy_games': []}

    return render_template('index.html',
                          app_title=APP_TITLE,
                          live_now=live_now,
                          streak_strip=streak_strip,
                          headlines=headlines,
                          combat_report=combat_report,
                          session_highlights=session_highlights,
                          session_mvp=session_mvp,
                          squad_report_card=squad_card,
                          recent_solo_strip=recent_solo_strip,
                          session_list=session_list,
                          selected_sid=sel_sid,
                          prev_sid=prev_sid,
                          next_sid=next_sid,
                          cur_session=cur_session,
                          sb_range=sb_range,
                          sb_range_order=['lifetime', '30', '60', '90', '180', '1y', '2y'],
                          sb_labels=SCOREBOARD_LABELS,
                          sb_stack=sb_stack,
                          sb_stack_order=['all', '4', '3', '2', 'solo'],
                          sb_stack_labels=SCOREBOARD_STACKS,
                          grade_timeline=base.get('grade_timeline', {}),
                          squad_skill_perf=base.get('squad_skill_perf', []),
                          csr_overview_rows=base['csr_overview_rows'],
                          csr_overview_trends=base['csr_overview_trends'],
                          player_analysis_rows=base['player_analysis_rows'],
                          summary_rows=base['summary_rows'],
                          ranked_arena_rows=base['ranked_arena_rows'],
                          ranked_arena_30day_rows=base['ranked_arena_30day_rows'],
                          ranked_arena_90day_rows=base['ranked_arena_90day_rows'],
                          ranked_arena_180day_rows=base['ranked_arena_180day_rows'],
                          ranked_arena_1y_rows=base['ranked_arena_1y_rows'],
                          ranked_arena_2y_rows=base['ranked_arena_2y_rows'],
                          ranked_arena_lifetime_rows=base['ranked_arena_lifetime_rows'],
                          players=base['players_list'],
                          map_rows=map_rows,
                          playlist_rows=playlist_rows,
                          cards=cards,
                          outlier_rows=outlier_rows,
                          outlier_ranges=outlier_ranges,
                          outlier_range=outlier_range,
                          last_update=last_update,
                          playlists=base['playlists'],
                          modes=base['modes'],
                          selected_player=player,
                          selected_playlist=playlist,
                          selected_mode=mode,
                          db_row_count=count_cache.get())


@app.route('/report')
def report_browser():
    """Session browser: view ANY recent session in the summary/report-card style.
    Renders index.html in single-session mode (report card + grade timeline only)."""
    df = cache.get()
    # session_list + grade_timeline are squad-wide (independent of the chosen
    # sid) — cache them so /report isn't a 6s rebuild on every click.
    sessions = get_cached_page_payload('session_list_squad', lambda: build_session_list(df, mode='squad'))
    grade_timeline = get_cached_page_payload('grade_timeline_squad', lambda: build_grade_timeline(df, mode='squad'))
    sid = request.args.get('sid', '')
    if not sid and sessions:
        sid = sessions[0]['sid']  # default to the latest session
    card = build_squad_report_card(df, mode='squad', anchor_match_id=sid) if sid else {}
    selected = next((s for s in sessions if s['sid'] == sid), None)
    return render_template('index.html',
                           app_title=APP_TITLE,
                           single_session=True,
                           session_list=sessions,
                           selected_sid=sid,
                           selected_session=selected,
                           squad_report_card=card,
                           grade_timeline=grade_timeline,
                           players=unique_sorted(df['player_gamertag']) if not df.empty and 'player_gamertag' in df.columns else [],
                           db_row_count=count_cache.get())


# ── Tabbed hubs ────────────────────────────────────────────────────────────
# Consolidate several related routes into single tabbed pages (hub.html renders
# the existing pages chrome-less via ?embed=1). The underlying routes still work
# as direct/deep links; the hubs are just a cleaner front door for the nav.
_HUBS = {
    'combat-hub': {
        'title': '⚔️ Combat', 'sub': 'Scoreboard, gunplay and objectives in one place.',
        'tabs': [
            {'key': 'scoreboard', 'label': 'Scoreboard', 'url': '/combat'},
            {'key': 'gunplay', 'label': 'Gunplay', 'url': '/weapons'},
            {'key': 'objectives', 'label': 'Objectives', 'url': '/advanced'},
        ],
    },
    'winning-hub': {
        'title': '🏆 Winning', 'sub': 'What our wins look like — and when they happen.',
        'tabs': [
            {'key': 'formula', 'label': 'Winning Formula', 'url': '/winning'},
            {'key': 'when', 'label': 'When You Win', 'url': '/heatmap'},
        ],
    },
    'analysis': {
        'title': '📈 Analysis', 'sub': 'Trends, insights, CSR climb and the full game log.',
        'tabs': [
            {'key': 'trends', 'label': 'Trends', 'url': '/trends'},
            {'key': 'insights', 'label': 'Insights', 'url': '/insights'},
            {'key': 'climb', 'label': 'CSR Climb', 'url': '/climb'},
            {'key': 'all-games', 'label': 'All Games', 'url': '/lifetime'},
        ],
    },
    'records': {
        'title': '🏅 Records', 'sub': 'Hall of fame, leaderboards, highlights and recaps.',
        'tabs': [
            {'key': 'hall', 'label': 'Hall of Fame', 'url': '/hall'},
            {'key': 'leaderboard', 'label': 'Leaderboard', 'url': '/leaderboard'},
            {'key': 'highlights', 'label': 'Highlights', 'url': '/highlights'},
            {'key': 'medals', 'label': 'Medals', 'url': '/medals'},
            {'key': 'recap', 'label': 'Weekly Recap', 'url': '/recap'},
            {'key': 'snapshots', 'label': 'Snapshots', 'url': '/snapshots'},
        ],
    },
}


@app.route('/combat-hub')
@app.route('/winning-hub')
@app.route('/analysis')
@app.route('/records')
def hub_page():
    key = request.path.strip('/')
    hub = _HUBS.get(key)
    if not hub:
        return redirect(url_for('index'))
    status = load_status()
    return render_template('hub.html', app_title=APP_TITLE,
                           hub_key=key, hub_title=hub['title'], hub_sub=hub.get('sub'),
                           hub_tabs=hub['tabs'],
                           last_update=status.get('last_update'),
                           db_row_count=count_cache.get())


@app.route('/combat')
def combat():
    """Full squad combat & medals page — the rich weapon/medal data summarised."""
    df = cache.get()
    try:
        report = get_cached_page_payload('combat_report', lambda: build_combat_report(medal_df()))
    except Exception as exc:
        logger.warning('combat page failed: %s', exc)
        report = {'scoreboard': [], 'leaders': [], 'has_data': False}
    return render_template('combat.html',
                          app_title=APP_TITLE,
                          combat_report=report,
                          players=unique_sorted(df['player_gamertag']) if 'player_gamertag' in df.columns else [],
                          last_update=load_status().get('last_update'),
                          db_row_count=count_cache.get())


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        try:
            match_limit = int(request.form.get('match_limit', LIFETIME_LIMIT_DEFAULT))
        except (TypeError, ValueError):
            match_limit = LIFETIME_LIMIT_DEFAULT
        try:
            update_interval = int(request.form.get('update_interval', 60))
        except (TypeError, ValueError):
            update_interval = 60
        # Clamp to sane ranges (0 == unlimited for match_limit).
        match_limit = 0 if match_limit <= 0 else min(match_limit, 10000)
        update_interval = max(10, min(update_interval, 86400))
        new_settings = {
            'match_limit': match_limit,
            'update_interval': update_interval,
            'force_refresh': request.form.get('force_refresh') == 'on'
        }
        save_settings(new_settings)
        cache.force_reload()
        return redirect(url_for('settings'))
    
    current_settings = load_settings()
    return render_template('settings.html',
                          app_title=APP_TITLE,
                          settings=current_settings,
                          db_row_count=count_cache.get())


@app.route('/suggestions', methods=['GET', 'POST'])
def suggestions():
    df = cache.get()
    status = load_status()
    message = None
    error = None
    
    if request.method == 'POST':
        if request.form.get('action') == 'delete':
            try:
                delete_suggestion(ENGINE, int(request.form.get('suggestion_id', 0)))
                message = 'Suggestion deleted.'
            except (ValueError, SQLAlchemyError) as exc:
                error = f'Could not delete suggestion: {exc}'
            suggestions_rows = fetch_suggestions(ENGINE, limit=50)
            return render_template('suggestions.html',
                                  app_title=APP_TITLE,
                                  players=unique_sorted(df['player_gamertag']) if not df.empty else [],
                                  suggestions=suggestions_rows,
                                  message=message,
                                  error=error,
                                  last_update=status.get('last_update'),
                                  db_row_count=count_cache.get())
        name = request.form.get('name', '').strip()
        gamertag = request.form.get('gamertag', '').strip()
        contact = request.form.get('contact', '').strip()
        summary = request.form.get('summary', '').strip()
        details = request.form.get('details', '').strip()
        follow_up = request.form.get('follow_up', '').strip()
        
        if not summary:
            error = 'Please add a short summary.'
        else:
            payload = {
                'name': name or None,
                'gamertag': gamertag or None,
                'contact': contact or None,
                'summary': summary,
                'details': details or None,
                'follow_up': follow_up or None
            }
            save_suggestion(ENGINE, payload)
            message = 'Thanks! Your suggestion has been saved.'
    
    suggestions_rows = fetch_suggestions(ENGINE, limit=50)
    
    return render_template('suggestions.html',
                          app_title=APP_TITLE,
                          players=unique_sorted(df['player_gamertag']) if not df.empty else [],
                          suggestions=suggestions_rows,
                          message=message,
                          error=error,
                          last_update=status.get('last_update'),
                          db_row_count=count_cache.get())


@app.route('/solo')
def solo_dashboard():
    """Solo dashboard: solo session report card (browsable via ?sid=, like the
    squad dash), every player's most recent solo grind, and the solo-mode grade
    timeline — the lone-queue mirror of the squad dashboard."""
    df = cache.get()
    payload = get_cached_page_payload('solo_page', lambda: {
        'solo_card': build_squad_report_card(df, mode='solo'),
        # Top-of-page Full Stat Table: every tracked player's latest solo
        # session, one row each (rows carry their own session date/sid).
        'solo_all_card': build_solo_all_table(df),
        'solo_player_cards': build_player_solo_cards(df),
        'grade_timeline': build_grade_timeline(df, mode='solo'),
        'session_list': build_session_list(df, mode='solo'),
    }) or {}

    # Session browser (same pattern as index): pick any SOLO session to anchor
    # the report card on that night instead of the latest.
    session_list = payload.get('session_list', [])
    sel_sid = request.args.get('sid', '')
    solo_card = payload.get('solo_card') or {}
    if sel_sid:
        _sc = get_cached_page_payload(
            'solo_session_card',
            lambda: build_squad_report_card(df, mode='solo', anchor_match_id=sel_sid),
            key=sel_sid)
        if _sc and _sc.get('rows'):
            solo_card = _sc
    _sids = [s['sid'] for s in session_list]
    _cur = _sids.index(sel_sid) if sel_sid in _sids else 0
    prev_sid = _sids[_cur + 1] if 0 <= _cur + 1 < len(_sids) else ''
    next_sid = _sids[_cur - 1] if _cur - 1 >= 0 else ''
    cur_session = session_list[_cur] if 0 <= _cur < len(session_list) else None

    return render_template('solo.html',
                           app_title=APP_TITLE,
                           solo_card=solo_card,
                           solo_all_card=payload.get('solo_all_card') or {},
                           solo_player_cards=payload.get('solo_player_cards', []),
                           grade_timeline=payload.get('grade_timeline', {}),
                           session_list=session_list,
                           selected_sid=sel_sid,
                           prev_sid=prev_sid,
                           next_sid=next_sid,
                           cur_session=cur_session,
                           db_row_count=count_cache.get())


@app.route('/lifetime')
def lifetime():
    """Lifetime stats page."""
    df = cache.get()
    player = request.args.get('player', 'all')
    playlist = request.args.get('playlist', 'all')
    mode = request.args.get('mode', 'all')
    limit_key = request.args.get('limit', str(LIFETIME_LIMIT_DEFAULT))
    
    filtered = apply_filters(df, player, playlist, mode)
    
    lifetime_rows = build_lifetime_stats(filtered)
    limit = None
    limit_note = None
    if limit_key and limit_key != 'all':
        try:
            limit = int(limit_key)
        except ValueError:
            limit = None
        if limit is not None and limit <= 0:
            limit = None
        if limit is not None and limit > LIFETIME_LIMIT_MAX:
            limit = LIFETIME_LIMIT_MAX
            limit_note = f'Showing latest {LIFETIME_LIMIT_MAX:,} matches for performance.'
    else:
        limit = LIFETIME_LIMIT_MAX
        limit_note = f'Showing latest {LIFETIME_LIMIT_MAX:,} matches for performance.'
    session_rows = build_session_history(filtered, limit=limit)
    
    status = load_status()
    
    return render_template('lifetime.html',
                          app_title=APP_TITLE,
                          players=unique_sorted(df['player_gamertag']) if not df.empty else [],
                          lifetime_rows=lifetime_rows,
                          session_rows=session_rows,
                          last_update=status.get('last_update'),
                          db_row_count=count_cache.get(),
                          playlists=unique_sorted(df['playlist']) if not df.empty else [],
                          modes=unique_sorted(df['game_type']) if not df.empty else [],
                          selected_player=player,
                          selected_playlist=playlist,
                          selected_mode=mode,
                          selected_limit=limit_key,
                          limit_note=limit_note)





@app.route('/advanced')
def advanced():
    """Advanced/objective stats page."""
    df = cache.get()
    
    objective_session_rows = build_objective_stats(df, 'session')
    objective_30day_rows = build_objective_stats(df, '30day')
    objective_lifetime_rows = build_objective_stats(df, 'all')
    
    status = load_status()
    
    return render_template('advanced.html',
                          app_title=APP_TITLE,
                          players=unique_sorted(df['player_gamertag']) if not df.empty else [],
                          objective_session_rows=objective_session_rows,
                          objective_30day_rows=objective_30day_rows,
                          objective_lifetime_rows=objective_lifetime_rows,
                          last_update=status.get('last_update'),
                          db_row_count=count_cache.get())


@app.route('/medals')
def medals():
    """Medal statistics page."""
    df = cache.get()

    # build_medal_stats iterates the per-medal columns generically, so it needs
    # the medal-inclusive dataframe (same rows as the lean cache). Cached (SWR)
    # — this build walks ~150 medal columns and took ~20s per new game.
    _mp = get_cached_page_payload('medals', lambda: dict(zip(
        ('players', 'ranked_rows', 'total_rows'), build_medal_stats(medal_df()))))
    medal_players, ranked_medal_rows, total_medal_rows = (
        _mp['players'], _mp['ranked_rows'], _mp['total_rows'])
    
    status = load_status()
    
    return render_template('medals.html',
                          app_title=APP_TITLE,
                          players=unique_sorted(df['player_gamertag']) if not df.empty else [],
                          ranked_arena_medal_players=medal_players,
                          ranked_arena_medal_rows=ranked_medal_rows,
                          medal_players=medal_players,
                          medal_rows=total_medal_rows,
                          last_update=status.get('last_update'),
                          db_row_count=count_cache.get())


@app.route('/debug/medals')
def debug_medals():
    """Debug endpoint to see raw medal data."""
    from flask import jsonify
    df = medal_df()  # needs the per-medal columns
    
    # Get medal columns
    medal_cols = [col for col in df.columns if col.startswith('medal_') and col != 'medal_count']
    
    # Get data for each player
    result = {}
    for player in unique_sorted(df['player_gamertag']):
        player_df = df[df['player_gamertag'] == player]
        games = len(player_df)
        
        player_medals = {}
        for col in medal_cols[:20]:  # First 20 medal types
            total = safe_col_sum(player_df, col)
            per_game = total / games if games > 0 else 0
            if total > 0:  # Only show medals they actually have
                player_medals[col] = {
                    'total': float(total),
                    'per_game': round(per_game, 3),
                    'games': games
                }
        
        result[player] = player_medals
    
    return jsonify(result)


@app.route('/highlights')
def highlights():
    """Highlight games page."""
    df = cache.get()
    
    highlight_rows = build_highlight_games(df, limit=50)
    
    status = load_status()
    
    return render_template('highlights.html',
                          app_title=APP_TITLE,
                          players=unique_sorted(df['player_gamertag']) if not df.empty else [],
                          highlight_rows=highlight_rows,
                          last_update=status.get('last_update'),
                          db_row_count=count_cache.get())


@app.route('/columns')
def columns():
    """Show available columns/keys."""
    df = cache.get()
    status = load_status()
    
    wide_columns = []
    kv_keys = []
    
    try:
        if inspect(ENGINE).has_table('halo_match_stats'):
            wide_columns = [c.get('name') for c in inspect(ENGINE).get_columns('halo_match_stats') if c.get('name')]
    except Exception:
        pass
    
    return render_template('columns.html',
                          app_title=APP_TITLE,
                          players=unique_sorted(df['player_gamertag']) if not df.empty else [],
                          last_update=status.get('last_update'),
                          wide_columns=wide_columns,
                          kv_keys=kv_keys,
                          wide_count=len(wide_columns),
                          kv_count=len(kv_keys),
                          db_row_count=count_cache.get())


def build_skill_performance(df: pd.DataFrame) -> dict:
    """Expected-vs-actual performance vs the player's own skill bracket, from the
    Halo matchmaking model (team MMR + expected kills/deaths). Aggregates over the
    matches that have the skill data captured. Returns {} if none yet."""
    if df is None or df.empty or 'expected_kills' not in df.columns:
        return {}
    ek = pd.to_numeric(df.get('expected_kills'), errors='coerce')
    ed = pd.to_numeric(df.get('expected_deaths'), errors='coerce')
    ak = pd.to_numeric(df.get('kills'), errors='coerce')
    ad = pd.to_numeric(df.get('deaths'), errors='coerce')
    mask = ek.notna() & ed.notna() & ak.notna() & ad.notna()
    n = int(mask.sum())
    if n < 1:
        return {}
    ak_m, ek_m = float(ak[mask].mean()), float(ek[mask].mean())
    ad_m, ed_m = float(ad[mask].mean()), float(ed[mask].mean())
    kills_delta = ak_m - ek_m       # + → more kills than your bracket averages (good)
    deaths_delta = ad_m - ed_m      # − → fewer deaths than expected (good)
    tm = pd.to_numeric(df.get('team_mmr'), errors='coerce')
    em = pd.to_numeric(df.get('enemy_team_mmr'), errors='coerce')
    team_mmr = float(tm[tm.notna()].mean()) if tm.notna().any() else None
    enemy_mmr = float(em[em.notna()].mean()) if em.notna().any() else None
    return {
        'games': n,
        'actual_kills': round(ak_m, 1), 'expected_kills': round(ek_m, 1),
        'kills_delta': round(kills_delta, 1),
        'actual_deaths': round(ad_m, 1), 'expected_deaths': round(ed_m, 1),
        'deaths_delta': round(deaths_delta, 1),
        'perf_index': round(kills_delta - deaths_delta, 1),  # net overperformance / game
        'beat_kills_pct': round(int((ak[mask] > ek[mask]).sum()) / n * 100),
        'team_mmr': round(team_mmr) if team_mmr is not None else None,
        'enemy_mmr': round(enemy_mmr) if enemy_mmr is not None else None,
        'mmr_gap': (round(enemy_mmr - team_mmr) if (team_mmr is not None and enemy_mmr is not None) else None),
    }


def build_squad_skill_performance(df: pd.DataFrame) -> list[dict]:
    """Per-player expected-vs-actual overperformance, ranked best→worst — for the
    dashboard squad comparison. One row per tracked player that has skill data."""
    if df is None or df.empty or 'player_gamertag' not in df.columns or 'expected_kills' not in df.columns:
        return []
    rows = []
    for p in unique_sorted(df['player_gamertag']):
        sp = build_skill_performance(df[df['player_gamertag'] == p])
        if not sp or not sp.get('games'):
            continue
        rows.append({'player': p, 'css': get_player_class(p), **sp})
    rows.sort(key=lambda r: r.get('perf_index', 0), reverse=True)
    return rows


@app.route('/player/<player_name>')
def player_profile(player_name: str):
    """Individual player profile page."""
    df = cache.get()
    
    if df.empty:
        return render_template('player.html',
                              app_title=APP_TITLE,
                              player_name=player_name,
                              players=[],
                              current_csr='-',
                              max_csr='-',
                              current_streak=0,
                              last_session={},
                              avg_30day={},
                              comparison={},
                              match_history=[],
                              map_stats=[],
                              teammate_stats=[],
                              player_win_corr=[],
                              csr_history=[],
                              error='No data available',
                              last_update=load_status().get('last_update'),
                              db_row_count=count_cache.get())
    
    all_players = unique_sorted(df['player_gamertag']) if not df.empty else []
    resolved_name = resolve_player_name(player_name, all_players)
    
    if not resolved_name:
        return render_template('player.html',
                              app_title=APP_TITLE,
                              player_name=player_name,
                              players=all_players,
                              current_csr='-',
                              max_csr='-',
                              current_streak=0,
                              last_session={},
                              avg_30day={},
                              comparison={},
                              match_history=[],
                              map_stats=[],
                              teammate_stats=[],
                              player_win_corr=[],
                              csr_history=[],
                              error='Player not found',
                              last_update=load_status().get('last_update'),
                              db_row_count=count_cache.get())
    player_name = resolved_name
    
    presence = load_presence()
    status = load_status()

    def build_player_payload():
        csr_overview = build_csr_overview(df)
        player_csr = next((r for r in csr_overview if r['player'] == player_name), {})
        ranked_sessions = build_ranked_arena_summary(df)
        last_session = next((r for r in ranked_sessions if r['player'] == player_name), {})
        player_df = df[df['player_gamertag'] == player_name] if not df.empty and 'player_gamertag' in df.columns else pd.DataFrame()
        ranked_df = _ranked_only(df)

        avg_30day_rows = build_30day_overview(ranked_df)
        avg_30day = avg_30day_rows.get(player_name, {})
        if not avg_30day:
            avg_30day = {
                'games': '0',
                'win_pct': '0',
                'kda': '0',
                'accuracy': '0',
                'avg_csr_change': '0',
                'win_pct_heat': '',
                'kda_heat': '',
                'accuracy_heat': ''
            }

        comparison = build_30day_comparison(ranked_df, player_name)
        if not comparison:
            comparison = {
                'trend': 'stable',
                'win_pct_diff': '-',
                'kda_diff': '-',
                'win_pct_class': '',
                'kda_class': ''
            }

        report_card_data = build_player_report_card(_ranked_only(player_df))
        return {
            'player_csr': player_csr,
            'last_session': last_session,
            'avg_30day': avg_30day,
            'comparison': comparison,
            'match_history': build_player_match_history(player_df, limit=20),
            'map_stats': build_player_map_summary(player_df),
            'teammate_stats': build_teammate_stats(ranked_df, player_name),
            'csr_history': build_player_csr_history(df, player_name),
            'current_streak': compute_current_streak(player_df),
            'player_win_corr': build_win_corr(player_df),
            'grade_trend': build_player_grade_trend(player_df),
            'report_card': report_card_data,
            'objective': (build_objective_stats(player_df, 'all') or [{}])[0],
            'gunplay': (build_weapon_rows(player_df) or [{}])[0],
            'form_heatmap': build_time_heatmap(player_df),
            'scouting': build_player_scouting_extras(df, player_name),
            'achievements': build_player_achievements(player_df),
            'medal_fingerprint': build_player_medal_fingerprint(
                (lambda m: m[m['player_gamertag'] == player_name]
                 if not m.empty and 'player_gamertag' in m.columns else m)(medal_df())
            ),
            'coach_tip': build_player_coach_tip(report_card_data),
            'skill_perf': build_skill_performance(player_df),
        }

    player_payload = get_cached_page_payload('player', build_player_payload, key=player_name)
    player_csr = player_payload['player_csr']
    
    return render_template('player.html',
                          app_title=APP_TITLE,
                          player_name=player_name,
                          players=all_players,
                          is_online=is_player_online(presence, player_name),
                          current_csr=player_csr.get('current_csr', '-'),
                          max_csr=player_csr.get('max_csr', '-'),
                          current_streak=player_payload['current_streak'],
                          last_session=player_payload['last_session'],
                          avg_30day=player_payload['avg_30day'],
                          comparison=player_payload['comparison'],
                          match_history=player_payload['match_history'],
                          map_stats=player_payload['map_stats'],
                          teammate_stats=player_payload['teammate_stats'],
                          player_win_corr=player_payload['player_win_corr'],
                          csr_history=player_payload['csr_history'],
                          grade_trend=player_payload.get('grade_trend'),
                          report_card=player_payload.get('report_card'),
                          objective=player_payload.get('objective'),
                          gunplay=player_payload.get('gunplay'),
                          form_heatmap=player_payload.get('form_heatmap'),
                          scouting=player_payload.get('scouting'),
                          achievements=player_payload.get('achievements'),
                          medal_fingerprint=player_payload.get('medal_fingerprint'),
                          coach_tip=player_payload.get('coach_tip'),
                          skill_perf=player_payload.get('skill_perf', {}),
                          last_update=status.get('last_update'),
                          db_row_count=count_cache.get())


@app.route('/weapons')
def weapons():
    """Weapon statistics page."""
    df = cache.get()
    payload = get_cached_page_payload('weapons', lambda: {
        'weapon_rows': build_weapon_rows(df),
        'accuracy_trend': build_weapon_accuracy_trend(df),
        'players': unique_sorted(df['player_gamertag']) if not df.empty else [],
    })
    
    status = load_status()
    
    return render_template('weapons.html',
                          app_title=APP_TITLE,
                          players=payload['players'],
                          weapon_rows=payload['weapon_rows'],
                          accuracy_trend=payload['accuracy_trend'],
                          last_update=status.get('last_update'),
                          db_row_count=count_cache.get())


@app.route('/hall')
def hall():
    """Hall of fame/shame page."""
    df = cache.get()
    payload = get_cached_page_payload('hall', lambda: {
        'players': unique_sorted(df['player_gamertag']) if not df.empty else [],
        'hall_rows': build_hall_fame_shame(df),
    })
    hall_fame_rows, hall_shame_rows = payload['hall_rows']
    status = load_status()
    return render_template('hall.html',
                          app_title=APP_TITLE,
                          players=payload['players'],
                          hall_fame_rows=hall_fame_rows,
                          hall_shame_rows=hall_shame_rows,
                          last_update=status.get('last_update'),
                          db_row_count=count_cache.get())


@app.route('/maps')
def maps():
    """Map statistics page."""
    df = cache.get()
    payload = get_cached_page_payload('maps', lambda: {
        'players': unique_sorted(df['player_gamertag']) if not df.empty else [],
        'map_rows': build_map_stats(df),
        'mode_rows': build_mode_stats(df),
        'player_map_rows': build_player_map_stats(df),
        'player_mode_rows': build_player_mode_stats(df),
    })

    status = load_status()

    return render_template('maps.html',
                          app_title=APP_TITLE,
                          players=payload['players'],
                          map_rows=payload['map_rows'],
                          mode_rows=payload['mode_rows'],
                          player_map_rows=payload['player_map_rows'],
                          player_mode_rows=payload.get('player_mode_rows', []),
                          last_update=status.get('last_update'),
                          db_row_count=count_cache.get())


@app.route('/trends')
def trends():
    """Trend analysis page."""
    df = cache.get()

    range_key = request.args.get('range', '90')
    trend_df = add_trend_metrics(apply_trend_range(normalize_trend_df(df), range_key))
    
    status = load_status()
    
    trend_ranges = [
        {'key': '7', 'label': '7 days', 'active': range_key == '7'},
        {'key': '30', 'label': '30 days', 'active': range_key == '30'},
        {'key': '90', 'label': '90 days', 'active': range_key == '90'},
        {'key': '180', 'label': '180 days', 'active': range_key == '180'},
        {'key': '365', 'label': '1 year', 'active': range_key == '365'},
        {'key': 'all', 'label': 'All time', 'active': range_key == 'all'}
    ]
    csr_trends = build_csr_trends(trend_df)
    win_rate_trends = build_win_rate_trends(trend_df)
    kda_trends = build_trend_data(trend_df, 'kda', 'kda') if 'kda' in trend_df.columns else {}
    obj_score_trends = build_trend_data(trend_df, 'obj_score', 'obj_score') if 'obj_score' in trend_df.columns else {}
    damage_min_trends = build_trend_data(trend_df, 'dmg_min', 'dmg_min') if 'dmg_min' in trend_df.columns else {}
    damage_diff_trends = build_trend_data(trend_df, 'dmg_diff', 'dmg_diff') if 'dmg_diff' in trend_df.columns else {}
    accuracy_trends = build_trend_data(trend_df, 'accuracy', 'accuracy') if 'accuracy' in trend_df.columns else {}
    kills_pg_trends = build_trend_data(trend_df, 'kills_pg', 'kills_pg') if 'kills_pg' in trend_df.columns else {}
    deaths_pg_trends = build_trend_data(trend_df, 'deaths_pg', 'deaths_pg') if 'deaths_pg' in trend_df.columns else {}
    max_spree_trends = build_trend_data(trend_df, 'max_spree', 'max_spree') if 'max_spree' in trend_df.columns else {}
    duration_trends = build_trend_data(trend_df, 'duration_min', 'duration_min') if 'duration_min' in trend_df.columns else {}
    _trend_win_corr = build_win_corr(trend_df)
    trend_grade_groups = {
        'csr': build_trend_grade_rows(csr_trends, 'csr', 'CSR', True, 0),
        'win_rate': build_trend_grade_rows(win_rate_trends, 'win_rate', 'Win rate', True, 1, '%'),
        'kda': build_trend_grade_rows(kda_trends, 'kda', 'KDA', True, 2),
        'obj_score': build_trend_grade_rows(obj_score_trends, 'obj_score', 'Objective score', True, 1),
        'damage_min': build_trend_grade_rows(damage_min_trends, 'dmg_min', 'Damage per minute', True, 0),
        'damage_diff': build_trend_grade_rows(damage_diff_trends, 'dmg_diff', 'Damage difference', True, 0),
        'accuracy': build_trend_grade_rows(accuracy_trends, 'accuracy', 'Accuracy', True, 1, '%'),
        'kills_pg': build_trend_grade_rows(kills_pg_trends, 'kills_pg', 'Kills per game', True, 1),
        'deaths_pg': build_trend_grade_rows(deaths_pg_trends, 'deaths_pg', 'Deaths per game', False, 1),
        'max_spree': build_trend_grade_rows(max_spree_trends, 'max_spree', 'Max killing spree', True, 1),
        'duration': build_trend_grade_rows(duration_trends, 'duration_min', 'Match duration', False, 1, 'm')
    }
    
    return render_template('trends.html',
                          app_title=APP_TITLE,
                          players=unique_sorted(df['player_gamertag']) if not df.empty else [],
                          csr_trends=csr_trends,
                          win_rate_trends=win_rate_trends,
                          kda_trends=kda_trends,
                          obj_score_trends=obj_score_trends,
                          damage_min_trends=damage_min_trends,
                          damage_diff_trends=damage_diff_trends,
                          accuracy_trends=accuracy_trends,
                          kills_pg_trends=kills_pg_trends,
                          deaths_pg_trends=deaths_pg_trends,
                          max_spree_trends=max_spree_trends,
                          duration_trends=duration_trends,
                          trend_grade_groups=trend_grade_groups,
                          activity_heatmap=build_activity_heatmap(trend_df),
                          win_corr_overall=_trend_win_corr,
                          win_corr_by_player=build_win_corr_by_player(trend_df),
                          player_moments=build_player_moments(trend_df),
                          trend_ranges=trend_ranges,
                          last_update=status.get('last_update'),
                          db_row_count=count_cache.get())


@app.route('/insights')
def insights():
    """Insights and comparisons page."""
    df = cache.get()
    
    players_list = unique_sorted(df['player_gamertag']) if not df.empty else []
    player = request.args.get('player', 'all')
    start_a = request.args.get('start_a', '')
    end_a = request.args.get('end_a', '')
    start_b = request.args.get('start_b', '')
    end_b = request.args.get('end_b', '')
    
    status = load_status()
    
    ranked_df = _ranked_only(df)
    
    compare_key = (player, start_a, end_a, start_b, end_b)
    session_compare_rows = get_cached_page_payload(f'insights_compare:{compare_key}', lambda: {
        'rows': build_session_compare(ranked_df, player, start_a, end_a, start_b, end_b)
    }, key=compare_key)['rows']
    insights_payload = get_insights_payload(ranked_df)
    clutch_rows = insights_payload['clutch_rows']
    role_rows = insights_payload['role_rows']
    momentum_rows = insights_payload['momentum_rows']
    veto_rows = insights_payload['veto_rows']
    consistency_rows = insights_payload['consistency_rows']
    notable_rows = insights_payload['notable_rows']
    change_rows = insights_payload['change_rows']
    lineup2_rows = insights_payload['lineup2_rows']
    lineup3_rows = insights_payload['lineup3_rows']
    lineup4_rows = insights_payload['lineup4_rows']
    
    range_a_label = 'Range A'
    range_b_label = 'Range B'
    if start_a or end_a:
        range_a_label = f"{start_a or '...'} to {end_a or '...'}"
    if start_b or end_b:
        range_b_label = f"{start_b or '...'} to {end_b or '...'}"
    
    return render_template('insights.html',
                          app_title=APP_TITLE,
                          players=players_list,
                          selected_player=player,
                          start_a=start_a,
                          end_a=end_a,
                          start_b=start_b,
                          end_b=end_b,
                          range_a_label=range_a_label,
                          range_b_label=range_b_label,
                          session_compare_rows=session_compare_rows,
                          clutch_rows=clutch_rows,
                          role_rows=role_rows,
                          momentum_rows=momentum_rows,
                          veto_rows=veto_rows,
                          consistency_rows=consistency_rows,
                          notable_rows=notable_rows,
                          change_rows=change_rows,
                          lineup2_rows=lineup2_rows,
                          lineup3_rows=lineup3_rows,
                          lineup4_rows=lineup4_rows,
                          last_update=status.get('last_update'),
                          db_row_count=count_cache.get())


@app.route('/leaderboard')
def leaderboard():
    """Leaderboards page."""
    df = cache.get()
    
    period = request.args.get('period', 'all')
    def build_leaderboard_payload():
        leaderboard_df = apply_leaderboard_period(df, period)
        leaders = {
            'csr_leaders': build_leaderboard(leaderboard_df, 'csr'),
            'csr_gained_leaders': build_leaderboard(leaderboard_df, 'csr_gained'),
            'win_rate_leaders': build_leaderboard(leaderboard_df, 'win_rate'),
            'kda_leaders': build_leaderboard(leaderboard_df, 'kda'),
            'accuracy_leaders': build_leaderboard(leaderboard_df, 'accuracy'),
            'streak_leaders': build_leaderboard(leaderboard_df, 'streak'),
            'kills_leaders': build_leaderboard(leaderboard_df, 'kills'),
            'games_leaders': build_leaderboard(leaderboard_df, 'games'),
        }
        # Combat boards need the medal-inclusive columns; degrade gracefully.
        try:
            mdf = apply_leaderboard_period(medal_df(), period)
            leaders.update({
                'snipes_leaders': build_leaderboard(mdf, 'snipes'),
                'headshots_leaders': build_leaderboard(mdf, 'headshots'),
                'power_weapons_leaders': build_leaderboard(mdf, 'power_weapons'),
                'medals_leaders': build_leaderboard(mdf, 'medals'),
            })
        except Exception as exc:
            logger.warning('combat leaderboards failed: %s', exc)
            for k in ('snipes_leaders', 'headshots_leaders', 'power_weapons_leaders', 'medals_leaders'):
                leaders.setdefault(k, [])
        return {
            'players': unique_sorted(df['player_gamertag']) if not df.empty else [],
            'leaders': leaders,
        }

    payload = get_cached_page_payload('leaderboard', build_leaderboard_payload, key=period)
    
    status = load_status()
    
    return render_template('leaderboard.html',
                          app_title=APP_TITLE,
                          players=payload['players'],
                          period=period,
                          **payload['leaders'],
                          last_update=status.get('last_update'),
                          db_row_count=count_cache.get())


@app.route('/api/debug')
def debug_data():
    """Debug endpoint - shows columns and data from both DB and cache."""
    engine = get_engine()
    try:
        # Get 5 rows from DB directly
        query = "SELECT * FROM halo_match_stats LIMIT 5"
        db_df = pd.read_sql_query(query, engine)
        
        # Get data from cache (what webapp uses)
        cache_df = cache.get()
        
        db_columns = list(db_df.columns) if not db_df.empty else []
        cache_columns = list(cache_df.columns) if not cache_df.empty else []
        
        # Find columns missing in cache
        missing_in_cache = [c for c in db_columns if c not in cache_columns]
        extra_in_cache = [c for c in cache_columns if c not in db_columns]
        
        # Sample data from cache
        sample_rows = []
        if not cache_df.empty:
            for idx, row in cache_df.head(3).iterrows():
                row_dict = {}
                for key in ['player_gamertag', 'date', 'kills', 'deaths', 'damage_dealt', 
                           'damage_taken', 'accuracy', 'post_match_csr', 'kda']:
                    if key in row:
                        val = row[key]
                        row_dict[key] = None if pd.isna(val) else val
                sample_rows.append(row_dict)
        
        return {
            "db_columns_count": len(db_columns),
            "cache_columns_count": len(cache_columns),
            "db_rows": len(db_df),
            "cache_rows": len(cache_df),
            "missing_in_cache": missing_in_cache,
            "extra_in_cache": extra_in_cache,
            "cache_columns": cache_columns,
            "sample_cache_data": sample_rows
        }
    except Exception as e:
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc()}


@app.route('/api/site-version')
def api_site_version():
    """Tiny freshness probe for browsers.

    Returns the authoritative DB row count, not the cached count. When the count
    changes, refresh the common dataframe cache before responding so the browser
    can immediately reload or partial-refresh into fresh stats.
    """
    try:
        since_raw = request.args.get('since', '')
        since = int(since_raw) if str(since_raw).strip() else None
    except (TypeError, ValueError):
        since = None
    try:
        current_count = load_db_row_count(ENGINE)
        refresh_match_caches_for_count(current_count)
        status = load_status()
        return jsonify({
            'ok': True,
            'count': int(current_count),
            'cache_count': int(cache.last_count or 0),
            'changed': bool(since is not None and int(current_count) != since),
            'poll_seconds': SITE_VERSION_POLL_SECONDS,
            'last_update': status.get('last_update') if isinstance(status, dict) else None,
        })
    except Exception as exc:
        logger.warning('site-version failed: %s', exc)
        return jsonify({'ok': False, 'error': 'version check failed'}), 500



# --- Twitch chat sign-in (device-code flow relay) --------------------------
# The live page's chat reader is anonymous (read-only, no login). To TALK
# from the page, each browser connects its own Twitch account once via the
# device-code flow. There is no client secret (public client); these routes
# only relay to id.twitch.tv so the browser never fights CORS, and tokens
# live in that browser's localStorage — the server stores nothing.
_TWITCH_ID = 'https://id.twitch.tv/oauth2'


def _twitch_client_id() -> str:
    return os.getenv('HALO_TWITCH_CLIENT_ID', '').strip()


def _twitch_relay(path: str, payload: dict):
    try:
        resp = requests.post(f'{_TWITCH_ID}/{path}', data=payload, timeout=15)
        try:
            body = resp.json()
        except ValueError:
            body = {'message': 'unexpected response'}
        return jsonify(body), resp.status_code
    except requests.RequestException:
        return jsonify({'message': 'twitch unreachable'}), 502


@app.route('/api/twitch/chat-config')
def twitch_chat_config():
    return jsonify({'enabled': bool(_twitch_client_id())})


@app.route('/api/twitch/device-start', methods=['POST'])
def twitch_device_start():
    cid = _twitch_client_id()
    if not cid:
        return jsonify({'message': 'HALO_TWITCH_CLIENT_ID not configured'}), 404
    return _twitch_relay('device', {'client_id': cid, 'scopes': 'chat:read chat:edit'})


@app.route('/api/twitch/device-poll', methods=['POST'])
def twitch_device_poll():
    cid = _twitch_client_id()
    if not cid:
        return jsonify({'message': 'not configured'}), 404
    dc = str((request.get_json(silent=True) or {}).get('device_code') or '')
    return _twitch_relay('token', {'client_id': cid, 'device_code': dc,
                                   'grant_type': 'urn:ietf:params:oauth:grant-type:device_code'})


@app.route('/api/twitch/refresh', methods=['POST'])
def twitch_refresh():
    cid = _twitch_client_id()
    if not cid:
        return jsonify({'message': 'not configured'}), 404
    rt = str((request.get_json(silent=True) or {}).get('refresh_token') or '')
    return _twitch_relay('token', {'client_id': cid, 'refresh_token': rt,
                                   'grant_type': 'refresh_token'})


@app.route('/api/twitch/validate', methods=['POST'])
def twitch_validate():
    tok = str((request.get_json(silent=True) or {}).get('token') or '')
    try:
        resp = requests.get(f'{_TWITCH_ID}/validate',
                            headers={'Authorization': f'OAuth {tok}'}, timeout=15)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return jsonify(body), resp.status_code
    except requests.RequestException:
        return jsonify({'message': 'twitch unreachable'}), 502


@app.route('/api/live-version')
def api_live_version():
    """Freshness probe for /live.

    Tracks both DB rows and the active Twitch channel set. A stream-only change
    refreshes the watch layout, but report-card stats still come from match rows
    in build_super_live.
    """
    try:
        since_count_raw = request.args.get('count', '')
        since_count = int(since_count_raw) if str(since_count_raw).strip() else None
    except (TypeError, ValueError):
        since_count = None
    since_streams = str(request.args.get('streams', '') or '')
    try:
        current_count = load_db_row_count(ENGINE)
        refresh_match_caches_for_count(current_count)
        streams = build_stream_embeds()
        stream_key = stream_key_for_streams(streams)
        return jsonify({
            'ok': True,
            'count': int(current_count),
            'stream_key': stream_key,
            'stream_count': len(streams),
            'changed': bool(
                (since_count is not None and int(current_count) != since_count)
                or stream_key != since_streams
            ),
            'poll_seconds': LIVE_STREAM_POLL_SECONDS,
        })
    except Exception as exc:
        logger.warning('live-version failed: %s', exc)
        return jsonify({'ok': False, 'error': 'live version check failed'}), 500



@app.route('/api/export')
def export_data():
    """Export filtered data as CSV or JSON."""
    # Export the full column set (incl. per-medal columns) so downloads keep
    # the same shape as before the lean-cache split.
    df = medal_df()
    format_type = request.args.get('format', 'json')
    player = request.args.get('player', 'all')
    playlist = request.args.get('playlist', 'all')
    mode = request.args.get('mode', 'all')
    
    filtered = apply_filters(df, player, playlist, mode)
    
    if format_type == 'csv':
        csv_data = filtered.to_csv(index=False)
        return Response(csv_data,
                       mimetype='text/csv',
                       headers={'Content-Disposition': 'attachment;filename=halo_stats.csv'})
    else:
        return Response(filtered.to_json(orient='records'),
                       mimetype='application/json')

def build_compare_radar(df: pd.DataFrame) -> dict | None:
    """Per-player absolute report-card category scores for a comparison radar."""
    if df is None or df.empty or 'player_gamertag' not in df.columns:
        return None
    cats = ['Slaying', 'Gunplay', 'Impact', 'Survival', 'Medals']
    players = []
    for p in unique_sorted(df['player_gamertag']):
        pdf = df[df['player_gamertag'] == p]
        ranked = _ranked_only(pdf)
        rc = build_player_report_card(ranked if not ranked.empty else pdf)
        if not rc:
            continue
        by = {c['label']: c['score'] for c in rc['categories']}
        players.append({'name': p, 'scores': [by.get(c, 0) for c in cats]})
    if not players:
        return None
    return {'labels': cats, 'players': players}


# Bar colors keyed on the generic palette classes (see get_player_class).
COMPARE_BAR_COLORS = {f'player-c{i}': c for i, c in enumerate(PLAYER_PALETTE)}
_COMPARE_FALLBACK_COLORS = ['#3dbfb8', '#ff7a3d', '#a78bfa', '#f472b6', '#facc15', '#4ade80']


def build_compare_matrix(df: pd.DataFrame) -> dict:
    """Side-by-side, stat-by-stat comparison of every tracked player.

    Returns players (with colors) and grouped rows; each row carries every
    player's formatted value, a 0..1 heat norm, and best/worst flags so the
    template can highlight the winner and draw relative-standing bars.
    """
    if df.empty or 'player_gamertag' not in df.columns:
        return {}
    players = [p for p in unique_sorted(df['player_gamertag']) if p]
    if not players:
        return {}

    def _metrics(pdf):
        g = len(pdf)
        if not g:
            return None
        kills = safe_col_sum(pdf, 'kills')
        deaths = safe_col_sum(pdf, 'deaths')
        assists = safe_col_sum(pdf, 'assists')
        shots_hit = safe_col_sum(pdf, 'shots_hit')
        shots_fired = safe_col_sum(pdf, 'shots_fired')
        if shots_fired > 0:
            acc = shots_hit / shots_fired * 100
        else:
            acc = numeric_series(pdf, 'accuracy').mean()
            if acc <= 1:
                acc *= 100
        dmg = safe_col_sum(pdf, 'damage_dealt')
        dmg_taken = safe_col_sum(pdf, 'damage_taken')
        dur = safe_col_sum(pdf, 'duration')
        wins = (pdf['outcome'].astype(str).str.lower() == 'win').sum()
        return {
            'games': g,
            'win_pct': wins / g * 100,
            'kda': safe_kda(kills / g, assists / g, deaths / g),
            'kills_pg': kills / g,
            'deaths_pg': deaths / g,
            'assists_pg': assists / g,
            'accuracy': acc,
            'headshot_pct': (safe_col_sum(pdf, 'headshot_kills') / kills * 100) if kills > 0 else 0,
            'power_pct': (safe_col_sum(pdf, 'power_weapon_kills') / kills * 100) if kills > 0 else 0,
            'dmg_pg': dmg / g,
            'dmg_diff_pg': (dmg - dmg_taken) / g,
            'dmg_per_min': dmg / (dur / 60) if dur > 0 else 0,
            'obj_pg': objective_score_series(pdf).sum() / g,
            'callouts_pg': safe_col_sum(pdf, 'callout_assists') / g,
            'medals_pg': safe_col_sum(pdf, 'medal_count') / g,
            'snipes_pg': sniper_series(pdf).sum() / g,
            'avg_life': safe_col_sum(pdf, 'average_life_duration') / g if 'average_life_duration' in pdf.columns else 0,
        }

    # Parse dates once for the "past 30 days" sub-window (robust to ms-epoch).
    dcol = None
    if 'date' in df.columns:
        dcol = pd.to_datetime(df['date'], errors='coerce', utc=True)
        if dcol.isna().all():
            dcol = pd.to_datetime(df['date'], unit='ms', errors='coerce', utc=True)
    cutoff = pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=30)

    per = {}
    per30 = {}
    for p in players:
        mask = df['player_gamertag'] == p
        m = _metrics(df[mask])
        if m is None:
            continue
        per[p] = m
        per30[p] = _metrics(df[mask & (dcol >= cutoff)]) if dcol is not None else None

    players = [p for p in players if p in per]
    if not players:
        return {}

    def color_for(idx: int, css: str) -> str:
        return COMPARE_BAR_COLORS.get(css) or _COMPARE_FALLBACK_COLORS[idx % len(_COMPARE_FALLBACK_COLORS)]

    player_meta = []
    for idx, p in enumerate(players):
        css = get_player_class(p)
        player_meta.append({'name': p, 'css': css, 'color': color_for(idx, css), 'games': per[p]['games']})
    color_by_player = {m['name']: m['color'] for m in player_meta}

    def fmt(v, d, suffix=''):
        return f"{format_float(v, d)}{suffix}"

    groups_def = [
        ('Overview', [
            ('win_pct', 'Win %', True, lambda v: fmt(v, 1, '%')),
            ('kda', 'KDA', True, lambda v: fmt(v, 2)),
        ]),
        ('Slaying', [
            ('kills_pg', 'Kills / Game', True, lambda v: fmt(v, 1)),
            ('deaths_pg', 'Deaths / Game', False, lambda v: fmt(v, 1)),
            ('assists_pg', 'Assists / Game', True, lambda v: fmt(v, 1)),
            ('accuracy', 'Accuracy', True, lambda v: fmt(v, 1, '%')),
            ('headshot_pct', 'Headshot %', True, lambda v: fmt(v, 1, '%')),
        ]),
        ('Firepower', [
            ('dmg_pg', 'Damage / Game', True, lambda v: fmt(v, 0)),
            ('dmg_diff_pg', 'Damage Diff / Game', True, lambda v: format_signed(v, 0)),
            ('dmg_per_min', 'Damage / Min', True, lambda v: fmt(v, 0)),
            ('power_pct', 'Power Weapon %', True, lambda v: fmt(v, 1, '%')),
            ('snipes_pg', 'Snipes / Game', True, lambda v: fmt(v, 2)),
        ]),
        ('Impact', [
            ('obj_pg', 'Objective Score / Game', True, lambda v: fmt(v, 1)),
            ('callouts_pg', 'Callouts / Game', True, lambda v: fmt(v, 1)),
            ('medals_pg', 'Medals / Game', True, lambda v: fmt(v, 2)),
            ('avg_life', 'Avg Life (s)', True, lambda v: fmt(v, 1)),
        ]),
    ]

    groups = []
    for gtitle, rows_def in groups_def:
        rows = []
        for key, label, hib, fmtfn in rows_def:
            vals = {p: safe_float(per[p].get(key, 0)) for p in players}
            vlist = list(vals.values())
            vmin, vmax = min(vlist), max(vlist)
            tie = (vmax == vmin)
            best_val = vmax if hib else vmin
            worst_val = vmin if hib else vmax
            span = (vmax - vmin) or 1
            cells = []
            for p in players:
                v = vals[p]
                norm = (v - vmin) / span if hib else (vmax - v) / span
                # Past-30-day value for the same stat (recent form), if any.
                v30 = None
                m30 = per30.get(p)
                if m30 and key in m30:
                    v30 = fmtfn(safe_float(m30.get(key, 0)))
                cells.append({
                    'player': p,
                    'css': get_player_class(p),
                    'color': color_by_player[p],
                    'value': fmtfn(v),
                    'val30': v30,
                    'num': v,
                    'norm': round(norm, 3),
                    'best': (not tie and v == best_val),
                    'worst': (not tie and v == worst_val),
                })
            rows.append({'label': label, 'higher_is_better': hib, 'cells': cells})
        groups.append({'title': gtitle, 'rows': rows})

    return {'players': player_meta, 'groups': groups}


@app.route('/compare')
def compare():
    # Ranked-only so /compare's headline stats match the player page,
    # leaderboard and dashboard (which are all Ranked-scoped).
    df = _ranked_only(medal_df())
    all_players = unique_sorted(df['player_gamertag']) if 'player_gamertag' in df.columns else []
    # ?players=a,b focuses the comparison on a chosen subset (default: everyone).
    raw = request.args.get('players', '')
    selected = [p for p in (s.strip() for s in raw.split(',')) if p and p in all_players]
    fdf = df[df['player_gamertag'].isin(selected)] if selected else df
    picker = [{'name': p, 'css': get_player_class(p), 'selected': (not selected) or (p in selected)}
              for p in all_players]
    player_summaries = build_player_summary(fdf)
    try:
        matrix = build_compare_matrix(fdf)
    except Exception as exc:
        logger.warning('compare matrix failed: %s', exc)
        matrix = None
    try:
        rdf_lean = _ranked_only(cache.get())
        radar_df = rdf_lean[rdf_lean['player_gamertag'].isin(selected)] if selected else rdf_lean
        radar = build_compare_radar(radar_df)
    except Exception as exc:
        logger.warning('compare radar failed: %s', exc)
        radar = None
    return render_template('compare.html',
                          app_title=APP_TITLE,
                          player_summaries=player_summaries,
                          matrix=matrix,
                          radar=radar,
                          compare_picker=picker,
                          selected_players=selected,
                          db_row_count=count_cache.get())


def build_player_summary(df: pd.DataFrame) -> dict:
    """Build comprehensive player analysis comparing all players."""
    if df.empty or 'player_gamertag' not in df.columns:
        return {}
    
    all_players = unique_sorted(df['player_gamertag'])
    if not all_players:
        return {}

    medal_cols = [
        col for col in df.columns
        if col.startswith('medal_') and col != 'medal_count'
    ]
    medal_totals = []
    for col in medal_cols:
        total = pd.to_numeric(df[col], errors='coerce').fillna(0).sum()
        if total > 0:
            medal_totals.append((col, total))
    medal_totals.sort(key=lambda item: item[1], reverse=True)
    compare_medal_cols = [col for col, _ in medal_totals[:50]]

    player_metrics = {}
    for player in all_players:
        player_df = df[df['player_gamertag'] == player]
        if player_df.empty:
            continue

        games = len(player_df)
        wins = (player_df['outcome'].astype(str).str.lower() == 'win').sum()

        kills_pg = safe_col_sum(player_df, 'kills') / games
        deaths_pg = safe_col_sum(player_df, 'deaths') / games
        assists_pg = safe_col_sum(player_df, 'assists') / games

        total_shots_hit = safe_col_sum(player_df, 'shots_hit')
        total_shots_fired = safe_col_sum(player_df, 'shots_fired')
        if total_shots_fired > 0:
            accuracy = total_shots_hit / total_shots_fired * 100
        else:
            accuracy = numeric_series(player_df, 'accuracy').mean()
            if accuracy <= 1:
                accuracy *= 100

        total_dmg_dealt = safe_col_sum(player_df, 'damage_dealt')
        total_dmg_taken = safe_col_sum(player_df, 'damage_taken')
        dmg_pg = total_dmg_dealt / games
        dmg_diff_pg = (total_dmg_dealt - total_dmg_taken) / games

        total_duration = safe_col_sum(player_df, 'duration')
        dmg_per_min = total_dmg_dealt / (total_duration / 60) if total_duration > 0 else 0

        medals_pg = safe_col_sum(player_df, 'medal_count') / games

        win_pct = wins / games * 100
        kda = safe_kda(kills_pg, assists_pg, deaths_pg)

        total_kills = safe_col_sum(player_df, 'kills')
        headshot_pct = (safe_col_sum(player_df, 'headshot_kills') / total_kills * 100) if total_kills > 0 else 0
        melee_pct = (safe_col_sum(player_df, 'melee_kills') / total_kills * 100) if total_kills > 0 else 0
        grenade_pct = (safe_col_sum(player_df, 'grenade_kills') / total_kills * 100) if total_kills > 0 else 0
        power_pct = (safe_col_sum(player_df, 'power_weapon_kills') / total_kills * 100) if total_kills > 0 else 0

        avg_life = safe_col_sum(player_df, 'average_life_duration') / games if 'average_life_duration' in player_df.columns else 0
        obj_score_pg = objective_score_series(player_df).sum() / games if games else 0
        callouts_pg = safe_col_sum(player_df, 'callout_assists') / games
        score_pg = score_series(player_df).sum() / games if games else 0
        betrayals_pg = safe_col_sum(player_df, 'betrayals') / games
        suicides_pg = safe_col_sum(player_df, 'suicides') / games

        metrics = {
            'games': games,
            'win_pct': win_pct,
            'kda': kda,
            'kills_pg': kills_pg,
            'deaths_pg': deaths_pg,
            'assists_pg': assists_pg,
            'accuracy': accuracy,
            'dmg_pg': dmg_pg,
            'dmg_diff_pg': dmg_diff_pg,
            'dmg_per_min': dmg_per_min,
            'medals_pg': medals_pg,
            'headshot_pct': headshot_pct,
            'melee_pct': melee_pct,
            'grenade_pct': grenade_pct,
            'power_pct': power_pct,
            'avg_life': avg_life,
            'obj_score_pg': obj_score_pg,
            'callouts_pg': callouts_pg,
            'score_pg': score_pg,
            'betrayals_pg': betrayals_pg,
            'suicides_pg': suicides_pg
        }

        for col in compare_medal_cols:
            metrics[col] = safe_col_sum(player_df, col) / games if games else 0

        player_metrics[player] = metrics

    if not player_metrics:
        return {}

    def format_value(value: float, digits: int, suffix: str = '') -> str:
        return f"{format_float(value, digits)}{suffix}"

    stat_metric_defs = [
        {'key': 'kills_pg', 'label': 'Kills/Game', 'higher_is_better': True, 'is_medal': False, 'format': lambda v: format_value(v, 1)},
        {'key': 'deaths_pg', 'label': 'Deaths/Game', 'higher_is_better': False, 'is_medal': False, 'format': lambda v: format_value(v, 1)},
        {'key': 'assists_pg', 'label': 'Assists/Game', 'higher_is_better': True, 'is_medal': False, 'format': lambda v: format_value(v, 1)},
        {'key': 'kda', 'label': 'KDA', 'higher_is_better': True, 'is_medal': False, 'format': lambda v: format_value(v, 2)},
        {'key': 'accuracy', 'label': 'Accuracy', 'higher_is_better': True, 'is_medal': False, 'format': lambda v: format_value(v, 1, '%')},
        {'key': 'win_pct', 'label': 'Win %', 'higher_is_better': True, 'is_medal': False, 'format': lambda v: format_value(v, 1, '%')},
        {'key': 'dmg_pg', 'label': 'Damage/Game', 'higher_is_better': True, 'is_medal': False, 'format': lambda v: format_value(v, 0)},
        {'key': 'dmg_diff_pg', 'label': 'Damage Diff/Game', 'higher_is_better': True, 'is_medal': False, 'format': lambda v: format_signed(v, 0)},
        {'key': 'dmg_per_min', 'label': 'Damage/Min', 'higher_is_better': True, 'is_medal': False, 'format': lambda v: format_value(v, 0)},
        {'key': 'score_pg', 'label': 'Score/Game', 'higher_is_better': True, 'is_medal': False, 'format': lambda v: format_value(v, 0)},
        {'key': 'obj_score_pg', 'label': 'Objective Score/Game', 'higher_is_better': True, 'is_medal': False, 'format': lambda v: format_value(v, 1)},
        {'key': 'callouts_pg', 'label': 'Callouts/Game', 'higher_is_better': True, 'is_medal': False, 'format': lambda v: format_value(v, 1)},
        {'key': 'headshot_pct', 'label': 'Headshot %', 'higher_is_better': True, 'is_medal': False, 'format': lambda v: format_value(v, 1, '%')},
        {'key': 'melee_pct', 'label': 'Melee Kill %', 'higher_is_better': True, 'is_medal': False, 'format': lambda v: format_value(v, 1, '%')},
        {'key': 'grenade_pct', 'label': 'Grenade Kill %', 'higher_is_better': True, 'is_medal': False, 'format': lambda v: format_value(v, 1, '%')},
        {'key': 'power_pct', 'label': 'Power Weapon %', 'higher_is_better': True, 'is_medal': False, 'format': lambda v: format_value(v, 1, '%')},
        {'key': 'avg_life', 'label': 'Avg Life', 'higher_is_better': True, 'is_medal': False, 'format': lambda v: format_value(v, 1)},
        {'key': 'betrayals_pg', 'label': 'Betrayals/Game', 'higher_is_better': False, 'is_medal': False, 'format': lambda v: format_value(v, 2)},
        {'key': 'suicides_pg', 'label': 'Suicides/Game', 'higher_is_better': False, 'is_medal': False, 'format': lambda v: format_value(v, 2)},
    ]

    medal_metric_defs = [
        {'key': 'medals_pg', 'label': 'Medals/Game', 'higher_is_better': True, 'is_medal': True, 'format': lambda v: format_value(v, 2)}
    ]

    for col in compare_medal_cols:
        label = f"{col.replace('medal_', '').replace('_', ' ').title()} Medals/Game"
        medal_metric_defs.append({
            'key': col,
            'label': label,
            'higher_is_better': True,
            'is_medal': True,
            'format': lambda v: format_value(v, 2)
        })

    norm_cache = {}

    def get_norm(metric_key: str, higher_is_better: bool = True) -> dict:
        cache_key = (metric_key, higher_is_better)
        if cache_key in norm_cache:
            return norm_cache[cache_key]
        values = {player: safe_float(player_metrics[player].get(metric_key, 0)) for player in player_metrics}
        if not values:
            norm_cache[cache_key] = {player: 0.5 for player in player_metrics}
            return norm_cache[cache_key]
        min_val = min(values.values())
        max_val = max(values.values())
        if max_val == min_val:
            norm_cache[cache_key] = {player: 0.5 for player in values}
            return norm_cache[cache_key]
        if higher_is_better:
            norm_cache[cache_key] = {
                player: (value - min_val) / (max_val - min_val) for player, value in values.items()
            }
        else:
            norm_cache[cache_key] = {
                player: (max_val - value) / (max_val - min_val) for player, value in values.items()
            }
        return norm_cache[cache_key]

    title_order = ['Objective Player', 'Slayer', 'Support', 'Sniper', 'Survivor', 'All-Rounder']
    title_score_fns = {
        'Objective Player': lambda player: (
            0.6 * get_norm('obj_score_pg')[player]
            + 0.2 * get_norm('callouts_pg')[player]
            + 0.2 * get_norm('win_pct')[player]
        ),
        'Slayer': lambda player: (
            0.35 * get_norm('kills_pg')[player]
            + 0.25 * get_norm('dmg_per_min')[player]
            + 0.2 * get_norm('kda')[player]
            + 0.2 * get_norm('dmg_diff_pg')[player]
        ),
        'Support': lambda player: (
            0.5 * get_norm('assists_pg')[player]
            + 0.3 * get_norm('callouts_pg')[player]
            + 0.2 * get_norm('win_pct')[player]
        ),
        'Sniper': lambda player: (
            0.35 * get_norm('accuracy')[player]
            + 0.25 * get_norm('headshot_pct')[player]
            + 0.2 * get_norm('medal_snipe')[player]
            + 0.2 * get_norm('medal_no_scope')[player]
        ),
        'Survivor': lambda player: (
            0.5 * get_norm('avg_life')[player]
            + 0.3 * get_norm('kda')[player]
            + 0.2 * get_norm('deaths_pg', False)[player]
        ),
        'All-Rounder': lambda player: (
            get_norm('kills_pg')[player]
            + get_norm('assists_pg')[player]
            + get_norm('accuracy')[player]
            + get_norm('obj_score_pg')[player]
            + get_norm('win_pct')[player]
            + get_norm('kda')[player]
        ) / 6
    }

    writeup_metric_defs = [
        {'key': 'kills_pg', 'label': 'Kills/Game', 'higher_is_better': True, 'format': lambda v: format_value(v, 1)},
        {'key': 'assists_pg', 'label': 'Assists/Game', 'higher_is_better': True, 'format': lambda v: format_value(v, 1)},
        {'key': 'kda', 'label': 'KDA', 'higher_is_better': True, 'format': lambda v: format_value(v, 2)},
        {'key': 'accuracy', 'label': 'Accuracy', 'higher_is_better': True, 'format': lambda v: format_value(v, 1, '%')},
        {'key': 'dmg_per_min', 'label': 'Damage/Min', 'higher_is_better': True, 'format': lambda v: format_value(v, 0)},
        {'key': 'dmg_diff_pg', 'label': 'Damage Diff/Game', 'higher_is_better': True, 'format': lambda v: format_signed(v, 0)},
        {'key': 'obj_score_pg', 'label': 'Objective Score/Game', 'higher_is_better': True, 'format': lambda v: format_signed(v, 1)},
        {'key': 'callouts_pg', 'label': 'Callouts/Game', 'higher_is_better': True, 'format': lambda v: format_value(v, 1)},
        {'key': 'avg_life', 'label': 'Avg Life', 'higher_is_better': True, 'format': lambda v: format_value(v, 1)},
        {'key': 'medals_pg', 'label': 'Medals/Game', 'higher_is_better': True, 'format': lambda v: format_value(v, 2)},
        {'key': 'headshot_pct', 'label': 'Headshot %', 'higher_is_better': True, 'format': lambda v: format_value(v, 1, '%')},
        {'key': 'deaths_pg', 'label': 'Deaths/Game', 'higher_is_better': False, 'format': lambda v: format_value(v, 1)},
        {'key': 'suicides_pg', 'label': 'Suicides/Game', 'higher_is_better': False, 'format': lambda v: format_value(v, 2)},
        {'key': 'betrayals_pg', 'label': 'Betrayals/Game', 'higher_is_better': False, 'format': lambda v: format_value(v, 2)}
    ]

    def gap_score(values: list[float], value: float, higher_is_better: bool, is_best: bool) -> float:
        if len(values) < 2:
            return 0
        sorted_vals = sorted(values, reverse=higher_is_better)
        if is_best:
            return abs(value - sorted_vals[1])
        return abs(sorted_vals[-2] - value)

    def build_extremes(metric_defs: list[dict]) -> tuple[dict, dict, dict, dict]:
        strengths_strict = {player: [] for player in player_metrics}
        strengths_tied = {player: [] for player in player_metrics}
        weaknesses_strict = {player: [] for player in player_metrics}
        weaknesses_tied = {player: [] for player in player_metrics}

        for metric in metric_defs:
            values = {player: safe_float(player_metrics[player].get(metric['key'], 0)) for player in player_metrics}
            value_list = list(values.values())
            if not value_list:
                continue
            max_val = max(value_list)
            min_val = min(value_list)
            if max_val == min_val:
                continue

            if metric['higher_is_better']:
                best_players = [p for p, v in values.items() if v == max_val]
                worst_players = [p for p, v in values.items() if v == min_val]
            else:
                best_players = [p for p, v in values.items() if v == min_val]
                worst_players = [p for p, v in values.items() if v == max_val]

            for player in best_players:
                score = gap_score(value_list, values[player], metric['higher_is_better'], True)
                label = f"{metric['label']} ({metric['format'](values[player])})"
                entry = {'key': metric['key'], 'label': label, 'score': score, 'is_medal': metric['is_medal'], 'tier': 'extreme'}
                if len(best_players) == 1:
                    strengths_strict[player].append(entry)
                else:
                    strengths_tied[player].append(entry)

            for player in worst_players:
                score = gap_score(value_list, values[player], metric['higher_is_better'], False)
                label = f"{metric['label']} ({metric['format'](values[player])})"
                entry = {'key': metric['key'], 'label': label, 'score': score, 'is_medal': metric['is_medal'], 'tier': 'extreme'}
                if len(worst_players) == 1:
                    weaknesses_strict[player].append(entry)
                else:
                    weaknesses_tied[player].append(entry)

        return strengths_strict, strengths_tied, weaknesses_strict, weaknesses_tied

    def build_relative_candidates(metric_defs: list[dict]) -> tuple[dict, dict]:
        strength_candidates = {player: [] for player in player_metrics}
        weakness_candidates = {player: [] for player in player_metrics}

        for metric in metric_defs:
            values = {player: safe_float(player_metrics[player].get(metric['key'], 0)) for player in player_metrics}
            value_list = list(values.values())
            if not value_list:
                continue
            max_val = max(value_list)
            min_val = min(value_list)
            if max_val == min_val:
                continue

            for player, value in values.items():
                if metric['higher_is_better']:
                    strength_score = (value - min_val) / (max_val - min_val)
                else:
                    strength_score = (max_val - value) / (max_val - min_val)
                weakness_score = 1 - strength_score
                label = f"{metric['label']} ({metric['format'](value)})"
                strength_candidates[player].append({
                    'key': metric['key'],
                    'label': label,
                    'score': strength_score,
                    'is_medal': metric['is_medal'],
                    'tier': 'relative'
                })
                weakness_candidates[player].append({
                    'key': metric['key'],
                    'label': label,
                    'score': weakness_score,
                    'is_medal': metric['is_medal'],
                    'tier': 'relative'
                })

        return strength_candidates, weakness_candidates

    def build_writeup_candidates(metric_defs: list[dict]) -> tuple[dict, dict]:
        strength_candidates = {player: [] for player in player_metrics}
        weakness_candidates = {player: [] for player in player_metrics}

        for metric in metric_defs:
            values = {player: safe_float(player_metrics[player].get(metric['key'], 0)) for player in player_metrics}
            if not values:
                continue
            max_val = max(values.values())
            min_val = min(values.values())
            if max_val == min_val:
                continue
            norms = get_norm(metric['key'], metric['higher_is_better'])
            for player, value in values.items():
                entry = {
                    'key': metric['key'],
                    'label': metric['label'],
                    'value': value,
                    'score': norms[player],
                    'format': metric['format']
                }
                strength_candidates[player].append(entry)
                weakness_candidates[player].append(entry)

        return strength_candidates, weakness_candidates

    def select_top_entries(primary: list[dict], fallback: list[dict], extra: list[dict], target_count: int = 5) -> list[dict]:
        primary_sorted = sorted(primary, key=lambda x: x['score'], reverse=True)
        fallback_sorted = sorted(fallback, key=lambda x: x['score'], reverse=True)
        extra_sorted = sorted(extra, key=lambda x: x['score'], reverse=True)
        selected = []
        used_keys = set()
        for entry in primary_sorted:
            if len(selected) >= target_count:
                break
            selected.append(entry)
            used_keys.add(entry['key'])
        for entry in fallback_sorted:
            if len(selected) >= target_count:
                break
            if entry['key'] in used_keys:
                continue
            selected.append(entry)
            used_keys.add(entry['key'])
        for entry in extra_sorted:
            if len(selected) >= target_count:
                break
            if entry['key'] in used_keys:
                continue
            selected.append(entry)
            used_keys.add(entry['key'])
        return selected[:target_count]

    def select_writeup_entries(candidates: list[dict], count: int, reverse: bool, exclude_keys: set[str] | None = None) -> list[dict]:
        exclude_keys = exclude_keys or set()
        selected = []
        for entry in sorted(candidates, key=lambda x: x['score'], reverse=reverse):
            if entry['key'] in exclude_keys:
                continue
            selected.append(entry)
            exclude_keys.add(entry['key'])
            if len(selected) >= count:
                break
        return selected

    def join_phrases(items: list[str]) -> str:
        if not items:
            return ''
        if len(items) == 1:
            return items[0]
        if len(items) == 2:
            return f"{items[0]} and {items[1]}"
        return f"{', '.join(items[:-1])}, and {items[-1]}"

    def build_stat_span(entry: dict, css_class: str) -> str:
        label_text = html.escape(entry['label'])
        value_text = html.escape(entry['format'](entry['value']))
        return f"<span class='{css_class}'>{label_text} {value_text}</span>"

    stat_strengths_strict, stat_strengths_tied, stat_weaknesses_strict, stat_weaknesses_tied = build_extremes(stat_metric_defs)
    medal_strengths_strict, medal_strengths_tied, medal_weaknesses_strict, medal_weaknesses_tied = build_extremes(medal_metric_defs)
    stat_relative_strengths, stat_relative_weaknesses = build_relative_candidates(stat_metric_defs)
    medal_relative_strengths, medal_relative_weaknesses = build_relative_candidates(medal_metric_defs)
    writeup_strength_candidates, writeup_weakness_candidates = build_writeup_candidates(writeup_metric_defs)

    player_profiles = {}
    for player, metrics in player_metrics.items():
        strengths_entries = select_top_entries(
            stat_strengths_strict[player],
            stat_strengths_tied[player],
            stat_relative_strengths[player]
        )
        weakness_entries = select_top_entries(
            stat_weaknesses_strict[player],
            stat_weaknesses_tied[player],
            stat_relative_weaknesses[player]
        )
        medal_strengths_entries = select_top_entries(
            medal_strengths_strict[player],
            medal_strengths_tied[player],
            medal_relative_strengths[player]
        )
        medal_weaknesses_entries = select_top_entries(
            medal_weaknesses_strict[player],
            medal_weaknesses_tied[player],
            medal_relative_weaknesses[player]
        )

        title_scores = {name: score_fn(player) for name, score_fn in title_score_fns.items()}
        title = sorted(title_scores.items(), key=lambda item: (-item[1], title_order.index(item[0])))[0][0]
        article = 'an' if title[:1].lower() in 'aeiou' else 'a'

        top_medal_label = ''
        top_medal_value = 0.0
        for col in compare_medal_cols:
            value = safe_float(metrics.get(col, 0))
            if value > top_medal_value:
                top_medal_value = value
                top_medal_label = col.replace('medal_', '').replace('_', ' ').title()

        safe_player = html.escape(str(player))
        overview_sentence = (
            f"{safe_player} is {article} {title.lower()} averaging "
            f"{format_float(metrics['kills_pg'], 1)} kills/game, "
            f"{format_float(metrics['assists_pg'], 1)} assists/game, "
            f"and a {format_float(metrics['kda'], 2)} KDA with "
            f"{format_float(metrics['win_pct'], 1)}% wins, while objective score sits at "
            f"{format_signed(metrics['obj_score_pg'], 1)} per game."
        )

        writeup_strength_entries = select_writeup_entries(
            writeup_strength_candidates[player],
            2,
            True
        )
        writeup_strength_keys = {entry['key'] for entry in writeup_strength_entries}
        writeup_weakness_entries = select_writeup_entries(
            writeup_weakness_candidates[player],
            2,
            False,
            writeup_strength_keys
        )

        strength_bits = [build_stat_span(entry, 'stat-good') for entry in writeup_strength_entries]
        weakness_bits = [build_stat_span(entry, 'stat-bad') for entry in writeup_weakness_entries]
        strength_phrase = join_phrases(strength_bits)
        weakness_phrase = join_phrases(weakness_bits)

        if strength_phrase:
            strength_intro = f"Strengths show up in {strength_phrase}"
        else:
            strength_intro = "Strengths are spread across the stat line"

        if top_medal_label:
            safe_medal_label = html.escape(top_medal_label)
            medal_clause = f", led by {safe_medal_label} ({format_float(top_medal_value, 2)}/game)"
        else:
            medal_clause = ""

        strength_sentence = (
            f"{strength_intro}; medal pace is {format_float(metrics['medals_pg'], 2)} per game"
            f"{medal_clause}, and accuracy sits at {format_float(metrics['accuracy'], 1)}% with "
            f"{format_float(metrics['headshot_pct'], 1)}% headshots."
        )

        if weakness_phrase:
            improve_sentence = (
                f"Areas to sharpen include {weakness_phrase}, so tightening those should lift consistency."
            )
        else:
            improve_sentence = (
                "Areas to sharpen are minimal right now, but small gains in damage output and accuracy "
                "could lift consistency."
            )

        def format_entry(entry: dict, is_strength: bool) -> str:
            if entry.get('tier') == 'relative':
                prefix = 'Strong' if is_strength else 'Weak'
            else:
                prefix = 'Best' if is_strength else 'Worst'
            return f"{prefix} {entry['label']}"

        strengths = [format_entry(item, True) for item in strengths_entries]
        weaknesses = [format_entry(item, False) for item in weakness_entries]
        medal_strengths = [format_entry(item, True) for item in medal_strengths_entries]
        medal_weaknesses = [format_entry(item, False) for item in medal_weaknesses_entries]

        player_profiles[player] = {
            'games': metrics['games'],
            'win_pct': metrics['win_pct'],
            'kda': metrics['kda'],
            'kills_pg': metrics['kills_pg'],
            'deaths_pg': metrics['deaths_pg'],
            'assists_pg': metrics['assists_pg'],
            'accuracy': metrics['accuracy'],
            'dmg_pg': metrics['dmg_pg'],
            'dmg_diff_pg': metrics['dmg_diff_pg'],
            'dmg_per_min': metrics['dmg_per_min'],
            'medals_pg': metrics['medals_pg'],
            'strengths': strengths,
            'weaknesses': weaknesses,
            'medal_strengths': medal_strengths,
            'medal_weaknesses': medal_weaknesses,
            'title': title,
            'writeup': f"{overview_sentence} {strength_sentence} {improve_sentence}"
        }

    return player_profiles
@app.route('/match/<match_id>')
def match(match_id):
    # build_match_details renders the per-match medal breakdown generically.
    df = medal_df()
    match_rows = build_match_details(df, match_id)
    try:
        detail = build_match_page(df, match_id)
    except Exception as exc:
        logger.warning('match detail failed for %s: %s', match_id, exc)
        detail = None
    try:
        full_scoreboard = build_full_scoreboard(ENGINE, match_id)
    except Exception as exc:
        logger.warning('full scoreboard failed for %s: %s', match_id, exc)
        full_scoreboard = None
    return render_template('match.html',
                          app_title=APP_TITLE,
                          match_rows=match_rows,
                          detail=detail,
                          full_scoreboard=full_scoreboard,
                          match_id=match_id,
                          db_row_count=count_cache.get())


@app.route('/w/<token>')
def watch_short(token):
    """Tiny share link for a game's synced rewatch: /w/<first 8 chars of the
    match id> 302s to the full MultiTwitch watch URL. Falls back to the match
    detail page when no rewatch exists for that game."""
    token = str(token).strip().lower()
    if not (4 <= len(token) <= 40) or not all(c in '0123456789abcdef-' for c in token):
        return Response('Not found', status=404)
    df = medal_df()
    if df.empty or 'match_id' not in df.columns:
        return Response('Not found', status=404)
    mids = df['match_id'].astype(str)
    hit = df[mids.str.lower().str.startswith(token)]
    if hit.empty:
        return Response('Not found', status=404)
    mid = str(hit.iloc[0]['match_id'])
    url = watch_url_for(mid)
    return redirect(url if url else f'/match/{mid}')


@app.route('/api/sus-thresholds')
def sus_thresholds_debug():
    """Read-only: the live cheat-check outlier thresholds (for tuning)."""
    return jsonify(_sus_thresholds(ENGINE) or {})


# ── Player intel: account age, career size, CSR history ──
# DB half: everything we've recorded about a lobby player across OUR games
# (encounters, CSR trail). API half: Halo career totals via the spartan token
# the scraper keeps fresh in tokens.json (matchmade game count + first-ever
# match date = account age). API results cached 24h per xuid; fail-soft to
# DB-only when the token/API is unavailable (e.g. fresh fork installs).
_INTEL_CACHE: dict = {}
_INTEL_TTL = 86400


def _spartan_token() -> str:
    try:
        data_dir = os.environ.get('HALO_DATA_DIR', '/data')
        with open(os.path.join(data_dir, 'tokens.json')) as f:
            return json.load(f).get('spartan_token') or ''
    except Exception:
        return ''


def _intel_from_api(xuid: str):
    e = _INTEL_CACHE.get(xuid)
    if e and time.time() - e['ts'] < _INTEL_TTL:
        return e['data']
    data = {}
    tok = _spartan_token()
    if tok:
        hdrs = {'x-343-authorization-spartan': tok, 'Accept': 'application/json'}
        base = f'https://halostats.svc.halowaypoint.com/hi/players/xuid({xuid})'
        try:
            c = requests.get(f'{base}/matches/count', headers=hdrs, timeout=6).json()
            mm = safe_int(c.get('MatchmadeMatchesPlayedCount'))
            data['total_games'] = mm
            # Oldest match = account age. The 'type' filter 400s, and the
            # unfiltered index may not include local matches, so fetch the
            # last PAGE and take its final entry — trying the all-matches
            # count first, then the matchmade count.
            first = None
            for total in (safe_int(c.get('MatchesPlayedCount')), mm):
                if total <= 0 or first:
                    continue
                h = requests.get(f'{base}/matches',
                                 params={'start': max(total - 25, 0), 'count': 25},
                                 headers=hdrs, timeout=6).json()
                res = h.get('Results') or []
                if res:
                    first = (res[-1].get('MatchInfo') or {}).get('StartTime')
                if first:
                    fd = pd.to_datetime(first, utc=True, errors='coerce')
                    if pd.notna(fd):
                        yrs = (pd.Timestamp.now(tz='UTC') - fd).days / 365.25
                        data['first_match'] = fd.strftime('%b %Y')
                        data['account_age'] = (f"{yrs:.1f} yrs" if yrs >= 1
                                               else f"{max((pd.Timestamp.now(tz='UTC') - fd).days, 1)} days")
        except Exception as exc:
            logger.warning('player intel api failed for %s: %s', xuid, exc)
    _INTEL_CACHE[xuid] = {'ts': time.time(), 'data': data}
    return data


@app.route('/api/player-intel/<xuid>')
def player_intel(xuid):
    """Everything we can say about one lobby player: games + CSR trail from
    our own records, career size + account age from the Halo API."""
    xuid = str(xuid).strip()
    if not xuid.isdigit() or len(xuid) > 20:
        return jsonify({'ok': False}), 400
    sql = text("""
        SELECT gamertag, match_date, csr, kda
        FROM halo_match_players
        WHERE player_xuid = :x AND playlist ILIKE :pl
        ORDER BY match_date
    """)
    rows = []
    try:
        with ENGINE.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(sql, {'x': xuid, 'pl': '%rank%'})]
    except SQLAlchemyError:
        pass
    csr_series = [[r['match_date'].strftime('%Y-%m-%d'), int(safe_float(r['csr']))]
                  for r in rows if safe_float(r.get('csr')) > 0 and r.get('match_date')]
    kdas = [safe_float(r['kda']) for r in rows]
    out = {
        'ok': True,
        'gamertag': rows[-1]['gamertag'] if rows else '',
        'games_vs_us': len(rows),
        'first_seen': rows[0]['match_date'].strftime('%b %d, %Y') if rows and rows[0].get('match_date') else '',
        'last_seen': rows[-1]['match_date'].strftime('%b %d, %Y') if rows and rows[-1].get('match_date') else '',
        'avg_kda_vs_us': round(sum(kdas) / len(kdas), 1) if kdas else None,
        'csr_series': csr_series,
        'csr_now': csr_series[-1][1] if csr_series else None,
        'csr_min': min(v for _, v in csr_series) if csr_series else None,
        'csr_max': max(v for _, v in csr_series) if csr_series else None,
    }
    out.update(_intel_from_api(xuid))
    return jsonify(out)


# ---------------------------------------------------------------------------
# Roster routes
# ---------------------------------------------------------------------------

@app.route('/rosters', methods=['GET', 'POST'])
def rosters():
    df = cache.get()
    all_players = unique_sorted(df['player_gamertag']) if not df.empty and 'player_gamertag' in df.columns else []
    message = None
    error = None

    if request.method == 'POST':
        action = request.form.get('action', 'save')
        if action == 'delete':
            try:
                delete_roster(ENGINE, int(request.form.get('roster_id', 0)))
                message = 'Roster deleted.'
            except (ValueError, SQLAlchemyError) as exc:
                error = f'Could not delete roster: {exc}'
        else:
            name = request.form.get('name', '').strip()
            description = request.form.get('description', '').strip()
            gamertags = [g.strip() for g in request.form.get('players', '').split('\n') if g.strip()]
            if not name:
                error = 'Roster name is required.'
            elif not gamertags:
                error = 'Add at least one player.'
            else:
                save_roster(ENGINE, name, description, gamertags)
                message = f'Roster "{name}" saved.'

    roster_list = fetch_rosters(ENGINE)
    status = load_status()
    return render_template('rosters.html',
                           app_title=APP_TITLE,
                           players=all_players,
                           rosters=roster_list,
                           message=message,
                           error=error,
                           last_update=status.get('last_update'),
                           db_row_count=count_cache.get())


# ---------------------------------------------------------------------------
# Snapshot routes
# ---------------------------------------------------------------------------

def build_snapshot_diff(sa: dict, sb: dict) -> dict:
    """Per-player CSR delta between two saved snapshots (b minus a)."""
    def csr_map(snap):
        out = {}
        for r in (snap.get('payload', {}).get('csr_overview') or []):
            out[r.get('player')] = to_number(r.get('current_csr'))
        return out
    ma, mb = csr_map(sa), csr_map(sb)
    rows = []
    for p in unique_sorted(pd.Series(list(set(ma) | set(mb)))):
        a, b = ma.get(p), mb.get(p)
        delta = (b - a) if (a is not None and b is not None) else None
        rows.append({
            'player': p, 'css': get_player_class(p),
            'csr_a': int(a) if a is not None else '–',
            'csr_b': int(b) if b is not None else '–',
            'delta': format_signed(delta, 0) if delta is not None else '–',
            'delta_class': ('heat-good' if (delta or 0) > 0 else 'heat-poor' if (delta or 0) < 0 else ''),
        })
    return {
        'a': {'name': sa['name'], 'date': sa['created_at']},
        'b': {'name': sb['name'], 'date': sb['created_at']},
        'rows': rows,
    }


@app.route('/snapshots', methods=['GET', 'POST'])
def snapshots():
    df = cache.get()
    all_players = unique_sorted(df['player_gamertag']) if not df.empty and 'player_gamertag' in df.columns else []
    message = None
    error = None

    if request.method == 'POST':
        action = request.form.get('action', 'save')
        if action == 'delete':
            try:
                delete_snapshot(ENGINE, int(request.form.get('snapshot_id', 0)))
                message = 'Snapshot deleted.'
            except (ValueError, SQLAlchemyError) as exc:
                error = f'Could not delete: {exc}'
        else:
            name = request.form.get('name', '').strip()
            notes = request.form.get('notes', '').strip()
            players_raw = request.form.getlist('players') or all_players
            date_from = request.form.get('date_from', '').strip()
            date_to = request.form.get('date_to', '').strip()

            if not name:
                error = 'Snapshot name is required.'
            else:
                # Build a lightweight payload from current filtered data
                filtered = df.copy()
                if players_raw and players_raw != all_players:
                    filtered = filtered[filtered['player_gamertag'].isin(players_raw)]
                if date_from:
                    try:
                        filtered = filtered[pd.to_datetime(filtered['date'], utc=True, errors='coerce')
                                            >= pd.to_datetime(date_from, utc=True)]
                    except Exception:
                        pass
                if date_to:
                    try:
                        filtered = filtered[pd.to_datetime(filtered['date'], utc=True, errors='coerce')
                                            <= pd.to_datetime(date_to + 'T23:59:59', utc=True)]
                    except Exception:
                        pass

                payload = {
                    'csr_overview': build_csr_overview(filtered),
                    'ranked_30day': _build_ranked_arena_period(filtered, 30),
                    'map_breakdown': build_breakdown(filtered, 'map'),
                    'generated_at': pd.Timestamp.now(tz='UTC').isoformat(),
                    'total_games': len(filtered),
                }
                result = save_snapshot(ENGINE, name, players_raw, date_from, date_to, notes, payload)
                message = f'Snapshot saved. Share link: /share/{result["share_token"]}'

    snap_list = fetch_snapshots(ENGINE)

    # Two-snapshot diff (per-player CSR delta) when ?cmp_a / ?cmp_b are set.
    comparison = None
    cmp_a = request.args.get('cmp_a')
    cmp_b = request.args.get('cmp_b')
    if cmp_a and cmp_b and cmp_a != cmp_b:
        sa = fetch_snapshot_by_token(ENGINE, cmp_a)
        sb = fetch_snapshot_by_token(ENGINE, cmp_b)
        if sa and sb:
            try:
                comparison = build_snapshot_diff(sa, sb)
            except Exception as exc:
                logger.warning('snapshot diff failed: %s', exc)

    status = load_status()
    return render_template('snapshots.html',
                           app_title=APP_TITLE,
                           players=all_players,
                           snapshots=snap_list,
                           comparison=comparison,
                           cmp_a=cmp_a, cmp_b=cmp_b,
                           message=message,
                           error=error,
                           last_update=status.get('last_update'),
                           db_row_count=count_cache.get())


@app.route('/share/<token>')
def shared_report(token):
    snap = fetch_snapshot_by_token(ENGINE, token)
    if not snap:
        return render_template('base.html',
                               app_title=APP_TITLE,
                               players=[],
                               db_row_count=count_cache.get()), 404
    return render_template('shared_report.html',
                           app_title=APP_TITLE,
                           snap=snap,
                           players=[],
                           db_row_count=count_cache.get())


# ---------------------------------------------------------------------------
# Weekly recap route
# ---------------------------------------------------------------------------

@app.route('/recap')
def weekly_recap():
    df = cache.get()
    try:
        week_offset = max(0, min(51, int(request.args.get('week', 0))))
    except (ValueError, TypeError):
        week_offset = 0

    recap = build_weekly_recap(df, week_offset)

    # Build week selector (current + last 7 weeks)
    week_options = []
    for i in range(8):
        now = pd.Timestamp.now(tz='UTC').tz_convert(APP_TIMEZONE)
        days_since_monday = now.dayofweek
        ws = (now - pd.Timedelta(days=days_since_monday + 7 * i)).normalize()
        we = ws + pd.Timedelta(days=6)
        week_options.append({
            'offset': i,
            'label': ws.strftime('%b %d') + ' – ' + we.strftime('%b %d'),
            'active': i == week_offset,
        })

    status = load_status()
    return render_template('recap.html',
                           app_title=APP_TITLE,
                           players=unique_sorted(df['player_gamertag']) if not df.empty else [],
                           recap=recap,
                           week_offset=week_offset,
                           week_options=week_options,
                           last_update=status.get('last_update'),
                           db_row_count=count_cache.get())


@app.route('/health')
def health():
    try:
        with ENGINE.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok"}, 200
    except Exception as e:
        return {"status": "error", "detail": str(e)}, 503


# ===========================================================================
# NEW FEATURES (2026-06-13) — climb / live / streaks / heatmap / veto / goals
# / coach / cards / nemesis / PWA. All routes degrade gracefully to an empty
# state and never 500 on missing data.
# ===========================================================================

# --- CSR tier model -------------------------------------------------------
CSR_TIERS = ['Bronze', 'Silver', 'Gold', 'Platinum', 'Diamond', 'Onyx']


def _csr_rank_score(tier, sub_tier, value):
    """Single sortable score so players/playlists can be ranked across tiers.
    Onyx has no sub-tiers and is driven by raw value."""
    tier = str(tier or '').title()
    try:
        sub = int(safe_float(sub_tier))
    except Exception:
        sub = 0
    if tier == 'Onyx':
        return 100000 + safe_float(value)
    base = CSR_TIERS.index(tier) * 1000 if tier in CSR_TIERS else 0
    return base + sub * 100 + safe_float(value) / 100.0


def _csr_label(tier, sub_tier, value):
    tier = str(tier or '').title()
    if not tier or tier == 'Nan':
        # Tier name is missing (common — the column is often null), but if we
        # have a numeric CSR, derive the tier from it instead of "Unranked".
        v = safe_float(value)
        if v and v > 0:
            return _sb_tier(v)[0]
        return 'Unranked'
    if tier == 'Onyx':
        return f'Onyx {int(safe_float(value))}'
    sub = int(safe_float(sub_tier)) if sub_tier not in (None, '') else 0
    return f'{tier} {sub}' if sub else tier


def _csr_columns_present():
    try:
        cols = {c.get('name') for c in inspect(ENGINE).get_columns('halo_match_stats')}
        return 'current_csr_value' in cols
    except SQLAlchemyError:
        return False


def fetch_csr_standings(engine, by_playlist=False):
    """Most-recent CSR snapshot per player (optionally per player+playlist),
    pulled straight from the rich csr_* columns the lean cache drops."""
    if not _csr_columns_present():
        return []
    distinct = 'player_gamertag, playlist' if by_playlist else 'player_gamertag'
    order_extra = ', playlist' if by_playlist else ''
    sql = f"""
        SELECT DISTINCT ON ({distinct})
            player_gamertag, playlist, date,
            current_csr_value, current_csr_tier, current_csr_sub_tier,
            current_csr_next_tier, current_csr_next_sub_tier,
            current_csr_tier_start, current_csr_next_tier_start,
            current_csr_measurement_matches_remaining,
            current_csr_initial_measurement_matches,
            season_max_csr_value, season_max_csr_tier, season_max_csr_sub_tier,
            all_time_max_csr_value, all_time_max_csr_tier, all_time_max_csr_sub_tier
        FROM halo_match_stats
        WHERE playlist ILIKE '%Ranked%' AND current_csr_value IS NOT NULL
        ORDER BY {distinct}{order_extra}, date DESC NULLS LAST
    """
    try:
        with engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(text(sql))]
        return rows
    except SQLAlchemyError as exc:
        logger.warning('fetch_csr_standings failed: %s', exc)
        return []


def _recent_csr_per_win(df, player, lookback=50):
    """Average CSR gained on a win and recent win-rate for a player, used to
    project how many wins remain to the next tier."""
    if df.empty:
        return 0.0, 0.0
    pdf = df[df['player_gamertag'] == player].copy()
    if pdf.empty or 'date' not in pdf.columns:
        return 0.0, 0.0
    pdf = pdf.sort_values('date', ascending=False).head(lookback)
    if 'pre_match_csr' in pdf.columns and 'post_match_csr' in pdf.columns:
        delta = pd.to_numeric(pdf['post_match_csr'], errors='coerce') - \
                pd.to_numeric(pdf['pre_match_csr'], errors='coerce')
        wins_delta = delta[delta > 0]
        per_win = float(wins_delta.mean()) if not wins_delta.empty else 0.0
    else:
        per_win = 0.0
    outcomes = pdf['outcome'].astype(str).str.lower()
    decided = outcomes.isin(['win', 'loss'])
    win_rate = float((outcomes == 'win').sum() / decided.sum() * 100) if decided.sum() else 0.0
    return per_win, win_rate


def _csr_progression(value):
    """From a numeric CSR → (tier_label, next_label, next_start, within_pct).
    Sub-tiers step every 50 CSR; the next milestone is the next 50-boundary."""
    v = int(safe_float(value))
    if v <= 0:
        return ('Unranked', '', 0, 0.0)
    label = _sb_tier(v)[0]
    next_start = (v // 50 + 1) * 50
    within = (v % 50) / 50 * 100
    return (label, _sb_tier(next_start)[0], next_start, within)


def build_strength_of_schedule(engine):
    """Average opponent CSR ('strength of schedule') per tracked player, from
    halo_match_players. Splits each player's rated ranked games into solo vs
    with-squad, so you can see a player who grinds solo against tough opponents
    (high opponent CSR, ~even gap) and therefore posts a lower win% at the same
    rank. Opponent CSR = avg CSR of the opposing team(s) in each rated match."""
    try:
        with engine.connect() as conn:
            hmp = pd.read_sql(text(
                "SELECT match_id, player_xuid, gamertag, team_id, outcome, "
                "is_tracked, csr FROM halo_match_players"), conn)
    except Exception as exc:
        logger.warning('strength_of_schedule query failed: %s', exc)
        return []
    if hmp is None or hmp.empty:
        return []

    hmp['csr'] = pd.to_numeric(hmp['csr'], errors='coerce')
    hmp['team_id'] = pd.to_numeric(hmp['team_id'], errors='coerce').fillna(-9).astype(int)

    # Per-(match, team) average CSR among players with a real rating (>0).
    rated = hmp[hmp['csr'] > 0]
    if rated.empty:
        return []
    team_csr = (rated.groupby(['match_id', 'team_id'])['csr'].mean()
                .reset_index())
    match_team_csr: dict = {}
    for _, r in team_csr.iterrows():
        match_team_csr.setdefault(r['match_id'], {})[int(r['team_id'])] = float(r['csr'])

    tracked = hmp[(hmp['is_tracked'] == True) & (hmp['csr'] > 0)]  # noqa: E712
    # (match, team) -> number of tracked players on that team (squad detection).
    sq_count = (hmp[hmp['is_tracked'] == True]  # noqa: E712
                .groupby(['match_id', 'team_id'])['player_xuid'].nunique().to_dict())

    acc: dict = {}
    for _, row in tracked.iterrows():
        mid, tid, gt = row['match_id'], int(row['team_id']), row['gamertag']
        own_csr = float(row['csr'])
        teams = match_team_csr.get(mid, {})
        opp_vals = [v for t, v in teams.items() if t != tid]
        if not opp_vals:
            continue
        opp_csr = sum(opp_vals) / len(opp_vals)
        outcome = str(row['outcome']).lower()
        is_squad = sq_count.get((mid, tid), 0) > 1
        a = acc.setdefault(gt, {'g': 0, 'w': 0, 'l': 0, 'own': 0.0, 'opp': 0.0,
                                'sg': 0, 'sw': 0, 'sl': 0, 'sopp': 0.0,
                                'qg': 0, 'qw': 0, 'ql': 0, 'qopp': 0.0})
        a['g'] += 1; a['own'] += own_csr; a['opp'] += opp_csr
        won = outcome == 'win'; lost = outcome == 'loss'
        a['w'] += won; a['l'] += lost
        if is_squad:
            a['qg'] += 1; a['qopp'] += opp_csr; a['qw'] += won; a['ql'] += lost
        else:
            a['sg'] += 1; a['sopp'] += opp_csr; a['sw'] += won; a['sl'] += lost

    def _wp(w, l):
        d = w + l
        return f"{w / d * 100:.0f}%" if d else '—'

    rows = []
    for gt, a in acc.items():
        if a['g'] < 1:
            continue
        own = a['own'] / a['g']
        opp = a['opp'] / a['g']
        rows.append({
            'player': gt, 'css': get_player_class(gt),
            'games': a['g'],
            'win_pct': round((a['w'] / (a['w'] + a['l']) * 100) if (a['w'] + a['l']) else 0, 0),
            'win_pct_str': _wp(a['w'], a['l']),
            'own_csr': int(round(own)), 'own_label': _sb_tier(own)[0],
            'opp_csr': int(round(opp)), 'opp_label': _sb_tier(opp)[0],
            'gap': int(round(own - opp)), 'gap_str': format_signed(own - opp, 0),
            'solo_games': a['sg'],
            'solo_win_pct_str': _wp(a['sw'], a['sl']),
            'solo_opp_csr': int(round(a['sopp'] / a['sg'])) if a['sg'] else None,
            'squad_games': a['qg'],
            'squad_win_pct_str': _wp(a['qw'], a['ql']),
            'squad_opp_csr': int(round(a['qopp'] / a['qg'])) if a['qg'] else None,
        })
    rows.sort(key=lambda r: r['opp_csr'], reverse=True)
    return rows


def build_climb(df, engine):
    """Climb view from each player's most recent **Ranked Arena** CSR (the spnkr
    current_csr_value standings column is null, so we derive current rank from
    the last Arena post_match_csr — same source as the dashboard rank)."""
    if df is None or df.empty or 'player_gamertag' not in df.columns:
        return {'rows': [], 'playlist_rows': []}
    work = df.copy()
    if 'playlist' in work.columns:
        arena = work[work['playlist'].astype(str).str.contains('Arena', case=False, na=False)]
    else:
        arena = work
    if arena.empty:
        arena = work
    dts = pd.to_datetime(work.get('date'), errors='coerce', utc=True) if 'date' in work.columns else None
    season_cut = pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=90)

    rows = []
    for player in unique_sorted(arena['player_gamertag']):
        pdf = arena[arena['player_gamertag'] == player]
        value = latest_arena_csr(pdf)
        if value is None:
            continue
        pv = pd.to_numeric(pdf.get('post_match_csr'), errors='coerce')
        pv = pv[pv > 0]
        all_time_max = int(pv.max()) if not pv.empty else int(value)
        season_max = all_time_max
        if dts is not None:
            recent = pd.to_numeric(arena.loc[(arena['player_gamertag'] == player) & (dts >= season_cut), 'post_match_csr'], errors='coerce')
            recent = recent[recent > 0]
            if not recent.empty:
                season_max = int(recent.max())
        per_win, win_rate = _recent_csr_per_win(df, player)
        label, next_label, next_start, within = _csr_progression(value)
        to_next = max(0.0, next_start - value)
        wins_to_next = games_to_next = None
        if to_next > 0 and per_win > 0:
            wins_to_next = int(-(-to_next // per_win))
            if win_rate > 0:
                games_to_next = int(-(-wins_to_next * 100 // win_rate))
        rows.append({
            'player': player,
            'rank_label': label,
            'value': int(value),
            'next_label': next_label,
            'to_next': int(to_next),
            'within_pct': round(max(0, min(100, within)), 0),
            'placement_remaining': 0,
            'in_placement': False,
            'per_win': round(per_win, 1),
            'win_rate': round(win_rate, 1),
            'wins_to_next': wins_to_next,
            'games_to_next': games_to_next,
            'season_max': int(season_max),
            'season_max_label': _sb_tier(season_max)[0],
            'all_time_max': int(all_time_max),
            'all_time_max_label': _sb_tier(all_time_max)[0],
            'at_peak': value >= season_max,
        })
    rows.sort(key=lambda r: r['value'], reverse=True)
    add_composite_grades(rows, {'value': True}, 'CSR grade')

    # Per-playlist breakdown: last CSR per (player, playlist) from match data.
    playlist_rows = []
    if 'playlist' in work.columns and 'post_match_csr' in work.columns:
        pw = work.copy()
        pw['_d'] = pd.to_datetime(pw.get('date'), errors='coerce', utc=True)
        pw = pw.sort_values('_d')
        for (player, playlist), g in pw.groupby(['player_gamertag', 'playlist']):
            csr = pd.to_numeric(g['post_match_csr'], errors='coerce')
            csr = csr[csr > 0]
            if csr.empty:
                continue
            value = float(csr.iloc[-1])
            playlist_rows.append({
                'player': player,
                'playlist': normalize_map_name(playlist) or playlist,
                'rank_label': _sb_tier(value)[0],
                'value': int(value),
                'season_max': int(csr.max()),
            })
        playlist_rows.sort(key=lambda r: (r['player'].lower(), -r['value']))
        add_composite_grades(playlist_rows, {'value': True}, 'CSR grade')
    sos_rows = build_strength_of_schedule(engine)
    return {'rows': rows, 'playlist_rows': playlist_rows, 'sos_rows': sos_rows}


@app.route('/climb')
def climb():
    df = cache.get()
    try:
        payload = get_cached_page_payload('climb', lambda: build_climb(df, ENGINE))
    except Exception as exc:
        logger.warning('climb page failed: %s', exc)
        payload = {'rows': [], 'playlist_rows': [], 'sos_rows': []}
    status = load_status()
    return render_template('climb.html', app_title=APP_TITLE,
                           rows=payload['rows'], playlist_rows=payload['playlist_rows'],
                           sos_rows=payload.get('sos_rows', []),
                           last_update=status.get('last_update'),
                           db_row_count=count_cache.get())


# --- Live session + streaks ----------------------------------------------

def _dedup_matches(df):
    """One row per match_id (squad games share a match_id), keeping the tracked
    player's own row — good enough for session W/L since squadmates share an
    outcome when on the same team."""
    if df.empty or 'match_id' not in df.columns:
        return df
    work = df.copy()
    if 'date' in work.columns:
        work = work.sort_values('date', ascending=False)
    return work.drop_duplicates(subset=['match_id'], keep='first')


def compute_streaks(df, player=None):
    """Current and best win streak + current KDA-2.0 streak for a player (or the
    squad if player is None), from most-recent matches backward."""
    work = df if player is None else df[df['player_gamertag'] == player]
    work = _dedup_matches(work) if player is None else work.copy()
    if work.empty or 'date' not in work.columns:
        return {'current_win': 0, 'best_win': 0, 'current_loss': 0, 'last10': '', 'hot_kda': 0}
    work = work.sort_values('date', ascending=False)
    outcomes = work['outcome'].astype(str).str.lower().tolist()
    # current streak (wins positive, losses negative)
    current_win = 0
    for o in outcomes:
        if o == 'win':
            current_win += 1
        else:
            break
    current_loss = 0
    for o in outcomes:
        if o == 'loss':
            current_loss += 1
        elif o == 'win':
            break
    # best win streak across the window
    best_win = run = 0
    for o in outcomes:
        if o == 'win':
            run += 1
            best_win = max(best_win, run)
        else:
            run = 0
    # last-10 W/L string (oldest→newest left to right)
    last10 = ''.join('W' if o == 'win' else ('L' if o == 'loss' else '-')
                     for o in reversed(outcomes[:10]))
    # current hot KDA streak (games in a row >= 2.0)
    hot = 0
    if 'kda' in work.columns:
        for _, r in work.iterrows():
            if safe_float(r.get('kda')) >= 2.0:
                hot += 1
            else:
                break
    return {'current_win': current_win, 'best_win': best_win,
            'current_loss': current_loss, 'last10': last10, 'hot_kda': hot}


def build_streak_strip(df):
    """Compact live-records row for the dashboard: squad overall + per player."""
    if df.empty:
        return {'squad': None, 'players': []}
    squad = compute_streaks(df)
    players = []
    for player in unique_sorted(df['player_gamertag']):
        st = compute_streaks(df, player)
        players.append({'player': player, **st})
    # Surface the hottest first (longest current win streak, then hot KDA).
    players.sort(key=lambda p: (p['current_win'], p['hot_kda']), reverse=True)
    return {'squad': squad, 'players': players}


def _fmt_ago(minutes: float) -> str:
    """Human 'x min ago' / 'x hr ago' for a minutes-elapsed value."""
    m = int(round(minutes))
    if m < 1:
        return 'just now'
    if m < 60:
        return f'{m} min ago'
    h = m / 60
    return f'{h:.0f} hr ago' if h >= 2 else f'{h:.1f} hr ago'


def build_live_now(df):
    """Who's playing right now: tracked players whose most recent match landed
    within LIVE_WINDOW_MINUTES. Reports their line in the in-progress session
    (the cluster of matches within SESSION_GAP_MINUTES of the latest game).
    Returns {'live': bool, 'minutes_ago', 'ago', 'session_games', 'wins',
    'losses', 'kind', 'latest_match_id', 'session_ids', 'players': [...]};
    degrades to {'live': False}."""
    out = {'live': False}
    # Squad members broadcasting on Twitch right now count as "live" even before
    # their first game lands — we assume a live stream means they're playing Halo.
    streaming = _live_streaming_gamertags()
    if df is None or df.empty or 'date' not in df.columns:
        return _stream_only_live(streaming) if streaming else out
    work = df.copy()
    ensure_datetime(work)
    work = work.dropna(subset=['date'])
    if work.empty:
        return _stream_only_live(streaming) if streaming else out
    now = pd.Timestamp.now(tz='UTC')
    latest = work['date'].max()
    mins_since = (now - latest).total_seconds() / 60.0
    if mins_since > LIVE_WINDOW_MINUTES:
        # No recent games — but if they're streaming, still go live.
        return _stream_only_live(streaming) if streaming else out

    # Current session = walk back from the latest match within the session gap.
    mt = work.groupby('match_id')['date'].max().sort_values(ascending=False)
    session_ids = latest_session_match_ids(mt)
    latest_match_id = mt.index[0] if len(mt.index) else ''
    sess = work[work['match_id'].isin(session_ids)].copy()
    ppm = sess.groupby('match_id')['player_gamertag'].nunique()
    squad_session_ids = set(ppm[ppm >= 2].index)
    counted_session_ids = squad_session_ids if squad_session_ids else session_ids
    counted_sess = sess[sess['match_id'].isin(counted_session_ids)].copy()
    mview = counted_sess.drop_duplicates('match_id')
    oc = mview['outcome'].astype(str).str.lower() if 'outcome' in mview.columns else pd.Series(dtype=str)
    wins, losses = int((oc == 'win').sum()), int((oc == 'loss').sum())
    kind = 'squad' if squad_session_ids else 'solo'

    players = []
    for p, g in counted_sess.groupby('player_gamertag'):
        g = g.sort_values('date')
        last_seen = g['date'].max()
        p_mins = (now - last_seen).total_seconds() / 60.0
        po = g['outcome'].astype(str).str.lower() if 'outcome' in g.columns else pd.Series(dtype=str)
        k = numeric_series(g, 'kills').sum()
        d = numeric_series(g, 'deaths').sum()
        a = numeric_series(g, 'assists').sum()
        gms = len(g)
        players.append({
            'player': p, 'css': get_player_class(p),
            'games': gms,
            'record': f"{int((po == 'win').sum())}-{int((po == 'loss').sum())}",
            'kda': format_float(safe_kda(k / gms, a / gms, d / gms) if gms else 0, 2),
            'is_live': p_mins <= LIVE_WINDOW_MINUTES,
            'mins_ago': int(round(p_mins)),
            'ago': _fmt_ago(p_mins),
        })
    # Fold in anyone streaming: mark matching session players live, and append
    # streamers who haven't logged a game yet this session.
    _merge_streaming_players(players, streaming)
    # Live players first, then by most recently seen.
    players.sort(key=lambda x: (not x['is_live'], x['mins_ago']))
    if not any(p['is_live'] for p in players):
        return _stream_only_live(streaming) if streaming else out
    return {
        'live': True,
        'minutes_ago': int(round(mins_since)),
        'ago': _fmt_ago(mins_since),
        'session_games': int(mview['match_id'].nunique()),
        'wins': wins, 'losses': losses,
        'kind': kind,
        'latest_match_id': latest_match_id,
        'session_ids': list(counted_session_ids),
        'players': players,
    }


# Gamertag → Twitch channel map for the /live stream embeds. Configure with
# HALO_TWITCH_CHANNELS (JSON object, e.g. '{"SomeGamertag": "twitch_login"}').
# Empty when unset — all Twitch live features simply stay off.


def _twitch_channel_map() -> dict:
    raw = os.getenv('HALO_TWITCH_CHANNELS', '')
    if raw.strip():
        try:
            m = json.loads(raw)
            if isinstance(m, dict):
                return {str(k): str(v) for k, v in m.items()}
        except ValueError:
            logger.warning('HALO_TWITCH_CHANNELS is not valid JSON — ignoring')
    return {}


_TWITCH_LIVE_CACHE: dict = {'ts': 0.0, 'data': {}}


def _twitch_live_map() -> dict:
    """Who's actually broadcasting on Twitch right now. Prefers asking Twitch
    Helix DIRECTLY (twitch_live.py, needs TWITCH_CLIENT_ID/SECRET) so the
    MultiTwitch app isn't needed for any live feature; falls back to
    MultiTwitch's /api/live proxy when creds aren't configured. Returns
    {login: {live, streamTitle, ...}}; {} when unreachable."""
    now = time.time()
    if now - _TWITCH_LIVE_CACHE['ts'] < TWITCH_LIVE_TTL_SECONDS:
        return _TWITCH_LIVE_CACHE['data']
    data = {}
    channels = _twitch_channel_map()
    if channels and twitch_live.creds_configured():
        data = twitch_live.fetch_live_map(list(channels.values()))
    else:
        # Optional fallback: a MultiTwitch instance's /api/live proxy.
        base = os.getenv('HALO_MULTITWITCH_API', '').rstrip('/')
        if base:
            try:
                r = requests.get(f"{base}/api/live", timeout=4)
                r.raise_for_status()
                j = r.json()
                if isinstance(j, dict):
                    data = j
            except Exception:
                data = {}
    _TWITCH_LIVE_CACHE['ts'] = now
    _TWITCH_LIVE_CACHE['data'] = data
    return data


def build_stream_embeds() -> list:
    """Squad channels currently broadcasting, for the /live embeds. Computed
    per request (NOT in the count-keyed page cache) so streams appear/disappear
    without waiting for a new game to land."""
    livemap = _twitch_live_map()
    if not livemap:
        return []
    streams = []
    for gt, ch in _twitch_channel_map().items():
        info = livemap.get(ch) or livemap.get(ch.lower()) or {}
        if info.get('live'):
            streams.append({
                'gamertag': gt,
                'channel': ch,
                'css': get_player_class(gt),
                'title': str(info.get('streamTitle') or ''),
                'game': str(info.get('streamGame') or ''),
                'viewers': info.get('viewers'),
            })
    streams.sort(key=lambda s: -(s['viewers'] or 0))
    return streams


def stream_key_for_streams(streams: list) -> str:
    """Stable identity for the live Twitch channel set."""
    return ','.join(sorted(str(s.get('channel', '')).lower() for s in streams if s.get('channel')))


def _live_streaming_gamertags() -> list:
    """Tracked gamertags whose Twitch channel is broadcasting right now (via the
    MultiTwitch Helix feed). Lets the site flip to 'live' the moment the squad
    starts streaming, on the assumption a live stream means they're playing Halo.
    Disable with HALO_LIVE_ON_STREAM=false."""
    if os.getenv('HALO_LIVE_ON_STREAM', 'true').strip().lower() in ('0', 'false', 'no', 'off'):
        return []
    livemap = _twitch_live_map()
    if not livemap:
        return []
    out = []
    for gt, ch in _twitch_channel_map().items():
        info = livemap.get(ch) or livemap.get(ch.lower()) or {}
        if info.get('live'):
            out.append(gt)
    return out


def _stream_only_player(gt: str) -> dict:
    """A live chip for a squad member who's streaming but has no game logged yet."""
    return {
        'player': gt, 'css': get_player_class(gt),
        'games': 0, 'record': '0-0', 'kda': format_float(0, 2),
        'is_live': True, 'mins_ago': 0, 'ago': 'streaming',
        'stream_only': True,
    }


def _stream_only_live(streaming: list) -> dict:
    """Live payload when the squad is streaming but no recent match has landed."""
    players = [_stream_only_player(gt) for gt in streaming]
    return {
        'live': True, 'minutes_ago': 0, 'ago': 'now',
        'session_games': 0, 'wins': 0, 'losses': 0,
        'kind': 'squad' if len(players) >= 2 else 'solo',
        'latest_match_id': '',
        'session_ids': [],
        'players': players, 'stream_only': True,
    }


def _merge_streaming_players(players: list, streaming: list) -> None:
    """Mark active-session players who are also streaming as live, and append any
    streamer who hasn't logged a game yet. Mutates `players` in place."""
    if not streaming:
        return
    have = {p['player'] for p in players}
    for gt in streaming:
        if gt in have:
            for p in players:
                if p['player'] == gt:
                    p['is_live'] = True
        else:
            players.append(_stream_only_player(gt))


def build_super_live(df):
    """The big live board: reuses the latest-session report card (all the rich
    per-player stats + game-by-game) and layers on who's actually online now.
    Auto-refreshes client-side so data updates as games land."""
    live = build_live_now(df)
    live_session_ids = live.get('session_ids') if isinstance(live.get('session_ids'), list) else []
    if live.get('live') and live_session_ids:
        card = build_squad_report_card(df, mode='latest', session_match_ids=live_session_ids) or {}
    else:
        card = build_squad_report_card(df, mode='latest') or {}
    rows = card.get('rows', [])
    live_players = live.get('players', []) if isinstance(live.get('players'), list) else []
    live_by = {p['player']: p for p in live_players}
    players = []
    for r in rows:
        lv = live_by.get(r['player'], {})
        gg = r.get('game_grades', [])
        best = max(gg, key=lambda g: g.get('score', 0)) if gg else None
        worst = min(gg, key=lambda g: g.get('score', 0)) if gg else None
        players.append({**r,
                        'is_live': bool(lv.get('is_live')),
                        'ago': lv.get('ago', ''),
                        'best_game': best, 'worst_game': worst})
    players.sort(key=lambda p: (not p['is_live'], -(p.get('composite_pct') or 0)))
    # Header record: live count while active, else derive from the card.
    if live.get('live'):
        w, l = int(live.get('wins', 0)), int(live.get('losses', 0))
    elif rows:
        w = int(rows[0].get('wins', 0)); l = max(int(rows[0].get('games', 0)) - w, 0)
    else:
        w = l = 0
    live_game_count = int(live.get('session_games') or 0) if live.get('live') else 0
    live_count = sum(1 for p in live_players if p.get('is_live')) if live.get('live') else 0
    return {
        'has_data': bool(rows),
        'live': bool(live.get('live')),
        'ago': live.get('ago', ''),
        'live_count': live_count,
        'session_date': card.get('session_date', ''),
        'game_count': live_game_count if live_game_count else card.get('game_count', 0),
        'wins': w, 'losses': l,
        'win_pct': format_pct(w / (w + l)) if (w + l) else '0%',
        'kind': live.get('kind') if live.get('live') else card.get('kind', 'squad'),
        'players': players,
        'obj_columns': card.get('obj_columns', []),
        'session_rank': card.get('session_rank'),
        'stack_records': card.get('stack_records', []),
        'sid': card.get('sid') or live.get('latest_match_id', ''),
    }


@app.route('/live')
def live():
    df = cache.get()
    try:
        # Live is time-sensitive and auto-refreshes. Build it from the current
        # cache frame every request so newly landed games show before the broader
        # page payload cache expires.
        payload = build_super_live(df)
    except Exception as exc:
        logger.warning('live page failed: %s', exc)
        import traceback; logger.warning(traceback.format_exc())
        payload = {'has_data': False, 'live': False, 'players': []}
    try:
        # Fresh per request (short TTL inside) — not tied to the new-game cache.
        payload['streams'] = build_stream_embeds()
    except Exception:
        payload['streams'] = []
    payload['stream_key'] = stream_key_for_streams(payload.get('streams') or [])
    payload['stream_poll_seconds'] = LIVE_STREAM_POLL_SECONDS
    status = load_status()
    return render_template('livesession.html', app_title=APP_TITLE, live=payload,
                           last_update=status.get('last_update'),
                           db_row_count=count_cache.get())


@app.route('/overlay')
def overlay():
    """Transparent stream/OBS browser-source: compact live session board (record,
    win%, per-player KDA). Empty (invisible) when the squad isn't live so it can
    sit in a scene permanently. ?always=1 shows the last session even when idle."""
    df = cache.get()
    always = request.args.get('always') in ('1', 'true', 'yes')
    try:
        live = build_live_now(df)
    except Exception as exc:
        logger.warning('overlay live failed: %s', exc)
        live = {'live': False}
    show = bool(live.get('live'))
    # Preview mode (?always=1): if not currently live, fall back to the last
    # session so the overlay has something to render.
    if not show and always:
        try:
            sl = build_super_live(df) or {}
        except Exception:
            sl = {}
        players = []
        for p in (sl.get('players') or [])[:5]:
            pw, pg = int(p.get('wins', 0) or 0), int(p.get('games', 0) or 0)
            players.append({'player': p.get('player', ''), 'css': get_player_class(p.get('player', '')),
                            'is_live': False, 'kda': p.get('kda'),
                            'record': f"{pw}-{max(pg - pw, 0)}"})
        live = {'live': False, 'wins': sl.get('wins', 0), 'losses': sl.get('losses', 0),
                'session_games': sl.get('game_count', 0), 'kind': sl.get('kind', 'squad'),
                'players': players}
        show = bool(players)
    return render_template('overlay.html', app_title=APP_TITLE, live=live, show=show)


@app.route('/api/at')
def api_at():
    """What the squad was playing at a given wall-clock instant.

    Powers the MultiTwitch rewatch stats rail: ?ts=<unix seconds|ms> → the
    match covering that moment (or the one just before it, flagged 'between'),
    with a compact per-player scoreboard, the game number + running record
    within that session, and the next match's start for smart polling.
    MultiTwitch proxies this server-side, so no CORS/auth concerns."""
    try:
        ts = float(request.args.get('ts', ''))
    except (TypeError, ValueError):
        return jsonify({'error': 'ts required (unix seconds)'}), 400
    if ts > 1e12:  # milliseconds
        ts /= 1000.0
    try:
        when = pd.Timestamp(ts, unit='s', tz='UTC')
    except (OverflowError, ValueError, pd.errors.OutOfBoundsDatetime):
        return jsonify({'error': 'ts out of range'}), 400

    df = cache.get()
    if df is None or df.empty or 'date' not in df.columns:
        return jsonify({'found': False})
    work = df.dropna(subset=['date'])
    if work.empty:
        return jsonify({'found': False})

    # One row per match: start time + duration (seconds), oldest → newest.
    marks = (work.groupby('match_id')
                 .agg(start=('date', 'max'), dur=('duration', 'max'))
                 .sort_values('start'))
    started = marks[marks['start'] <= when]
    if started.empty:
        nxt = marks[marks['start'] > when]
        return jsonify({'found': False,
                        'next_start': int(nxt['start'].iloc[0].timestamp()) if not nxt.empty else None})
    mid = started.index[-1]
    row0 = started.iloc[-1]
    start_ts = int(row0['start'].timestamp())
    dur = safe_float(row0['dur']) or 0
    end_ts = start_ts + int(dur)
    # 'between' = the instant falls after this match ended (loading/lobby time).
    between = ts > end_ts + 90
    nxt = marks[marks['start'] > when]
    next_start = int(nxt['start'].iloc[0].timestamp()) if not nxt.empty else None
    # Stale guard: if the nearest match ended >2h before ts, there's no game.
    if between and (ts - end_ts) > 2 * 3600:
        return jsonify({'found': False, 'next_start': next_start})

    # Game number + running record within this match's gap-cluster session.
    starts = marks['start']
    idx = list(marks.index).index(mid)
    first_i = idx
    while first_i > 0 and (starts.iloc[first_i] - starts.iloc[first_i - 1]).total_seconds() / 60 <= SESSION_GAP_MINUTES:
        first_i -= 1
    session_mids = list(marks.index[first_i:idx + 1])
    sess_view = work[work['match_id'].isin(session_mids)].drop_duplicates('match_id')
    oc = sess_view['outcome'].astype(str).str.lower()
    wins, losses = int((oc == 'win').sum()), int((oc == 'loss').sum())

    g = work[work['match_id'] == mid]
    players = []
    for _, r in g.iterrows():
        gr = _match_grade_for_row(r)
        players.append({
            'player': str(r.get('player_gamertag', '')),
            'css': get_player_class(str(r.get('player_gamertag', ''))),
            'kills': int(safe_float(r.get('kills', 0))),
            'deaths': int(safe_float(r.get('deaths', 0))),
            'assists': int(safe_float(r.get('assists', 0))),
            'kda': format_float(safe_kda(safe_float(r.get('kills', 0)),
                                         safe_float(r.get('assists', 0)),
                                         safe_float(r.get('deaths', 0))), 2),
            'grade': gr.get('grade', ''),
            'grade_class': gr.get('grade_class', ''),
        })
    players.sort(key=lambda p: -safe_float(p['kda']))
    r0 = g.iloc[0]
    outcome = str(r0.get('outcome', '')).title()
    return jsonify({
        'found': True,
        'between': between,
        'match': {
            'match_id': mid,
            'map': normalize_map_name(str(r0.get('map', ''))),
            'mode': clean_mode(r0.get('game_type', '')),
            'result': outcome,
            'result_class': outcome_class(r0.get('outcome', '')),
            'start': start_ts,
            'end': end_ts,
            'game_num': len(session_mids),
            'session_record': f"{wins}-{losses}",
            'players': players,
        },
        'next_start': next_start,
    })


@app.route('/sessions')
def sessions_list():
    df = cache.get()
    try:
        sessions = get_cached_page_payload('sessions', lambda: build_squad_sessions(df))
    except Exception as exc:
        logger.warning('sessions page failed: %s', exc)
        sessions = []
    # Chart data oldest→newest so the trend reads left-to-right.
    chrono = list(reversed(sessions))
    chart = {
        'labels': [s['date'] for s in chrono],
        'sids': [s['sid'] for s in chrono],
        'kda': [s['avg_kda_num'] for s in chrono],
        'win_pct': [s['win_pct'] for s in chrono],
    }
    status = load_status()
    return render_template('sessions.html', app_title=APP_TITLE,
                           sessions=sessions, chart=chart,
                           last_update=status.get('last_update'),
                           db_row_count=count_cache.get())


@app.route('/session/<sid>')
def session_detail(sid):
    df = cache.get()
    try:
        detail = build_session_detail(df, sid)
    except Exception as exc:
        logger.warning('session detail failed: %s', exc)
        detail = None
    status = load_status()
    return render_template('session_detail.html', app_title=APP_TITLE,
                           detail=detail,
                           last_update=status.get('last_update'),
                           db_row_count=count_cache.get())


# --- Time-of-day / weekday heatmap ---------------------------------------

def build_heatmap_compare(df):
    """Per-player performance (KDA) across time-of-day and session-position
    buckets so you can compare WHO plays best WHEN. Tracked players share a team
    (and thus win/loss), so win% by time is squad-level — KDA is what differs by
    player, which is the useful comparison here. Each player's best window is
    flagged; cells need a minimum sample before they show."""
    out = {'time_cols': [], 'time_players': [], 'pos_cols': [], 'pos_players': [], 'has_data': False}
    if df is None or df.empty or 'date_local' not in df.columns or 'player_gamertag' not in df.columns:
        return out
    work = df.copy()
    ensure_datetime(work, 'date_local')
    work = work.dropna(subset=['date_local'])
    if work.empty:
        return out
    work['hour'] = work['date_local'].dt.hour
    work['kda_num'] = work.apply(
        lambda r: safe_kda(r.get('kills', 0), r.get('assists', 0), r.get('deaths', 0)), axis=1)
    players = unique_sorted(work['player_gamertag'])
    if not players:
        return out

    MIN_GAMES = 4  # require a real sample before a cell earns a number

    # Session position is a match-level attribute (game N of the night), so
    # compute it on unique matches then map back onto every player's row.
    matches = work.drop_duplicates('match_id').sort_values('date_local')
    gap = matches['date_local'].diff() > pd.Timedelta(minutes=SESSION_GAP_MINUTES)
    matches = matches.assign(session=gap.cumsum())
    matches = matches.assign(pos=matches.groupby('session').cumcount() + 1)
    work['pos'] = work['match_id'].map(dict(zip(matches['match_id'], matches['pos']))).fillna(1)

    def _make(bins, attr):
        cols = [b[2] for b in bins]
        raw = {}
        cell_vals = []
        for p in players:
            pdf = work[work['player_gamertag'] == p]
            cells = []
            for lo, hi, _label in bins:
                sub = pdf[(pdf[attr] >= lo) & (pdf[attr] < hi)]
                if len(sub) >= MIN_GAMES:
                    kda = float(sub['kda_num'].mean())
                    oc = sub['outcome'].astype(str).str.lower() if 'outcome' in sub.columns else pd.Series(dtype=str)
                    dec = int(oc.isin(['win', 'loss']).sum())
                    wins = int((oc == 'win').sum())
                    win_pct = (wins / dec * 100) if dec else None
                    cells.append({'kda': kda, 'games': len(sub), 'has': True, 'win_pct': win_pct})
                    cell_vals.append(kda)
                else:
                    cells.append({'kda': None, 'games': len(sub), 'has': False})
            # Window grade: rank this player's OWN windows against each other so
            # the grades actually differ across the timeline (their best window
            # vs their worst), rather than collapsing to one overall letter.
            graded = [c for c in cells if c['has']]
            add_composite_grades(graded, {'kda': True}, 'Window grade (vs this player’s other windows)')
            raw[p] = cells
        lo_v = min(cell_vals) if cell_vals else 0.0
        hi_v = max(cell_vals) if cell_vals else 1.0
        span = (hi_v - lo_v) or 1.0
        rows = []
        for p in players:
            cells = raw[p]
            best_idx, best_kda = None, None
            for i, c in enumerate(cells):
                if c['has'] and (best_kda is None or c['kda'] > best_kda):
                    best_kda, best_idx = c['kda'], i
            out_cells = []
            for i, c in enumerate(cells):
                if c['has']:
                    out_cells.append({
                        'val': format_float(c['kda'], 2), 'kda_num': round(c['kda'], 2), 'games': c['games'],
                        'win_pct': (f"{c['win_pct']:.0f}%" if c.get('win_pct') is not None else '–'),
                        'grade': c.get('grade', '—'), 'grade_class': c.get('grade_class', ''),
                        'norm': round((c['kda'] - lo_v) / span, 3),
                        'best': (i == best_idx), 'has': True,
                    })
                else:
                    out_cells.append({'val': '–', 'kda_num': None, 'games': c['games'], 'norm': 0,
                                      'best': False, 'has': False, 'win_pct': '–', 'grade': '—', 'grade_class': ''})
            rows.append({'player': p, 'css': get_player_class(p),
                         'color': COMPARE_BAR_COLORS.get(get_player_class(p), '#3dbfb8'),
                         'cells': out_cells})
        return cols, rows

    def _chart(cols, player_rows):
        """Line-chart payload: x = buckets, one KDA series per player."""
        return {
            'labels': cols,
            'series': [{
                'name': r['player'],
                'color': r['color'],
                'data': [(c['kda_num'] if c.get('has') else None) for c in r['cells']],
            } for r in player_rows],
        }

    time_bins = [(0, 6, 'Late 12–6a'), (6, 12, 'Morning'), (12, 17, 'Afternoon'),
                 (17, 21, 'Evening'), (21, 24, 'Night')]
    pos_bins = [(1, 3, 'Games 1–3'), (4, 6, 'Games 4–6'), (7, 10, 'Games 7–10'), (11, 999, 'Games 11+')]
    time_cols, time_players = _make(time_bins, 'hour')
    pos_cols, pos_players = _make(pos_bins, 'pos')
    return {'time_cols': time_cols, 'time_players': time_players,
            'pos_cols': pos_cols, 'pos_players': pos_players,
            'pos_chart': _chart(pos_cols, pos_players),
            'time_chart': _chart(time_cols, time_players),
            'has_data': True}


def build_time_heatmap(df):
    if df.empty or 'date_local' not in df.columns:
        return {'grid': [], 'hours': [], 'by_position': []}
    work = _dedup_matches(df).copy()
    ensure_datetime(work, 'date_local')
    work = work.dropna(subset=['date_local'])
    if work.empty:
        return {'grid': [], 'hours': [], 'by_position': []}
    work['dow'] = work['date_local'].dt.dayofweek
    work['hour'] = work['date_local'].dt.hour
    work['is_win'] = work['outcome'].astype(str).str.lower() == 'win'
    work['is_dec'] = work['outcome'].astype(str).str.lower().isin(['win', 'loss'])

    # Hour buckets (3-hour bins) × weekday grid.
    bins = [(0, 6, 'Late (12-6a)'), (6, 12, 'Morning'), (12, 17, 'Afternoon'),
            (17, 21, 'Evening'), (21, 24, 'Night')]
    days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    grid = []
    for di, day in enumerate(days):
        cells = []
        for lo, hi, _ in bins:
            sub = work[(work['dow'] == di) & (work['hour'] >= lo) & (work['hour'] < hi)]
            dec = sub[sub['is_dec']]
            wr = (sub['is_win'].sum() / len(dec) * 100) if len(dec) else None
            cells.append({
                'games': len(sub),
                'win_pct': f'{wr:.0f}%' if wr is not None else '–',
                'kda': format_float(pd.to_numeric(sub.get('kda'), errors='coerce').mean(), 2) if len(sub) else '–',
                'win_val': wr,
            })
        grid.append({'day': day, 'cells': cells})
    # Colour each cell RELATIVE to the actual spread of win% across the grid
    # (squad win rates cluster ~45-65%, so grading vs [0,100] made everything
    # "average"/amber — relative grading restores the red→green gradient).
    all_wr = [c['win_val'] for row in grid for c in row['cells'] if c['win_val'] is not None]
    for row in grid:
        for c in row['cells']:
            c['heat'] = get_heatmap_class(c['win_val'], all_wr, True) if c['win_val'] is not None else ''
            c['win_val'] = c['win_val'] if c['win_val'] is not None else 0

    # Performance by position-in-session (does the squad fall off late?).
    work2 = work.sort_values('date_local')
    gap = work2['date_local'].diff() > pd.Timedelta(minutes=SESSION_GAP_MINUTES)
    work2['session'] = gap.cumsum()
    work2['pos'] = work2.groupby('session').cumcount() + 1
    by_position = []
    pos_buckets = [(1, 3, 'Games 1-3'), (4, 6, 'Games 4-6'),
                   (7, 10, 'Games 7-10'), (11, 999, 'Games 11+')]
    for lo, hi, label in pos_buckets:
        sub = work2[(work2['pos'] >= lo) & (work2['pos'] <= hi)]
        dec = sub[sub['is_dec']]
        wr = (sub['is_win'].sum() / len(dec) * 100) if len(dec) else None
        by_position.append({
            'label': label,
            'games': len(sub),
            'win_pct': f'{wr:.0f}%' if wr is not None else '–',
            'kda': format_float(pd.to_numeric(sub.get('kda'), errors='coerce').mean(), 2) if len(sub) else '–',
            'win_val': wr if wr is not None else 0,
        })
    add_composite_grades(by_position, {
        'win_val': True, 'kda': True,
    }, 'Session-position grade')
    return {'grid': grid, 'hours': [b[2] for b in bins], 'by_position': by_position}


WIN_DNA_RANGES = {'30': 30, '90': 90, '180': 180, '365': 365, '2y': 730, 'lifetime': None}


def build_win_dna(df: pd.DataFrame, stack_size: int = 4, range_key: str = '365') -> dict:
    """Winning-formula analysis for full-stack games: when the whole stack queues
    together, how do each player's stats differ in WINS vs LOSSES? Surfaces who
    drives wins (steps up), who backseats (team wins without leaning on them),
    and how tied each player's game is to the result."""
    import numpy as np
    empty = {'has_data': False, 'stack_size': stack_size, 'range_key': range_key}
    if df is None or df.empty or not {'match_id', 'player_gamertag', 'outcome'} <= set(df.columns):
        return empty
    ranked = _ranked_only(df)
    if ranked.empty:
        return empty
    # Time-range filter (default last 365 days).
    days = WIN_DNA_RANGES.get(range_key, 365)
    if days and 'date' in ranked.columns:
        ensure_datetime(ranked)
        cutoff = pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=days)
        ranked = ranked[ranked['date'] >= cutoff]
        if ranked.empty:
            return empty
    ppm = ranked.groupby('match_id')['player_gamertag'].nunique()
    stack = ranked[ranked['match_id'].isin(set(ppm[ppm == stack_size].index))].copy()
    if stack.empty:
        return empty
    o = stack['outcome'].astype(str).str.lower()
    stack = stack[o.isin(['win', 'loss', 'lose', 'loose'])].copy()
    if stack.empty:
        return empty
    stack['_win'] = stack['outcome'].astype(str).str.lower().eq('win')

    def kda_series(f):
        return numeric_series(f, 'kills') + numeric_series(f, 'assists') / 3 - numeric_series(f, 'deaths')

    def dmgdiff(f):
        return numeric_series(f, 'damage_dealt') - numeric_series(f, 'damage_taken')

    match_win = stack.groupby('match_id')['_win'].first()
    games = int(len(match_win)); wins = int(match_win.sum()); losses = games - wins
    if games < 3 or wins == 0 or losses == 0:
        return {**empty, 'games': games, 'wins': wins, 'losses': losses, 'thin': True}

    win_rows = stack[stack['_win']]; loss_rows = stack[~stack['_win']]

    def pair(fn, fmt, higher_better=True):
        w = float(fn(win_rows).mean()) if len(win_rows) else 0.0
        l = float(fn(loss_rows).mean()) if len(loss_rows) else 0.0
        good = (w >= l) if higher_better else (w <= l)
        return {'win': format_float(w, fmt), 'loss': format_float(l, fmt),
                'win_raw': round(w, 3), 'loss_raw': round(l, 3),
                'delta': format_signed(w - l, fmt), 'up': w >= l, 'good': good}

    team = {
        'kda': {'label': 'KDA', **pair(kda_series, 2)},
        'kills': {'label': 'Kills', **pair(lambda f: numeric_series(f, 'kills'), 1)},
        'deaths': {'label': 'Deaths', **pair(lambda f: numeric_series(f, 'deaths'), 1, higher_better=False)},
        'assists': {'label': 'Assists', **pair(lambda f: numeric_series(f, 'assists'), 1)},
        'dmg_diff': {'label': 'Dmg ±', **pair(dmgdiff, 0)},
        'obj': {'label': 'Obj Score', **pair(objective_score_series, 0)},
    }

    def share(f):
        ps = score_series(f)
        ts = pd.to_numeric(f.get('team_personal_score', 0), errors='coerce').fillna(0)
        s = (ps / ts.where(ts > 0) * 100).replace([float('inf'), float('-inf')], pd.NA).dropna()
        return float(s.mean()) if not s.empty else 0.0

    prows = []
    for p in unique_sorted(stack['player_gamertag']):
        pf = stack[stack['player_gamertag'] == p]
        pw = pf[pf['_win']]; pl = pf[~pf['_win']]
        if len(pw) == 0 or len(pl) == 0:
            continue
        kw = float(kda_series(pw).mean()); kl = float(kda_series(pl).mean())
        delta = kw - kl
        ks = kda_series(pf).values.astype(float); ys = pf['_win'].astype(float).values
        corr = 0.0
        if len(ks) >= 4 and ks.std() > 1e-9 and ys.std() > 1e-9:
            corr = float(np.corrcoef(ks, ys)[0, 1])
        sw, sl = share(pw), share(pl)
        _fired = numeric_series(pf, 'shots_fired').sum(); _hit = numeric_series(pf, 'shots_hit').sum()
        prows.append({
            'player': p, 'css': get_player_class(p),
            'win_games': int(len(pw)), 'loss_games': int(len(pl)),
            'kda_win': format_float(kw, 2), 'kda_loss': format_float(kl, 2),
            'kda_win_raw': round(kw, 3), 'kda_loss_raw': round(kl, 3),
            'delta': format_signed(delta, 2), 'delta_raw': round(delta, 3),
            'corr': round(corr, 2), 'corr_raw': round(corr, 3),
            'dmg_win': format_signed(float(dmgdiff(pw).mean()), 0),
            'dmg_loss': format_signed(float(dmgdiff(pl).mean()), 0),
            'dmg_win_raw': round(float(dmgdiff(pw).mean()), 1),
            'share_win': format_float(sw, 0), 'share_loss': format_float(sl, 0),
            'share_win_raw': round(sw, 1), 'share_loss_raw': round(sl, 1),
            'share_delta': format_signed(sw - sl, 0), 'share_delta_raw': round(sw - sl, 2),
            'deaths_win': round(float(numeric_series(pw, 'deaths').mean()), 1),
            'kills_win': round(float(numeric_series(pw, 'kills').mean()), 1),
            'acc': round(float(_hit / _fired * 100), 1) if _fired > 0 else 0.0,
        })
    if not prows:
        return {**empty, 'games': games, 'wins': wins, 'losses': losses, 'thin': True}

    # Everyone plays better in wins, so absolute KDA lift barely differentiates.
    # Classify RELATIVE to the squad: who lifts MORE than teammates (drives wins)
    # vs who lifts LEAST (gets carried / takes a backseat). Use a z-score of the
    # win-lift across players.
    _deltas = np.array([r['delta_raw'] for r in prows], dtype=float)
    _mean = float(_deltas.mean()); _std = float(_deltas.std()) or 1.0
    for r in prows:
        z = (r['delta_raw'] - _mean) / _std
        r['lift_z'] = round(z, 2)
        if z >= 0.55:
            r.update(role='Win driver', role_tone='role-driver', role_emoji='🔥')
        elif z <= -0.55:
            r.update(role='Backseat', role_tone='role-back', role_emoji='😴')
        else:
            r.update(role='Steady', role_tone='role-steady', role_emoji='➖')
    prows.sort(key=lambda r: r['delta_raw'], reverse=True)
    add_heatmap_classes(prows, {'kda_win_raw': True, 'delta_raw': True, 'corr_raw': True})

    top = prows[0]; bot = prows[-1]
    hi = max(prows, key=lambda r: abs(r['corr_raw']))
    riser = max(prows, key=lambda r: r['share_delta_raw'])
    insights = [
        f"🔥 <strong>{top['player']}</strong> lifts most in wins — {top['kda_loss']} → {top['kda_win']} KDA ({top['delta']}), the biggest jump on the squad.",
        f"😴 <strong>{bot['player']}</strong> lifts least ({bot['kda_loss']} → {bot['kda_win']} KDA, {bot['delta']}) — wins don't hinge on them popping off.",
        f"📊 <strong>{hi['player']}</strong>'s game is most tied to the result (correlation {hi['corr']:+.2f} between their KDA and winning).",
    ]
    if riser['share_delta_raw'] > 1:
        insights.append(f"📈 <strong>{riser['player']}</strong> takes over the scoreboard in wins — {riser['share_loss']}% → {riser['share_win']}% of team score.")
    insights.append(f"🏆 A winning {stack_size}-stack averages <strong>{team['kda']['win']}</strong> KDA/player vs {team['kda']['loss']} in losses · Dmg± {team['dmg_diff']['win']} vs {team['dmg_diff']['loss']}.")

    # ── Breakdowns by mode + map, oriented around WINNING ──
    # Rows ranked by squad WIN RATE (which modes/maps we actually win). Player
    # cells show WIN-LIFT (their KDA in wins minus losses in that mode) — the
    # honest per-player win signal, and it cancels the mode's KDA scale (objective
    # modes aren't "weak" just because slaying is low there).
    player_names = [r['player'] for r in prows]

    def breakdown(catfn, min_games=4, min_pg=3):
        tmp = stack.copy()
        tmp['_cat'] = catfn(tmp)
        cats = []
        for cat, cf in tmp.groupby('_cat'):
            cs = str(cat).strip()
            if not cs or cs.lower() in ('nan', 'none', 'unknown'):
                continue
            mwin = cf.groupby('match_id')['_win'].first()
            g = int(len(mwin))
            if g < min_games:
                continue
            w = int(mwin.sum())
            pj = {}
            for p in player_names:
                pcf = cf[cf['player_gamertag'] == p]
                if len(pcf) < min_pg:
                    continue
                pw = pcf[pcf['_win']]; pl = pcf[~pcf['_win']]
                cell = {'kda': round(float(kda_series(pcf).mean()), 2), 'games': int(len(pcf))}
                if len(pw) >= 2 and len(pl) >= 2:
                    cell['lift'] = round(float(kda_series(pw).mean()) - float(kda_series(pl).mean()), 2)
                pj[p] = cell
            cats.append({'name': cs, 'games': g, 'wins': w, 'losses': g - w,
                         'win_pct': format_float(w / g * 100, 0), 'win_pct_raw': round(w / g * 100),
                         'players': pj})
        cats.sort(key=lambda c: (c['win_pct_raw'], c['games']), reverse=True)
        for c in cats:  # heat each row by win-lift across players (who steps up when we win here)
            lifts = [c['players'][p]['lift'] for p in c['players'] if 'lift' in c['players'][p]]
            for p in c['players']:
                cell = c['players'][p]
                cell['heat'] = get_heatmap_class(cell.get('lift'), lifts, True) if ('lift' in cell and len(lifts) >= 2) else ''
        return cats

    by_mode = breakdown(lambda f: f['game_type'].map(clean_mode)) if 'game_type' in stack.columns else []
    by_map = breakdown(lambda f: f['map'].map(normalize_map_name)) if 'map' in stack.columns else []

    # Squad-level winning read (this is the win-tied headline)
    if by_mode:
        bmo, wmo = by_mode[0], by_mode[-1]
        if bmo['win_pct_raw'] != wmo['win_pct_raw']:
            insights.append(f"🎮 We win most in <strong>{bmo['name']}</strong> ({bmo['win_pct']}%, {bmo['wins']}-{bmo['losses']}) and struggle most in <strong>{wmo['name']}</strong> ({wmo['win_pct']}%, {wmo['wins']}-{wmo['losses']}).")
    if by_map:
        bma, wma = by_map[0], by_map[-1]
        if bma['win_pct_raw'] != wma['win_pct_raw']:
            insights.append(f"🗺️ Strongest map: <strong>{bma['name']}</strong> ({bma['win_pct']}% win) · roughest: <strong>{wma['name']}</strong> ({wma['win_pct']}%).")

    def _lifts(cats, p):
        return [(c['name'], c['players'][p]['lift'], c['players'][p]['games'])
                for c in cats if p in c['players'] and 'lift' in c['players'][p]]

    _corr_max = max(prows, key=lambda x: x['corr_raw'])['player']
    _corr_min = min(prows, key=lambda x: x['corr_raw'])['player']
    _n = len(prows)
    _kda_win_max = max(r['kda_win_raw'] for r in prows)
    _kda_loss_min = min(r['kda_loss_raw'] for r in prows)
    _deaths_avg = sum(r['deaths_win'] for r in prows) / _n
    _dmg_avg = sum(r['dmg_win_raw'] for r in prows) / _n
    _share_avg = sum(r['share_win_raw'] for r in prows) / _n
    _acc_vals = [r['acc'] for r in prows if r['acc']]
    _acc_avg = sum(_acc_vals) / len(_acc_vals) if _acc_vals else 0.0
    # z-score of each player on each dimension → their most DISTINCTIVE trait,
    # so every player's card gets an individual read (not a shared blurb).
    _z = {}
    for _zk in ('kda_win_raw', 'deaths_win', 'dmg_win_raw', 'share_win_raw', 'acc', 'kda_loss_raw'):
        _vv = np.array([r[_zk] for r in prows], dtype=float)
        _m = float(_vv.mean()); _s = float(_vv.std()) or 1.0
        _z[_zk] = {r['player']: (r[_zk] - _m) / _s for r in prows}
    _TRAIT = {
        ('kda_win_raw', True): "🔫 <strong>Top-tier slayer in wins</strong> ({d}) — keep hunting; the squad plays off your frags.",
        ('kda_win_raw', False): "🔫 <strong>Your slaying trails in wins</strong> ({d}) — get into more fights and win your duels.",
        ('deaths_win', True): "🧼 <strong>You barely die, even in wins</strong> ({d}) — you're the anchor; keep enabling the aggressors.",
        ('deaths_win', False): "💀 <strong>You die the most in our wins</strong> ({d}) — trade smarter, stop over-peeking; free deaths cost rounds.",
        ('dmg_win_raw', True): "💥 <strong>Damage machine in wins</strong> ({d} net) — your pressure cracks rounds open.",
        ('dmg_win_raw', False): "💥 <strong>Your damage lags in wins</strong> ({d} net) — break more shields before you die.",
        ('share_win_raw', True): "📈 <strong>You carry the scoreboard in wins</strong> ({d}% of team score) — that's your identity, lean in.",
        ('share_win_raw', False): "📉 <strong>You add least to the score in wins</strong> ({d}%) — get more active on kills / objective.",
        ('acc', True): "🎯 <strong>Sharpest aim on the squad</strong> ({d}%) — win your gunfights and the rest follows.",
        ('acc', False): "🎯 <strong>Your accuracy trails the squad</strong> ({d}%) — aim reps are the cheapest KDA you can buy.",
        ('kda_loss_raw', True): "⚓ <strong>Highest floor on the squad</strong> ({d} KDA even in losses) — rock-solid, you rarely have a throwaway game.",
        ('kda_loss_raw', False): "⚓ <strong>Lowest floor on the squad</strong> ({d} KDA in losses) — raising your worst games matters more than your ceiling.",
    }
    _DISP = {'kda_win_raw': 'kda_win', 'deaths_win': 'deaths_win', 'dmg_win_raw': 'dmg_win',
             'share_win_raw': 'share_win', 'acc': 'acc', 'kda_loss_raw': 'kda_loss'}
    scouting = []
    for r in prows:
        p = r['player']; notes = []
        ml = _lifts(by_mode, p)
        if ml:
            bm = max(ml, key=lambda x: x[1]); wm = min(ml, key=lambda x: x[1])
            notes.append(f"🔥 Shows up in our <strong>{bm[0]}</strong> wins — {'+' if bm[1] >= 0 else ''}{bm[1]} KDA lift over {bm[2]} games.")
            if len(ml) > 1 and wm[0] != bm[0]:
                notes.append(f"😴 Flat in <strong>{wm[0]}</strong> ({'+' if wm[1] >= 0 else ''}{wm[1]} lift) — wins there don't ride on them.")
        xl = _lifts(by_map, p)
        if xl:
            bx = max(xl, key=lambda x: x[1])
            notes.append(f"🗺️ Biggest map impact: <strong>{bx[0]}</strong> ({'+' if bx[1] >= 0 else ''}{bx[1]} KDA lift in wins).")
        notes.append(f"{r['role_emoji']} Overall: {r['kda_loss']} → {r['kda_win']} KDA in wins ({r['delta']}) · r={r['corr']:+.2f} vs winning — {r['role']}.")

        # ── INDIVIDUAL coaching, from this player's own profile ──
        focus = []
        # (a) The role/lean line — only for the two extremes, so it's not shared.
        if p == _corr_max:
            focus.append(f"🎯 More than anyone, our wins track your game (r&nbsp;{r['corr']:+.2f}) — you're the swing factor. Your <strong>consistency</strong> is worth more than anyone's ceiling.")
        elif p == _corr_min and r['corr_raw'] < 0.15:
            focus.append(f"🧭 We win even when you're quiet (r&nbsp;{r['corr']:+.2f}) — you're <strong>free to anchor a role</strong> (objective / support) and take smart risks.")
        # (b) Their single MOST distinctive stat vs the squad (always individual).
        _cands = [(abs(_z[k][p]), k, _z[k][p] > 0) for k in _z]
        _cands.sort(reverse=True)
        for _mag, _k, _pos in _cands:
            if _mag < 0.55:
                break
            _hib = _k != 'deaths_win'  # deaths: lower is better
            _msg = _TRAIT.get((_k, _pos if _hib else not _pos))
            _line = _msg.format(d=r[_DISP[_k]]) if _msg else None
            if _line and _line not in focus:
                focus.append(_line)
                break
        # (c) A concrete focus area — their weakest mode (or worst map).
        if ml:
            wmf = min(ml, key=lambda x: x[1])
            if wmf[1] < 0.6:
                focus.append(f"📉 <strong>Rep {wmf[0]}</strong> — your game doesn't lift there in wins ({'+' if wmf[1] >= 0 else ''}{wmf[1]} KDA). Review those losses.")
        if len(focus) < 2 and xl:
            wxf = min(xl, key=lambda x: x[1])
            focus.append(f"🧭 <strong>Toughest map: {wxf[0]}</strong> ({'+' if wxf[1] >= 0 else ''}{wxf[1]} impact) — rethink your setup / spawns there.")
        if not focus:
            focus.append("🧩 Steady all-rounder — no glaring hole; keep sharpening the fundamentals.")

        scouting.append({'player': p, 'css': r['css'], 'role': r['role'], 'role_tone': r['role_tone'],
                         'notes': notes, 'focus': focus})

    return {
        'has_data': True, 'stack_size': stack_size, 'range_key': range_key,
        'games': games, 'wins': wins, 'losses': losses,
        'win_pct': format_float(wins / games * 100, 0),
        'team': team, 'players': prows, 'insights': insights,
        'player_names': player_names, 'by_mode': by_mode, 'by_map': by_map, 'scouting': scouting,
    }


@app.route('/winning')
def winning():
    df = cache.get()
    try:
        size = int(request.args.get('stack', 4))
    except (TypeError, ValueError):
        size = 4
    size = size if size in (2, 3, 4) else 4
    rng = request.args.get('range', '365')
    if rng not in WIN_DNA_RANGES:
        rng = '365'
    try:
        payload = get_cached_page_payload(f'win_dna_{size}_{rng}', lambda: build_win_dna(df, size, rng))
    except Exception as exc:
        logger.warning('winning page failed: %s', exc)
        payload = {'has_data': False, 'stack_size': size, 'range_key': rng}
    status = load_status()
    return render_template('winning.html', app_title=APP_TITLE, dna=payload,
                           stack_size=size, range_key=rng,
                           range_labels={'30': '30d', '90': '90d', '180': '180d', '365': '1y', '2y': '2y', 'lifetime': 'All'},
                           last_update=status.get('last_update'),
                           db_row_count=count_cache.get())


@app.route('/heatmap')
def heatmap():
    df = cache.get()
    try:
        payload = get_cached_page_payload('heatmap', lambda: build_time_heatmap(df))
    except Exception as exc:
        logger.warning('heatmap page failed: %s', exc)
        payload = {'grid': [], 'hours': [], 'by_position': []}
    try:
        compare = get_cached_page_payload('heatmap_compare', lambda: build_heatmap_compare(df))
    except Exception as exc:
        logger.warning('heatmap compare failed: %s', exc)
        compare = {'time_cols': [], 'time_players': [], 'pos_cols': [], 'pos_players': [], 'has_data': False}
    status = load_status()
    return render_template('heatmap.html', app_title=APP_TITLE,
                           grid=payload['grid'], hours=payload['hours'],
                           by_position=payload['by_position'],
                           compare=compare,
                           last_update=status.get('last_update'),
                           db_row_count=count_cache.get())


# --- Map veto cheat-sheet -------------------------------------------------

def build_veto_sheet(df):
    if df.empty:
        return []
    work = add_normalized_map_column(_dedup_matches(df))
    work = work[work['_map_normalized'].astype(bool)]
    if work.empty:
        return []
    work['is_win'] = work['outcome'].astype(str).str.lower() == 'win'
    work['is_dec'] = work['outcome'].astype(str).str.lower().isin(['win', 'loss'])
    active = get_active_map_set()
    out = []
    for playlist in unique_sorted(work['playlist']):
        pdf = work[work['playlist'] == playlist]
        maps = []
        for map_name in unique_sorted(pdf['_map_normalized']):
            if _map_hidden(map_name, active):
                continue
            mdf = pdf[pdf['_map_normalized'] == map_name]
            dec = mdf[mdf['is_dec']]
            if len(dec) < 4:
                continue
            # Win% over DECIDED games (wins/decided), matching the rest of the site.
            wr = dec['is_win'].sum() / len(dec) * 100
            maps.append({
                'map': map_name, 'games': len(dec), 'win_pct': round(wr, 0),
                'win_pct_str': f'{wr:.0f}%',
                'kda': format_float(pd.to_numeric(mdf.get('kda'), errors='coerce').mean(), 2),
            })
        if not maps:
            continue
        maps.sort(key=lambda m: m['win_pct'], reverse=True)
        for m in maps:
            m['verdict'] = 'prefer' if m['win_pct'] >= 55 else ('ban' if m['win_pct'] <= 45 else 'neutral')
        add_composite_grades(maps, {'win_pct': True}, 'Map grade')
        out.append({'playlist': normalize_map_name(playlist) or playlist, 'maps': maps})
    return out


@app.route('/veto')
def veto():
    df = cache.get()
    try:
        sheets = get_cached_page_payload('veto', lambda: {'sheets': build_veto_sheet(df)})['sheets']
    except Exception as exc:
        logger.warning('veto page failed: %s', exc)
        sheets = []
    status = load_status()
    return render_template('veto.html', app_title=APP_TITLE, sheets=sheets,
                           last_update=status.get('last_update'),
                           db_row_count=count_cache.get())


# --- Goals & challenges ---------------------------------------------------

# metric key -> (label, higher_is_better, kind). kind: 'avg' over window,
# 'rate' (win %), or 'csr' (current standing value).
GOAL_METRICS = {
    'kda': ('KDA', True, 'avg'),
    'kills': ('Avg Kills', True, 'avg'),
    'accuracy': ('Accuracy %', True, 'avg'),
    'damage_dealt': ('Avg Damage', True, 'avg'),
    'win_rate': ('Win Rate %', True, 'rate'),
    'csr': ('Current CSR', True, 'csr'),
}


def fetch_goals(engine):
    ensure_goals_table(engine)
    with engine.begin() as conn:
        rows = conn.execute(text('SELECT id, player, metric, target, window_games, note, created_at '
                                 'FROM halo_goals ORDER BY created_at DESC')).fetchall()
    return [dict(r._mapping) for r in rows]


def save_goal(engine, player, metric, target, window_games, note):
    ensure_goals_table(engine)
    with engine.begin() as conn:
        conn.execute(text('INSERT INTO halo_goals (player, metric, target, window_games, note) '
                          'VALUES (:p, :m, :t, :w, :n)'),
                     {'p': player, 'm': metric, 't': float(target),
                      'w': int(window_games), 'n': note or None})


def delete_goal(engine, goal_id):
    with engine.begin() as conn:
        conn.execute(text('DELETE FROM halo_goals WHERE id = :id'), {'id': int(goal_id)})


def _goal_current_value(df, csr_map, player, metric, window_games):
    label, higher, kind = GOAL_METRICS.get(metric, ('', True, 'avg'))
    if kind == 'csr':
        return csr_map.get(player)
    pdf = df[df['player_gamertag'] == player].copy() if not df.empty else df
    if pdf.empty or 'date' not in pdf.columns:
        return None
    pdf = pdf.sort_values('date', ascending=False).head(int(window_games or 20))
    if pdf.empty:
        return None
    if kind == 'rate':
        o = pdf['outcome'].astype(str).str.lower()
        dec = o.isin(['win', 'loss'])
        return float((o == 'win').sum() / dec.sum() * 100) if dec.sum() else None
    series = pd.to_numeric(pdf.get(metric), errors='coerce')
    if metric == 'accuracy':
        # stored as a fraction in some rows; normalize to %.
        m = series.mean()
        return float(m * 100 if m is not None and m <= 1.0 else m)
    return float(series.mean()) if not series.dropna().empty else None


def build_goals(df, engine):
    goals = fetch_goals(engine)
    csr_standings = {s['player_gamertag']: safe_float(s.get('current_csr_value'))
                     for s in fetch_csr_standings(engine)}
    out = []
    for g in goals:
        label, higher, kind = GOAL_METRICS.get(g['metric'], (g['metric'], True, 'avg'))
        current = _goal_current_value(df, csr_standings, g['player'], g['metric'], g['window_games'])
        target = safe_float(g['target'])
        raw_pct = (current / target * 100) if (current is not None and target) else 0
        if current is None:
            pct = 0
            current_str = '–'
            pct_label = '—'
        else:
            pct = max(0, min(100, raw_pct))  # bar width is clamped 0-100
            pct_label = f'{raw_pct:.0f}%'    # but show the true % (can exceed 100 / go negative)
            current_str = f'{current:.2f}' if kind == 'avg' and g['metric'] not in ('kills',) else f'{current:.1f}'
            if g['metric'] in ('kills',):
                current_str = f'{current:.1f}'
            if kind == 'csr':
                current_str = f'{int(current)}'
        out.append({
            'id': g['id'], 'player': g['player'], 'metric_label': label,
            'metric': g['metric'],
            'target': (f'{int(target)}' if kind == 'csr' else f'{target:g}'),
            'window': g['window_games'], 'note': g.get('note') or '',
            'current': current_str, 'pct': round(pct, 0), 'pct_label': pct_label,
            'over': current is not None and current > target,
            'below_zero': current is not None and current < 0,
            'done': current is not None and current >= target,
            'kind': kind,
        })
    return out


@app.route('/goals', methods=['GET', 'POST'])
def goals():
    df = cache.get()
    all_players = unique_sorted(df['player_gamertag']) if not df.empty else []
    message = error = None
    if request.method == 'POST':
        action = request.form.get('action', 'save')
        try:
            if action == 'delete':
                delete_goal(ENGINE, request.form.get('goal_id', 0))
                message = 'Goal removed.'
            else:
                player = request.form.get('player', '').strip()
                metric = request.form.get('metric', '').strip()
                target = request.form.get('target', '').strip()
                window = request.form.get('window_games', '20').strip() or '20'
                note = request.form.get('note', '').strip()
                if not player or metric not in GOAL_METRICS or not target:
                    error = 'Pick a player, a metric, and a target.'
                else:
                    save_goal(ENGINE, player, metric, float(target), int(window), note)
                    message = 'Goal added.'
        except (ValueError, SQLAlchemyError) as exc:
            error = f'Could not save goal: {exc}'
    try:
        goal_rows = build_goals(df, ENGINE)
    except Exception as exc:
        logger.warning('goals page failed: %s', exc)
        goal_rows = []
    status = load_status()
    metric_options = [{'key': k, 'label': v[0]} for k, v in GOAL_METRICS.items()]
    return render_template('goals.html', app_title=APP_TITLE, goals=goal_rows,
                           players=all_players, metric_options=metric_options,
                           message=message, error=error,
                           last_update=status.get('last_update'),
                           db_row_count=count_cache.get())


# --- AI Coach (local Ollama) ---------------------------------------------

# Optional local-LLM coach. Point HALO_OLLAMA_URL at an Ollama server to enable
# the AI Coach pages; leave unset and the coach reports itself unavailable.
OLLAMA_URL = os.getenv('HALO_OLLAMA_URL', '').rstrip('/')
OLLAMA_MODEL = os.getenv('HALO_OLLAMA_MODEL', 'llama3.1:8b')
# Keep under waitress channel_timeout (120s) so a slow model fails cleanly
# instead of the proxy killing the whole connection (→ blank page).
OLLAMA_TIMEOUT = int(os.getenv('HALO_OLLAMA_TIMEOUT', '90'))
_COACH_CACHE = {'ts': 0.0, 'count': -1, 'text': None}
_PLAYER_COACH_CACHE = {}  # player -> {'count': int, 'text': str}
_COACH_PAGE_CACHE = {'ts': 0.0, 'count': -1, 'context': None, 'plan': None, 'ollama_ok': None}
COACH_PAGE_CACHE_TTL = int(os.getenv('HALO_COACH_PAGE_CACHE_TTL', '300'))


def _ollama_reachable():
    """Fast liveness probe so the page can render instantly and tell the user
    whether the model is available without blocking on a generation."""
    if not OLLAMA_URL:
        return False
    try:
        r = requests.get(f'{OLLAMA_URL}/api/tags', timeout=4)
        return r.status_code == 200
    except Exception:
        return False


def _player_quick_stats(df, player, lookback=30):
    pdf = df[df['player_gamertag'] == player].copy()
    if pdf.empty:
        return None
    if 'date' in pdf.columns:
        pdf = pdf.sort_values('date', ascending=False).head(lookback)
    o = pdf['outcome'].astype(str).str.lower()
    dec = o.isin(['win', 'loss'])
    k = pd.to_numeric(pdf.get('kills'), errors='coerce')
    d = pd.to_numeric(pdf.get('deaths'), errors='coerce')
    a = pd.to_numeric(pdf.get('assists'), errors='coerce')
    acc = pd.to_numeric(pdf.get('accuracy'), errors='coerce')
    acc_mean = acc.mean()
    if acc_mean is not None and acc_mean <= 1.0:
        acc_mean *= 100
    return {
        'games': len(pdf),
        'win_rate': (o == 'win').sum() / dec.sum() * 100 if dec.sum() else 0,
        'kda': safe_kda(k.mean(), a.mean(), d.mean()),  # per-game, not summed totals
        'kills': k.mean(), 'deaths': d.mean(), 'accuracy': acc_mean,
    }


def build_coach_context(df, engine):
    """Compact text snapshot of squad form for the model to reason over."""
    if df.empty:
        return 'No match data available.'
    lines = []
    csr_map = {s['player_gamertag']: s for s in fetch_csr_standings(engine)}
    for player in unique_sorted(df['player_gamertag']):
        qs = _player_quick_stats(df, player)
        if not qs:
            continue
        st = compute_streaks(df, player)
        csr = csr_map.get(player, {})
        rank = _csr_label(csr.get('current_csr_tier'), csr.get('current_csr_sub_tier'),
                          csr.get('current_csr_value')) if csr else 'Unknown'
        lines.append(
            f"- {player}: last {qs['games']} ranked games, "
            f"win rate {qs['win_rate']:.0f}%, KDA {qs['kda']:.2f}, "
            f"avg {qs['kills']:.1f}K/{qs['deaths']:.1f}D, accuracy {qs['accuracy']:.0f}%, "
            f"rank {rank}, current streak "
            f"{'W'+str(st['current_win']) if st['current_win'] else ('L'+str(st['current_loss']) if st['current_loss'] else 'none')}."
        )
    # Best/worst maps for the squad
    veto = build_veto_sheet(df)
    if veto:
        for sheet in veto[:1]:
            maps = sheet['maps']
            if maps:
                best = maps[0]
                worst = maps[-1]
                lines.append(f"- Squad best map ({sheet['playlist']}): {best['map']} ({best['win_pct_str']} win). "
                             f"Worst: {worst['map']} ({worst['win_pct_str']} win).")
    return 'Squad form snapshot (Halo Infinite ranked):\n' + '\n'.join(lines)


def build_plan_tonight(df):
    """'What to run tonight' — strongest lineup + best/worst maps by squad win%."""
    out = {'best_lineup': None, 'top_maps': [], 'avoid_maps': []}
    if df is None or df.empty:
        return out
    for size in (4, 3, 2):
        rows = build_lineup_stats(df, size, min_games=4, limit=1)
        if rows:
            bl = dict(rows[0])
            bl['size'] = size
            out['best_lineup'] = bl
            break
    ranked = _ranked_only(df)
    maps = [m for m in build_breakdown(ranked, 'map')
            if (to_number(m.get('matches')) or 0) >= 5]
    maps.sort(key=lambda m: to_number(m.get('win_rate')) or 0, reverse=True)
    out['top_maps'] = maps[:3]
    out['avoid_maps'] = list(reversed(maps[-3:])) if len(maps) > 3 else []
    return out


def get_cached_coach_page_data(df, engine, count):
    now = time.time()
    have = _COACH_PAGE_CACHE.get('context') is not None
    fresh = (have and _COACH_PAGE_CACHE.get('count') == count
             and now - _COACH_PAGE_CACHE.get('ts', 0) < COACH_PAGE_CACHE_TTL)

    def _compute():
        context = build_coach_context(df, engine)
        plan = build_plan_tonight(df)
        ollama_ok = _ollama_reachable()
        _COACH_PAGE_CACHE.update(ts=time.time(), count=count, context=context,
                                 plan=plan, ollama_ok=ollama_ok)
        return context, plan, ollama_ok

    if have:
        # Serve the cached context instantly; refresh in the background when
        # stale (the build walks a lot of df — it took ~20s per new game).
        if not fresh:
            _spawn_page_rebuild('_coach_context', _compute, None)
        return (_COACH_PAGE_CACHE['context'], _COACH_PAGE_CACHE['plan'],
                bool(_COACH_PAGE_CACHE.get('ollama_ok')))
    return _compute()


def call_ollama(prompt, system=None):
    payload = {'model': OLLAMA_MODEL, 'prompt': prompt, 'stream': False,
               'options': {'temperature': 0.6}}
    if system:
        payload['system'] = system
    r = requests.post(f'{OLLAMA_URL}/api/generate', json=payload, timeout=OLLAMA_TIMEOUT)
    r.raise_for_status()
    return (r.json() or {}).get('response', '').strip()


COACH_SYSTEM = (
    "You are a sharp, encouraging Halo Infinite coach. Be concise and specific. "
    "Use the provided squad stats; never invent numbers. Prefer concrete, actionable "
    "advice (positioning, role, weapon discipline, when to reset). Use short paragraphs "
    "or bullet points. Keep it under ~200 words unless asked for more."
)


@app.route('/api/coach/player/<player_name>')
def api_coach_player(player_name):
    """On-demand Ollama coaching tips for one player (cached per db count).
    Called by the player page so the page never blocks on a generation."""
    df = cache.get()
    all_players = unique_sorted(df['player_gamertag']) if not df.empty else []
    name = resolve_player_name(player_name, all_players)
    if not name:
        return {'ok': False, 'error': 'player not found'}, 404
    cur = count_cache.get()
    ent = _PLAYER_COACH_CACHE.get(name)
    if ent and ent.get('count') == cur:
        return {'ok': True, 'text': ent['text'], 'cached': True}
    if not _ollama_reachable():
        return {'ok': False, 'error': ('AI coach not configured — set HALO_OLLAMA_URL.' if not OLLAMA_URL
                                      else f'AI model offline ({OLLAMA_MODEL} @ {OLLAMA_URL}).')}
    try:
        context = build_coach_context(df, ENGINE)
        prompt = (f"{context}\n\nFocus on {name}. Give {name} 3 short, specific, actionable "
                  f"tips to climb ranked Halo Infinite, based ONLY on the stats above. "
                  f"Bullet points, under 130 words, encouraging but direct.")
        txt = call_ollama(prompt, system=COACH_SYSTEM)
        if txt:
            _PLAYER_COACH_CACHE[name] = {'count': cur, 'text': txt}
        return {'ok': True, 'text': txt}
    except Exception as exc:
        logger.warning('api_coach_player failed for %s: %s', name, exc)
        return {'ok': False, 'error': 'generation failed'}


@app.route('/coach', methods=['GET', 'POST'])
def coach():
    df = cache.get()
    action = request.form.get('action', '') if request.method == 'POST' else ''
    question = (request.form.get('question', '') if request.method == 'POST' else '').strip()
    answer = None
    auto = None
    error = None
    now = time.time()
    cur = count_cache.get()
    try:
        context, plan, ollama_ok = get_cached_coach_page_data(df, ENGINE, cur)
    except Exception as exc:
        logger.warning('coach page data failed: %s', exc)
        context, plan, ollama_ok = 'No match data available.', None, False
    cached_brief = (_COACH_CACHE['text'] if _COACH_CACHE['text'] and
                    _COACH_CACHE['count'] == cur else None)

    if question:
        if not ollama_ok:
            error = ('AI coach not configured — set HALO_OLLAMA_URL to an Ollama server to enable it.'
                     if not OLLAMA_URL else
                     f'The local AI model ({OLLAMA_MODEL} @ {OLLAMA_URL}) is not reachable right now.')
        else:
            try:
                answer = call_ollama(f"{context}\n\nQuestion: {question}\n\nAnswer using the stats above.",
                                     system=COACH_SYSTEM)
            except Exception as exc:
                error = f'The model didn\'t respond in time. {exc}'
    elif action == 'brief':
        if not ollama_ok:
            error = ('AI coach not configured — set HALO_OLLAMA_URL to an Ollama server to enable it.'
                     if not OLLAMA_URL else
                     f'The local AI model ({OLLAMA_MODEL} @ {OLLAMA_URL}) is not reachable right now.')
        else:
            try:
                auto = call_ollama(
                    f"{context}\n\nGive the squad a short coaching briefing: who's hot, who's "
                    f"struggling, one concrete focus for the group, and a map tip.",
                    system=COACH_SYSTEM)
                _COACH_CACHE.update(text=auto, count=cur, ts=now)
            except Exception as exc:
                error = f'The model didn\'t respond in time. {exc}'
    else:
        auto = cached_brief  # show last briefing if we have one; else offer button

    status = load_status()
    return render_template('coach.html', app_title=APP_TITLE,
                           answer=answer, auto=auto, question=question,
                           error=error, ollama_ok=ollama_ok, model=OLLAMA_MODEL,
                           context=context, has_brief=auto is not None,
                           plan=plan,
                           last_update=status.get('last_update'),
                           db_row_count=count_cache.get())


# --- Shareable PNG stat cards + PWA icons --------------------------------

def _load_font(size, bold=False):
    from PIL import ImageFont
    candidates = [
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold
        else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _build_stat_card(player, df, engine):
    from PIL import Image, ImageDraw
    W, H = 1000, 520
    bg = (16, 18, 24)
    accent = (88, 166, 255)
    img = Image.new('RGB', (W, H), bg)
    d = ImageDraw.Draw(img)
    # Header band
    d.rectangle([0, 0, W, 96], fill=(24, 27, 36))
    d.text((40, 26), _img_text(APP_TITLE).upper() or 'HALO STATS', font=_load_font(40, bold=True), fill=accent)
    d.text((40, 70), 'Halo Infinite — Ranked', font=_load_font(20), fill=(150, 156, 170))

    qs = _player_quick_stats(df, player, lookback=50) or {
        'games': 0, 'win_rate': 0, 'kda': 0, 'kills': 0, 'deaths': 0, 'accuracy': 0}
    csr_map = {s['player_gamertag']: s for s in fetch_csr_standings(engine)}
    csr = csr_map.get(player, {})
    rank = _csr_label(csr.get('current_csr_tier'), csr.get('current_csr_sub_tier'),
                      csr.get('current_csr_value')) if csr else 'Unranked'

    d.text((40, 130), player, font=_load_font(56, bold=True), fill=(240, 242, 248))
    d.text((40, 200), f'{rank}', font=_load_font(30), fill=accent)

    stats = [
        ('KDA', f"{qs['kda']:.2f}"),
        ('WIN %', f"{qs['win_rate']:.0f}%"),
        ('ACC', f"{(qs['accuracy'] or 0):.0f}%"),
        ('AVG K', f"{(qs['kills'] or 0):.1f}"),
        ('AVG D', f"{(qs['deaths'] or 0):.1f}"),
        ('GAMES', f"{qs['games']}"),
    ]
    cols = 3
    bx, by = 40, 270
    bw, bh, gap = 300, 100, 20
    for i, (label, val) in enumerate(stats):
        cx = bx + (i % cols) * (bw + gap)
        cy = by + (i // cols) * (bh + gap)
        d.rounded_rectangle([cx, cy, cx + bw, cy + bh], radius=14, fill=(26, 30, 40))
        d.text((cx + 20, cy + 16), label, font=_load_font(20), fill=(150, 156, 170))
        d.text((cx + 20, cy + 44), val, font=_load_font(40, bold=True), fill=(240, 242, 248))
    d.text((40, H - 34), 'last 50 ranked games', font=_load_font(18), fill=(110, 116, 130))
    return img


@app.route('/card/<player_name>.png')
def stat_card(player_name):
    df = cache.get()
    all_players = unique_sorted(df['player_gamertag']) if not df.empty else []
    player = resolve_player_name(player_name, all_players) or player_name
    try:
        import io
        img = _build_stat_card(player, df, ENGINE)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return Response(buf.getvalue(), mimetype='image/png',
                        headers={'Cache-Control': 'public, max-age=300'})
    except Exception as exc:
        logger.warning('stat card failed for %s: %s', player, exc)
        return Response('card unavailable', status=503)


# Grade → colour + ordering, shared by the session card.
_GRADE_ORDER = ['S', 'A+', 'A', 'A-', 'B+', 'B', 'B-', 'C+', 'C', 'C-', 'D+', 'D', 'D-', 'F']


def _grade_rgb(g):
    g = (g or '').strip().upper()
    if g == 'S':
        return (242, 169, 0)            # gold
    if g.startswith('A'):
        return (74, 222, 128)           # green
    if g.startswith('B'):
        return (96, 165, 250)           # blue
    if g.startswith('C'):
        return (245, 190, 90)           # amber
    if g.startswith('D') or g == 'F':
        return (248, 113, 113)          # red
    return (150, 156, 170)


def _grade_rank(g):
    g = (g or '').strip().upper()
    return _GRADE_ORDER.index(g) if g in _GRADE_ORDER else len(_GRADE_ORDER)


# Weakest graded stat → one concrete thing to work on next session.
_COACH_TIPS = {
    'ddiff': "Win more gunfights - pre-aim power angles and trade, don't solo-push.",
    'life':  "Dying too fast - play near teammates and disengage when you're low.",
    'obj':   "Get on the objective - hill time and caps swing games more than kills.",
    'score': "Round out your impact - assists, obj time and damage, not just frags.",
    'dmgm':  "Stay active every fight - put out consistent damage instead of going quiet.",
}
_STAT_LABEL = {'ddiff': 'Dmg±', 'life': 'Survival', 'obj': 'Objectives',
               'score': 'Score share', 'dmgm': 'Damage'}


def _img_text(s):
    """Drop emoji/high glyphs DejaVu can't render (keeps ±, ·, accents)."""
    return ''.join(c for c in str(s) if ord(c) < 0x2500).strip()


def _coach_for(p):
    """(coaching tip, weakest stat label, strongest stat label) for a player."""
    graded = [(k, p.get(f'{k}_grade', '')) for k in ('ddiff', 'life', 'obj', 'dmgm', 'score')]
    graded = [(k, g) for k, g in graded if g]
    if not graded:
        return ("Keep stacking reps — not enough games to read a weak spot yet.", '', '')
    worst = max(graded, key=lambda kv: _grade_rank(kv[1]))
    best = min(graded, key=lambda kv: _grade_rank(kv[1]))
    tip = _COACH_TIPS.get(worst[0], "Keep stacking reps.")
    # If they're bleeding deaths, that trumps the graded weak spot.
    try:
        if float(p.get('deaths', 0)) >= 8.5:
            tip = "Cut the deaths - " + tip[0].lower() + tip[1:]
    except (TypeError, ValueError):
        pass
    return (tip, f"{_STAT_LABEL.get(worst[0], worst[0])} {worst[1]}",
            f"{_STAT_LABEL.get(best[0], best[0])} {best[1]}")


def _build_session_card(df):
    """Rich shareable PNG of the latest squad session: record, per-player grades,
    every game's result+grade, outlier standouts, and a coaching focus per player.
    Reuses build_super_live so it matches the /live board exactly."""
    from PIL import Image, ImageDraw
    data = build_super_live(df) or {}
    players = [p for p in (data.get('players') or [])][:6]

    bg, panel, accent = (16, 18, 24), (26, 30, 40), (242, 169, 0)
    muted, white, green, red, blue = (150, 156, 170), (240, 242, 248), (74, 222, 128), (248, 113, 113), (150, 190, 255)
    W, margin = 1200, 40
    HEADER_H, SUMMARY_H, CARD_H, GAP = 100, 120, 214, 14
    n = max(1, len(players))
    H = HEADER_H + SUMMARY_H + n * (CARD_H + GAP) + 30

    f_hdr = _load_font(40, bold=True); f_sub = _load_font(20)
    f_big = _load_font(66, bold=True); f_name = _load_font(28, bold=True)
    f_grade = _load_font(36, bold=True); f_stat = _load_font(20)
    f_chip = _load_font(16, bold=True); f_small = _load_font(16); f_coach = _load_font(18)
    f_game = _load_font(13, bold=True)

    img = Image.new('RGB', (W, H), bg)
    d = ImageDraw.Draw(img)

    # ── Header ──
    d.rectangle([0, 0, W, HEADER_H], fill=(24, 27, 36))
    d.text((margin, 24), _img_text(APP_TITLE).upper() or 'HALO STATS', font=f_hdr, fill=accent)
    d.text((margin, 72), 'Session Recap · Halo Infinite Ranked', font=f_sub, fill=muted)

    # ── Summary band ──
    wins, losses = int(data.get('wins', 0)), int(data.get('losses', 0))
    games = data.get('game_count', wins + losses)
    win_pct = data.get('win_pct', '0%')
    sess_date = _img_text(data.get('session_date', ''))
    kind = str(data.get('kind', 'squad')).upper()
    sy = HEADER_H + 16
    d.text((margin, sy), f'{wins}', font=f_big, fill=green)
    ww = d.textlength(f'{wins}', font=f_big)
    d.text((margin + ww + 10, sy + 14), '-', font=_load_font(54, bold=True), fill=muted)
    sep = d.textlength('- ', font=_load_font(54, bold=True))
    d.text((margin + ww + 10 + sep, sy), f'{losses}', font=f_big, fill=red)
    d.text((330, sy + 6), kind, font=_load_font(22, bold=True), fill=accent)
    d.text((330, sy + 38), f'{games} games · {win_pct} win rate', font=_load_font(24), fill=white)
    if sess_date:
        d.text((330, sy + 72), sess_date, font=f_small, fill=muted)
    rank = data.get('session_rank') or {}
    if rank.get('rank') and rank.get('total'):
        d.text((470, sy + 6), f"#{rank['rank']} of {rank['total']} nights (30d)",
                font=_load_font(20), fill=accent)

    # ── Per-player cards ──
    y = HEADER_H + SUMMARY_H
    for p in players:
        name = _img_text(p.get('player', ''))
        pg = int(p.get('games', 0) or 0)
        pw = int(p.get('wins', 0) or 0)
        pl_ = max(pg - pw, 0)
        try:
            kda = f"{float(p.get('kda', 0)):.2f}"
        except (TypeError, ValueError):
            kda = str(p.get('kda', ''))
        grade = str(p.get('avg_game_grade', '') or '')
        csr = p.get('current_csr', '') or ''
        csr_d = p.get('csr_delta', '') or ''
        acc = p.get('accuracy', '')
        ddpg = p.get('dmg_diff_pg', '')
        kpg, dpg, apg = p.get('kills', '?'), p.get('deaths', '?'), p.get('assists', '?')

        d.rounded_rectangle([margin, y, W - margin, y + CARD_H], radius=14, fill=panel)
        x = margin + 22
        d.text((x, y + 14), name, font=f_name, fill=white)
        # grade badge + CSR (right-aligned)
        if grade:
            gw = d.textlength(grade, font=f_grade)
            d.text((W - margin - 24 - gw, y + 12), grade, font=f_grade, fill=_grade_rgb(grade))
        csr_line = f"CSR {csr}" + (f" ({csr_d})" if csr_d and csr_d != '—' else '')
        cw = d.textlength(csr_line, font=f_small)
        d.text((W - margin - 24 - cw, y + 56), csr_line, font=f_small, fill=muted)

        # stat line
        stat = f"{pw}-{pl_}      {kda} KDA      {kpg}/{dpg}/{apg} K/D/A      {acc} ACC      {ddpg} Dmg±"
        d.text((x, y + 54), _img_text(stat), font=f_stat, fill=(205, 212, 226))

        # per-stat grade chips
        cx = x
        for key, lbl in (('ddiff', 'Dmg±'), ('life', 'Surv'), ('obj', 'Obj'), ('score', 'Sc%'), ('dmgm', 'Dmg/m')):
            g = p.get(f'{key}_grade', '')
            if not g:
                continue
            txt = f"{lbl} {g}"
            tw = d.textlength(txt, font=f_chip)
            d.rounded_rectangle([cx, y + 88, cx + tw + 20, y + 112], radius=11, fill=(20, 23, 31))
            d.text((cx + 10, y + 92), txt, font=f_chip, fill=_grade_rgb(g))
            cx += tw + 30

        # game-by-game strip: one square per game, coloured by W/L, grade letter inside
        gg = p.get('game_grades', []) or []
        d.text((x, y + 126), 'GAMES', font=f_chip, fill=muted)
        gx = x + 76
        for g in gg[:30]:
            fill = green if g.get('won') else (red if g.get('result') == 'L' else (90, 96, 110))
            d.rounded_rectangle([gx, y + 122, gx + 24, y + 146], radius=5, fill=fill)
            gl = str(g.get('grade', '') or '')
            if gl:
                lw = d.textlength(gl, font=f_game)
                d.text((gx + 12 - lw / 2, y + 127), gl, font=f_game, fill=(16, 18, 24))
            gx += 28

        # standouts (outlier games)
        outs = [f"G{g['num']} {_img_text(g.get('watch_reason', ''))}" for g in gg if g.get('watch')]
        so = "  ·  ".join(outs[:3]) if outs else "no standout games this session"
        d.text((x, y + 156), _img_text("STANDOUTS  " + so), font=f_small, fill=accent)

        # coaching
        tip, weak, strong = _coach_for(p)
        coach = f"COACH:  {tip}"
        d.text((x, y + 182), _img_text(coach), font=f_coach, fill=blue)
        if weak:
            wtxt = _img_text(f"work on: {weak}")
            wtw = d.textlength(wtxt, font=f_small)
            d.text((W - margin - 24 - wtw, y + 184), wtxt, font=f_small, fill=(200, 150, 150))

        y += CARD_H + GAP
    return img


@app.route('/session-card.png')
@app.route('/squad-card.png')
def session_card():
    df = cache.get()
    try:
        def _render() -> dict:
            import io
            img = _build_session_card(df)
            buf = io.BytesIO()
            img.save(buf, format='PNG')
            return {'png': buf.getvalue()}
        # SWR-cached PNG bytes — rendering took ~3s per hit.
        png = get_cached_page_payload('session_card_png', _render)['png']
        return Response(png, mimetype='image/png',
                        headers={'Cache-Control': 'public, max-age=120'})
    except Exception as exc:
        logger.warning('session card failed: %s', exc)
        return Response('card unavailable', status=503)


def _build_app_icon(size):
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (size, size), (16, 18, 24))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([size * 0.08, size * 0.08, size * 0.92, size * 0.92],
                        radius=int(size * 0.18), fill=(88, 166, 255))
    d.text((size * 0.30, size * 0.18), 'SK', font=_load_font(int(size * 0.42), bold=True),
           fill=(16, 18, 24))
    return img


@app.route('/icon-<int:size>.png')
def app_icon(size):
    if size not in (192, 512, 180):
        size = 192
    # Baked logo art (blade-crown + halo ring) ships in static/; the old
    # PIL-generated square is only a fallback if the files ever go missing.
    baked = os.path.join(app.static_folder or 'static', f'icon-{size}.png')
    if os.path.exists(baked):
        with open(baked, 'rb') as f:
            return Response(f.read(), mimetype='image/png',
                            headers={'Cache-Control': 'public, max-age=86400'})
    try:
        import io
        img = _build_app_icon(size)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return Response(buf.getvalue(), mimetype='image/png',
                        headers={'Cache-Control': 'public, max-age=86400'})
    except Exception as exc:
        logger.warning('icon failed: %s', exc)
        return Response('icon unavailable', status=503)


# --- PWA: manifest + service worker --------------------------------------

@app.route('/api/push/key')
def push_key():
    """VAPID public key for the browser to subscribe with."""
    return jsonify({'key': push.public_key()})


@app.route('/api/push/subscribe', methods=['POST'])
def push_subscribe():
    sub = request.get_json(silent=True) or {}
    if not sub.get('endpoint'):
        return jsonify({'ok': False, 'error': 'no endpoint'}), 400
    push.add_sub(sub)
    return jsonify({'ok': True, 'count': push.sub_count()})


@app.route('/api/push/unsubscribe', methods=['POST'])
def push_unsubscribe():
    sub = request.get_json(silent=True) or {}
    push.remove_sub(sub.get('endpoint', ''))
    return jsonify({'ok': True})


@app.route('/api/push/test', methods=['POST'])
def push_test():
    sent = push.send_push(APP_TITLE, "Test notification — you're subscribed to live alerts.", '/live')
    return jsonify({'ok': True, 'sent': sent})


@app.route('/manifest.webmanifest')
def manifest():
    data = {
        'name': APP_TITLE,
        'short_name': APP_TITLE,
        'start_url': '/',
        'display': 'standalone',
        'background_color': '#101218',
        'theme_color': '#101218',
        'icons': [
            {'src': '/icon-192.png', 'sizes': '192x192', 'type': 'image/png'},
            {'src': '/icon-512.png', 'sizes': '512x512', 'type': 'image/png'},
            {'src': '/static/icon-512-maskable.png', 'sizes': '512x512',
             'type': 'image/png', 'purpose': 'maskable'},
        ],
    }
    return Response(json.dumps(data), mimetype='application/manifest+json')


@app.route('/sw.js')
def service_worker():
    # Network-first for navigations (always try fresh stats), cache-first for
    # static assets, with an offline fallback to the last cached dashboard.
    js = "const APP_TITLE = " + json.dumps(APP_TITLE) + ";\n" + """
const CACHE = 'halostats-v2';
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(
  caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim())
));

// Web Push: live-event notifications (fires even when the app is closed).
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; }
  catch (_) { d = { title: APP_TITLE, body: (e.data && e.data.text()) || '' }; }
  e.waitUntil(self.registration.showNotification(d.title || APP_TITLE, {
    body: d.body || '', icon: '/icon-192.png', badge: '/icon-192.png',
    tag: d.tag || 'halo', renotify: true, data: { url: d.url || '/live' },
  }));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/live';
  e.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(ws => {
    for (const w of ws) { if ('focus' in w) { w.navigate && w.navigate(url); return w.focus(); } }
    return clients.openWindow(url);
  }));
});
self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.pathname.startsWith('/static/') || url.pathname.startsWith('/icon-')) {
    e.respondWith(caches.open(CACHE).then(c => c.match(req).then(hit =>
      hit || fetch(req).then(res => { c.put(req, res.clone()); return res; }))));
    return;
  }
  e.respondWith(fetch(req).then(res => {
    if (url.pathname === '/' ) { const cp = res.clone(); caches.open(CACHE).then(c => c.put(req, cp)); }
    return res;
  }).catch(() => caches.match(req)));
});
"""
    return Response(js, mimetype='application/javascript')


# --- Opponents: nemesis + full match scoreboard --------------------------

def _match_players_available():
    try:
        with ENGINE.connect() as conn:
            return int(conn.execute(text('SELECT COUNT(*) FROM halo_match_players')).scalar() or 0)
    except SQLAlchemyError:
        return 0


def build_nemesis(engine):
    """For every match a tracked player was in, the squad's result vs each
    recurring opponent. 'Nemesis' = opponents the squad loses to most."""
    sql = """
        WITH squad AS (
            SELECT match_id, BOOL_OR(outcome = 'win') AS squad_won
            FROM halo_match_players WHERE is_tracked = TRUE
            GROUP BY match_id
        )
        SELECT mp.gamertag, mp.player_xuid,
               COUNT(*) AS games,
               SUM(CASE WHEN s.squad_won THEN 1 ELSE 0 END) AS squad_wins,
               AVG(mp.kda) AS avg_kda
        FROM halo_match_players mp
        JOIN squad s ON s.match_id = mp.match_id
        WHERE mp.is_tracked = FALSE
        GROUP BY mp.gamertag, mp.player_xuid
        HAVING COUNT(*) >= 3
    """
    try:
        with engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(text(sql))]
    except SQLAlchemyError as exc:
        logger.warning('build_nemesis failed: %s', exc)
        return {'nemeses': [], 'most_faced': []}
    out = []
    for r in rows:
        games = int(r['games'])
        squad_wins = int(r['squad_wins'] or 0)
        win_pct = squad_wins / games * 100 if games else 0
        out.append({
            'gamertag': r['gamertag'],
            'games': games,
            'squad_record': f'{squad_wins}-{games - squad_wins}',
            'win_pct': round(win_pct, 0),
            'win_pct_str': f'{win_pct:.0f}%',
            'avg_kda': format_float(r.get('avg_kda'), 2),
        })
    # Nemeses: lowest squad win% against (most painful), needs a few games.
    nemeses = sorted([o for o in out if o['games'] >= 4], key=lambda o: o['win_pct'])[:15]
    # Favorites: opponents the squad beats up on (highest win%).
    favorites = sorted([o for o in out if o['games'] >= 4], key=lambda o: -o['win_pct'])[:15]
    most_faced = sorted(out, key=lambda o: o['games'], reverse=True)[:15]
    add_composite_grades(nemeses, {'win_pct': True}, 'Matchup grade')
    add_composite_grades(favorites, {'win_pct': True}, 'Matchup grade')
    add_composite_grades(most_faced, {'win_pct': True}, 'Matchup grade')

    # Per-player breakdown vs the top nemeses: WHO on the squad each one wrecks.
    breakdown = []
    nemesis_names = [n['gamertag'] for n in nemeses[:8]]
    if nemesis_names:
        psql = text("""
            SELECT t.gamertag AS player, o.gamertag AS opponent,
                   COUNT(*) AS games,
                   SUM(CASE WHEN t.outcome = 'win' THEN 1 ELSE 0 END) AS wins,
                   AVG(t.kda) AS avg_kda
            FROM halo_match_players t
            JOIN halo_match_players o ON o.match_id = t.match_id AND o.is_tracked = FALSE
            WHERE t.is_tracked = TRUE AND o.gamertag IN :names
            GROUP BY t.gamertag, o.gamertag
            HAVING COUNT(*) >= 1
        """).bindparams(bindparam('names', expanding=True))
        try:
            with engine.connect() as conn:
                prows = [dict(r._mapping) for r in conn.execute(psql, {'names': nemesis_names})]
        except SQLAlchemyError as exc:
            logger.warning('nemesis per-player failed: %s', exc)
            prows = []
        by_opp = {}
        for r in prows:
            g = int(r['games'])
            w = int(r['wins'] or 0)
            by_opp.setdefault(r['opponent'], []).append({
                'player': r['player'],
                'css': get_player_class(r['player']),
                'games': g,
                'record': f'{w}-{g - w}',
                'avg_kda': format_float(r.get('avg_kda'), 2),
                'avg_kda_num': safe_float(r.get('avg_kda')),
            })
        for n in nemeses[:8]:
            players = by_opp.get(n['gamertag'], [])
            if not players:
                continue
            players.sort(key=lambda p: p['avg_kda_num'])  # worst-performing player first
            breakdown.append({
                'opponent': n['gamertag'],
                'win_pct_str': n['win_pct_str'],
                'squad_record': n['squad_record'],
                'players': players,
            })
    return {'nemeses': nemeses, 'favorites': favorites, 'most_faced': most_faced,
            'nemesis_breakdown': breakdown}


@app.route('/nemesis')
def nemesis():
    have = _match_players_available()
    try:
        payload = get_cached_page_payload('nemesis', lambda: build_nemesis(ENGINE)) if have else {'nemeses': [], 'favorites': [], 'most_faced': []}
    except Exception as exc:
        logger.warning('nemesis page failed: %s', exc)
        payload = {'nemeses': [], 'favorites': [], 'most_faced': []}
    status = load_status()
    return render_template('nemesis.html', app_title=APP_TITLE,
                           nemeses=payload['nemeses'], favorites=payload.get('favorites', []),
                           most_faced=payload['most_faced'],
                           nemesis_breakdown=payload.get('nemesis_breakdown', []),
                           have_data=have > 0,
                           last_update=status.get('last_update'),
                           db_row_count=count_cache.get())


# ── Full match-detail page: every stat + every medal we captured ──
# Per tracked player, halo_match_stats holds the COMPLETE per-match stat row
# (combat, shooting, damage, survival, per-mode objective stats, CSR/MMR
# context) plus one column per medal earned. The match page renders all of it,
# organized into labeled groups; groups render only the rows that exist and
# mode groups only when that player actually has mode activity (no zero-walls
# of CTF stats on a Slayer game).
_MP_STAT_GROUPS = [
    ('⚔️ Combat', [
        ('Kills', 'kills', 'int'), ('Deaths', 'deaths', 'int'),
        ('Assists', 'assists', 'int'), ('KDA', 'kda', 'f2'),
        ('Max killing spree', 'max_killing_spree', 'int'),
        ('Headshot kills', 'headshot_kills', 'int'),
        ('Melee kills', 'melee_kills', 'int'),
        ('Grenade kills', 'grenade_kills', 'int'),
        ('Power weapon kills', 'power_weapon_kills', 'int'),
        ('Hijacks', 'hijacks', 'int'), ('Vehicle destroys', 'vehicle_destroys', 'int'),
        ('Betrayals', 'betrayals', 'int'), ('Suicides', 'suicides', 'int'),
    ]),
    ('🎯 Shooting', [
        ('Accuracy', 'accuracy', 'pct'),
        ('Shots fired', 'shots_fired', 'int'), ('Shots hit', 'shots_hit', 'int'),
    ]),
    ('💥 Damage', [
        ('Damage dealt', 'damage_dealt', 'int'), ('Damage taken', 'damage_taken', 'int'),
        ('Damage diff', 'dmg_difference', 'signed'), ('Dmg / min', 'dmg/min', 'f1'),
        ('Dmg / kill+assist', 'dmg/ka', 'f1'), ('Dmg / death', 'dmg/death', 'f1'),
    ]),
    ('🛡️ Survival & score', [
        ('Avg life', 'average_life_duration', 'secs'), ('Spawns', 'spawns', 'int'),
        ('Score', 'score', 'int'), ('Personal score', 'personal_score', 'int'),
        ('Objectives completed', 'objectives_completed', 'int'),
        ('Callout assists', 'callout_assists', 'int'),
        ('Driver assists', 'driver_assists', 'int'), ('EMP assists', 'emp_assists', 'int'),
        ('Rounds won', 'rounds_won', 'int'), ('Rounds lost', 'rounds_lost', 'int'),
        ('Rounds tied', 'rounds_tied', 'int'),
    ]),
    ('📈 Skill context', [
        ('CSR before', 'pre_match_csr', 'int'), ('CSR after', 'post_match_csr', 'int'),
        ('Expected kills', 'expected_kills', 'f1'),
        ('Expected deaths', 'expected_deaths', 'f1'),
    ]),
]
_MP_MODE_GROUPS = [
    ('🚩 Capture the Flag', 'capture_the_flag_stats_', [
        ('flag_captures', 'Flag captures', 'int'),
        ('flag_capture_assists', 'Capture assists', 'int'),
        ('flag_grabs', 'Flag grabs', 'int'), ('flag_steals', 'Flag steals', 'int'),
        ('flag_returns', 'Flag returns', 'int'), ('flag_secures', 'Flag secures', 'int'),
        ('flag_carriers_killed', 'Carriers killed', 'int'),
        ('flag_returners_killed', 'Returners killed', 'int'),
        ('kills_as_flag_carrier', 'Kills as carrier', 'int'),
        ('kills_as_flag_returner', 'Kills as returner', 'int'),
        ('time_as_flag_carrier', 'Time as carrier', 'mmss'),
    ]),
    ('💀 Oddball', 'oddball_stats_', [
        ('time_as_skull_carrier', 'Time with ball', 'mmss'),
        ('longest_time_as_skull_carrier', 'Longest hold', 'mmss'),
        ('skull_grabs', 'Ball grabs', 'int'),
        ('skull_scoring_ticks', 'Scoring ticks', 'int'),
        ('skull_carriers_killed', 'Carriers killed', 'int'),
        ('kills_as_skull_carrier', 'Kills as carrier', 'int'),
    ]),
    ('⛰️ Strongholds / KOTH', 'zones_stats_', [
        ('stronghold_occupation_time', 'Zone time', 'mmss'),
        ('stronghold_captures', 'Zone captures', 'int'),
        ('stronghold_secures', 'Zone secures', 'int'),
        ('stronghold_offensive_kills', 'Offensive kills', 'int'),
        ('stronghold_defensive_kills', 'Defensive kills', 'int'),
        ('stronghold_scoring_ticks', 'Scoring ticks', 'int'),
    ]),
    ('⛏️ Extraction', 'extraction_stats_', [
        ('successful_extractions', 'Extractions', 'int'),
        ('extraction_initiations_completed', 'Initiations', 'int'),
        ('extraction_initiations_denied', 'Initiations denied', 'int'),
        ('extraction_conversions_completed', 'Conversions', 'int'),
        ('extraction_conversions_denied', 'Conversions denied', 'int'),
    ]),
]
# Team-vs-team aggregate rows: (label, stat suffix, kind, lower_is_better)
_MP_TEAM_ROWS = [
    ('Kills', 'kills', 'int', False), ('Deaths', 'deaths', 'int', True),
    ('Assists', 'assists', 'int', False), ('KDA', 'kda', 'f2', False),
    ('Damage dealt', 'damage_dealt', 'int', False),
    ('Damage taken', 'damage_taken', 'int', True),
    ('Accuracy', 'accuracy', 'pct', False),
    ('Avg life', 'average_life_duration', 'secs', False),
    ('Headshot kills', 'headshot_kills', 'int', False),
    ('Melee kills', 'melee_kills', 'int', False),
    ('Grenade kills', 'grenade_kills', 'int', False),
    ('Power weapon kills', 'power_weapon_kills', 'int', False),
    ('Max killing spree', 'max_killing_spree', 'int', False),
    ('Medals', 'medal_count', 'int', False),
    ('Personal score', 'personal_score', 'int', False),
    ('Team MMR', 'mmr', 'int', False),
]


def _mp_fmt(v, kind):
    """Format one stat value; None when the value is missing (row omitted)."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    f = safe_float(v)
    if kind == 'int':
        return format_int(f)
    if kind == 'f1':
        return format_float(f, 1)
    if kind == 'f2':
        return format_float(f, 2)
    if kind == 'pct':
        return f"{f * 100:.1f}%" if 0 < f <= 1.0 else f"{f:.1f}%"
    if kind == 'secs':
        return f"{f:.1f}s"
    if kind == 'mmss':
        s = int(round(f))
        return f"{s // 60}:{s % 60:02d}"
    if kind == 'signed':
        return f"{f:+,.0f}"
    return str(v)


def _mp_medal_name(col: str) -> str:
    """medal_Back_Smack → 'Back Smack'; medal_Odin_s_Raven → \"Odin's Raven\";
    medal_id_123 → 'Medal #123' (metadata name was unavailable at scrape time)."""
    if col.startswith('medal_id_'):
        return f"Medal #{col[len('medal_id_'):]}"
    return col[len('medal_'):].replace('_s_', "'s_").replace('_', ' ').strip()


def _mp_why(info, mdf, meta):
    """Why we won / why we lost: split the game into the DECISIVE stats (what
    actually scores in this mode — hill ticks, flag caps, ball time, kills in
    Slayer) vs the gunfight stats, and say which one swung the result. Built
    because good stat lines kept coexisting with losses — the answer is almost
    always 'won the fight, lost the scoreboard'."""
    oc = str(info.get('outcome', '')).lower()
    if oc not in ('win', 'loss'):
        return None
    won = oc == 'win'

    def g(col):
        v = info.get(col)
        try:
            if v is None or pd.isna(v):
                return None
        except (TypeError, ValueError):
            return None
        return safe_float(v)

    mode = (clean_mode(info.get('game_type')) or '').lower()
    bullets = []

    # ── The decisive exchange: what scores in this mode ──
    decisive_good = None
    def decide(us, them, desc, fmt='int'):
        nonlocal decisive_good
        if us is None or them is None or (us == 0 and them == 0):
            return
        decisive_good = us > them
        u, t = _mp_fmt(us, fmt), _mp_fmt(them, fmt)
        if us > them:
            bullets.append(f"🎯 Scoreboard: we took the {desc} {u}–{t} — that's what decides this mode")
        elif us < them:
            bullets.append(f"🎯 Scoreboard: they took the {desc} {t}–{u} — that's what decides this mode")
        else:
            bullets.append(f"🎯 Scoreboard: dead even on {desc} ({u}–{t}) — this one came down to moments")
            decisive_good = None
    if 'slayer' in mode:
        decide(g('team_kills'), g('enemy_team_kills'), 'kill race')
    elif 'flag' in mode or 'ctf' in mode:
        decide(g('team_capture_the_flag_stats_flag_captures'),
               g('enemy_team_capture_the_flag_stats_flag_captures'), 'flag captures')
    elif 'oddball' in mode or 'ball' in mode:
        decide(g('team_oddball_stats_skull_scoring_ticks'),
               g('enemy_team_oddball_stats_skull_scoring_ticks'), 'ball-time ticks')
    elif 'hill' in mode or 'strongholds' in mode or 'king' in mode or 'zone' in mode:
        decide(g('team_zones_stats_stronghold_scoring_ticks'),
               g('enemy_team_zones_stats_stronghold_scoring_ticks'), 'zone-control ticks')
    elif 'extraction' in mode:
        decide(g('team_extraction_stats_successful_extractions'),
               g('enemy_team_extraction_stats_successful_extractions'), 'extractions')

    # ── The gunfight: did we win the combat exchange? ──
    fight_pts = []
    fight_score = 0
    tk, ek = g('team_kills'), g('enemy_team_kills')
    if tk is not None and ek is not None and 'slayer' not in mode:
        fight_score += (tk > ek) - (tk < ek)
        fight_pts.append(f"kills {format_int(tk)}–{format_int(ek)}")
    td, ed = g('team_damage_dealt'), g('enemy_team_damage_dealt')
    if td is not None and ed is not None and (td or ed):
        fight_score += (td > ed) - (td < ed)
        fight_pts.append(f"damage {_mp_fmt(td - ed, 'signed')}")
    ta, ea = g('team_accuracy'), g('enemy_team_accuracy')
    if ta and ea:
        fight_score += (ta > ea) - (ta < ea)
        fight_pts.append(f"accuracy {_mp_fmt(ta, 'pct')} vs {_mp_fmt(ea, 'pct')}")
    tl, el = g('team_average_life_duration'), g('enemy_team_average_life_duration')
    if tl and el:
        fight_score += (tl > el) - (tl < el)
    if fight_pts:
        if fight_score > 0:
            bullets.append(f"🔫 Gunfight: we won the fight — {' · '.join(fight_pts)}")
        elif fight_score < 0:
            bullets.append(f"🔫 Gunfight: they won the fight — {' · '.join(fight_pts)}")
        else:
            bullets.append(f"🔫 Gunfight: even fight — {' · '.join(fight_pts)}")

    # ── Advanced reads: tactical patterns behind the score, per mode ──
    # (e.g. over-rotating on Strongholds: winning zone fights, then leaving —
    # captures even or better but far fewer scoring ticks.)
    adv = []
    if 'strongholds' in mode or 'hill' in mode or 'king' in mode or 'zone' in mode:
        caps_u, caps_t = g('team_zones_stats_stronghold_captures'), g('enemy_team_zones_stats_stronghold_captures')
        tick_u, tick_t = g('team_zones_stats_stronghold_scoring_ticks'), g('enemy_team_zones_stats_stronghold_scoring_ticks')
        dk_u, ok_u = g('team_zones_stats_stronghold_defensive_kills'), g('team_zones_stats_stronghold_offensive_kills')
        if caps_u and caps_t and tick_u is not None and tick_t and tick_u < tick_t and caps_u >= caps_t * 0.9:
            adv.append(f"🌀 Over-rotating: we captured zones as often as them ({format_int(caps_u)} vs {format_int(caps_t)}) "
                       f"but held for fewer ticks ({format_int(tick_u)} vs {format_int(tick_t)}) — "
                       "winning the zone fight, then leaving it. Hold two, don't chase three.")
        elif dk_u is not None and ok_u and ok_u > 2 * max(dk_u, 1) and tick_t and tick_u is not None and tick_u < tick_t:
            adv.append(f"🌀 Fighting at THEIR zones: {format_int(ok_u)} offensive vs {format_int(dk_u)} defensive kills "
                       "while losing the tick race — nobody stayed home to hold what we took.")
    if 'flag' in mode or 'ctf' in mode:
        grabs_u, caps_u2 = g('team_capture_the_flag_stats_flag_grabs'), g('team_capture_the_flag_stats_flag_captures')
        grabs_t, caps_t2 = g('enemy_team_capture_the_flag_stats_flag_grabs'), g('enemy_team_capture_the_flag_stats_flag_captures')
        ret_u = g('team_capture_the_flag_stats_flag_returns')
        carrier_time_t = g('enemy_team_capture_the_flag_stats_time_as_flag_carrier')
        if grabs_u and grabs_u >= 3 and (caps_u2 or 0) / grabs_u < 0.34 and grabs_t and (caps_t2 or 0) / grabs_t > (caps_u2 or 0) / grabs_u:
            adv.append(f"🏃 Dying runs: {format_int(grabs_u)} flag grabs but only {format_int(caps_u2 or 0)} caps — "
                       "runs die mid-map. Escort the carrier instead of starting new fights.")
        if carrier_time_t and carrier_time_t >= 60 and (ret_u or 0) <= 2:
            adv.append(f"🚩 Slow returns: their carriers held our flag {_mp_fmt(carrier_time_t, 'mmss')} "
                       f"with only {format_int(ret_u or 0)} returns — punish the carrier first, then fight.")
    if 'oddball' in mode or 'ball' in mode:
        grabs_u = g('team_oddball_stats_skull_grabs')
        time_u, time_t = g('team_oddball_stats_time_as_skull_carrier'), g('enemy_team_oddball_stats_time_as_skull_carrier')
        long_u, long_t = g('team_oddball_stats_longest_time_as_skull_carrier'), g('enemy_team_oddball_stats_longest_time_as_skull_carrier')
        if grabs_u and time_u is not None and time_t and grabs_u >= 4:
            hold_u = time_u / grabs_u
            if hold_u < 15 and time_u < time_t:
                adv.append(f"⏳ Dropping the ball fast: avg hold {hold_u:.0f}s per grab "
                           f"({format_int(grabs_u)} grabs) — set up around the carrier BEFORE picking it up.")
        if long_u is not None and long_t and long_t >= 45 and long_t > 2 * max(long_u or 1, 1):
            adv.append(f"🛑 They got a monster hold: longest {_mp_fmt(long_t, 'mmss')} vs our {_mp_fmt(long_u, 'mmss')} — "
                       "break setups earlier, don't feed one at a time into a held position.")
    pw_u, pw_t = g('team_power_weapon_kills'), g('enemy_team_power_weapon_kills')
    if pw_u is not None and pw_t and pw_t >= pw_u + 4:
        adv.append(f"🔋 Power-weapon control lost {format_int(pw_u)}–{format_int(pw_t)} — "
                   "time the spawns; those kills are the margin.")
    tdths, edths = g('team_deaths'), g('enemy_team_deaths')
    if (tl and el and tl < el * 0.8) and tdths and edths and tdths > edths and not won:
        adv.append(f"⚰️ Trading too aggressively: shorter lives ({_mp_fmt(tl, 'secs')} vs {_mp_fmt(el, 'secs')}) "
                   f"and more deaths ({format_int(tdths)} vs {format_int(edths)}) — take fights with backup, not solo.")
    bullets.extend(adv[:3])

    # ── Lobby strength context ──
    tm, em = g('team_mmr'), g('enemy_team_mmr')
    if tm and em and abs(tm - em) >= 15:
        gap = int(round(em - tm))
        bullets.append(f"🧠 Ratings: their side was {abs(gap)} MMR {'stronger' if gap > 0 else 'weaker'} on paper")

    # ── Swing player among tracked players ──
    if len(mdf) > 1 and 'dmg_difference' in mdf.columns:
        dd = pd.to_numeric(mdf['dmg_difference'], errors='coerce')
        if dd.notna().any():
            hi, lo = dd.idxmax(), dd.idxmin()
            if won and safe_float(dd.loc[hi]) > 0:
                bullets.append(f"📌 Biggest lift: {mdf.loc[hi, 'player_gamertag']} "
                               f"({_mp_fmt(dd.loc[hi], 'signed')} damage diff)")
            elif not won and safe_float(dd.loc[lo]) < 0:
                bullets.append(f"📌 Toughest game: {mdf.loc[lo, 'player_gamertag']} "
                               f"({_mp_fmt(dd.loc[lo], 'signed')} damage diff)")

    # ── Headline: reconcile the fight with the scoreboard ──
    if won:
        if fight_score < 0:
            summary = "Stole it — lost the gunfight but won where it scores."
        elif decisive_good is False:
            summary = "Won ugly — the decisive stat went their way but we closed it out."
        else:
            summary = "Clean win — took the fight and the scoreboard."
    else:
        if fight_score > 0 and decisive_good is False:
            summary = ("Won the gunfight, lost the game — the slaying was there, "
                       "but they beat us where this mode actually scores.")
        elif fight_score > 0:
            summary = "Lost despite winning the fight — this one slipped away in the clutch moments."
        elif fight_score == 0:
            summary = "Coin-flip game that fell their way."
        else:
            summary = "Beaten straight up — outgunned and outscored."
    return {'title': 'Why we won' if won else 'Why we lost',
            'summary': summary, 'bullets': bullets}


def build_match_page(df: pd.DataFrame, match_id: str):
    """Everything captured about one match: header meta, per tracked player
    EVERY stat organized into groups plus EVERY medal, and a team-vs-team
    aggregate comparison. The lobby-wide (enemies included) scoreboard comes
    from build_full_scoreboard; this covers the deep per-player detail."""
    if df.empty or 'match_id' not in df.columns:
        return None
    mdf = df[df['match_id'].astype(str) == str(match_id)]
    if mdf.empty:
        return None
    info = mdf.iloc[0]

    dur = safe_float(info.get('duration'))
    meta = {
        'map': normalize_map_name(info.get('map')) or '—',
        'mode': clean_mode(info.get('game_type')) or '—',
        'playlist': info.get('playlist') or '',
        'date': format_date(info.get('date')),
        'duration': _mp_fmt(dur, 'mmss') if dur else '',
        'outcome': str(info.get('outcome') or '').title(),
        'outcome_class': outcome_class(info.get('outcome')),
        'team_score': _mp_fmt(info.get('team_score'), 'int'),
        'enemy_score': _mp_fmt(info.get('enemy_team_score'), 'int'),
        'team_mmr': _mp_fmt(info.get('team_mmr'), 'int'),
        'enemy_mmr': _mp_fmt(info.get('enemy_team_mmr'), 'int'),
    }

    medal_cols = [c for c in mdf.columns
                  if str(c).startswith('medal_') and c != 'medal_count']

    players = []
    for _, row in mdf.sort_values('kda', ascending=False).iterrows():
        grade = _match_grade_for_row(row) or {}
        groups = []
        for title, spec in _MP_STAT_GROUPS:
            rows = []
            for label, col, kind in spec:
                if col not in mdf.columns:
                    continue
                val = _mp_fmt(row.get(col), kind)
                if val is not None:
                    rows.append({'label': label, 'value': val})
            if rows:
                groups.append({'title': title, 'rows': rows})
        for title, prefix, spec in _MP_MODE_GROUPS:
            vals = [(label, safe_float(row.get(prefix + suffix, 0) or 0), kind)
                    for suffix, label, kind in spec if (prefix + suffix) in mdf.columns]
            if not any(v > 0 for _, v, _ in vals):
                continue  # player had no activity in this mode this game
            groups.append({'title': title, 'rows': [
                {'label': label, 'value': _mp_fmt(v, kind)} for label, v, kind in vals
            ]})
        medals = []
        for c in medal_cols:
            n = safe_int(row.get(c) or 0)
            if n > 0:
                medals.append({'name': _mp_medal_name(str(c)), 'count': n})
        medals.sort(key=lambda m: (-m['count'], m['name']))
        players.append({
            'player': row.get('player_gamertag'),
            'grade': grade.get('grade', ''),
            'grade_class': grade.get('grade_class', ''),
            'grade_tip': grade.get('grade_tip', ''),
            'outcome': str(row.get('outcome') or '').title(),
            'outcome_class': outcome_class(row.get('outcome')),
            'kda_line': (f"{format_int(row.get('kills'))}/{format_int(row.get('deaths'))}"
                         f"/{format_int(row.get('assists'))}"),
            'medal_total': format_int(row.get('medal_count')),
            'groups': groups,
            'medals': medals,
        })

    team_rows = []
    for label, suffix, kind, lower_better in _MP_TEAM_ROWS:
        tcol, ecol = f'team_{suffix}', f'enemy_team_{suffix}'
        if tcol not in mdf.columns or ecol not in mdf.columns:
            continue
        us_v, them_v = safe_float(info.get(tcol)), safe_float(info.get(ecol))
        us, them = _mp_fmt(info.get(tcol), kind), _mp_fmt(info.get(ecol), kind)
        if us is None and them is None:
            continue
        better = None
        if us is not None and them is not None and us_v != them_v:
            better = 'us' if ((us_v < them_v) if lower_better else (us_v > them_v)) else 'them'
        team_rows.append({'label': label, 'us': us or '—', 'them': them or '—',
                          'better': better})

    try:
        why = _mp_why(info, mdf, meta)
    except Exception as exc:
        logger.warning('match why-verdict failed for %s: %s', match_id, exc)
        why = None

    return {'meta': meta, 'players': players, 'team_rows': team_rows, 'why': why}


# ── "Might be cheating" check for lobby players ──
# Compares each (non-tracked) player's game and career numbers against the
# distribution of EVERY lobby player-game this site has ever captured
# (halo_match_players, bots excluded). Flags are framed as statistical
# outliers, not proof: 1 signal = ⚠️ sus, 2+ = 🚨 might be cheating.
_SUS_THRESH_TTL = 3600
_SUS_THRESH_CACHE = {'ts': 0.0, 'data': None}


def _sus_thresholds(engine):
    """Global outlier thresholds from all captured lobby player-games (cached)."""
    now = time.time()
    if _SUS_THRESH_CACHE['data'] is not None and now - _SUS_THRESH_CACHE['ts'] < _SUS_THRESH_TTL:
        return _SUS_THRESH_CACHE['data']
    sql = """
        SELECT
          percentile_cont(0.995) WITHIN GROUP (ORDER BY kda) AS kda_p999,
          percentile_cont(0.99)  WITHIN GROUP (ORDER BY kda) AS kda_p99,
          percentile_cont(0.95)  WITHIN GROUP (ORDER BY kda) AS kda_p95,
          percentile_cont(0.995) WITHIN GROUP (ORDER BY
            CASE WHEN accuracy <= 1 THEN accuracy * 100 ELSE accuracy END) AS acc_p999,
          percentile_cont(0.99)  WITHIN GROUP (ORDER BY
            CASE WHEN accuracy <= 1 THEN accuracy * 100 ELSE accuracy END) AS acc_p99,
          percentile_cont(0.995) WITHIN GROUP (ORDER BY damage_dealt) AS dmg_p999,
          percentile_cont(0.99)  WITHIN GROUP (ORDER BY damage_dealt) AS dmg_p99,
          percentile_cont(0.95)  WITHIN GROUP (ORDER BY damage_dealt) AS dmg_p95,
          percentile_cont(0.95)  WITHIN GROUP (ORDER BY
            CASE WHEN accuracy <= 1 THEN accuracy * 100 ELSE accuracy END) AS acc_p95,
          COUNT(*) AS n
        FROM halo_match_players
        WHERE COALESCE(is_bot, FALSE) = FALSE AND kda IS NOT NULL
          AND playlist ILIKE :pl
    """
    career_sql = """
        SELECT
          percentile_cont(0.999) WITHIN GROUP (ORDER BY avg_kda) AS ckda_p999,
          percentile_cont(0.99)  WITHIN GROUP (ORDER BY avg_kda) AS ckda_p99,
          percentile_cont(0.999) WITHIN GROUP (ORDER BY avg_acc) AS cacc_p999,
          percentile_cont(0.99)  WITHIN GROUP (ORDER BY avg_acc) AS cacc_p99
        FROM (
          SELECT AVG(kda) AS avg_kda,
                 AVG(CASE WHEN accuracy <= 1 THEN accuracy * 100 ELSE accuracy END) AS avg_acc
          FROM halo_match_players
          WHERE COALESCE(is_bot, FALSE) = FALSE AND kda IS NOT NULL
            AND playlist ILIKE :pl
          GROUP BY player_xuid HAVING COUNT(*) >= 3
        ) t
    """
    try:
        with engine.connect() as conn:
            row = conn.execute(text(sql), {'pl': '%rank%'}).mappings().first() or {}
            crow = conn.execute(text(career_sql), {'pl': '%rank%'}).mappings().first() or {}
        data = {
            'n': safe_int(row.get('n')),
            'kda_p999': safe_float(row.get('kda_p999')),
            'kda_p99': safe_float(row.get('kda_p99')),
            'kda_p95': safe_float(row.get('kda_p95')),
            'acc_p999': safe_float(row.get('acc_p999')),
            'acc_p99': safe_float(row.get('acc_p99')),
            'dmg_p999': safe_float(row.get('dmg_p999')),
            'dmg_p99': safe_float(row.get('dmg_p99')),
            'dmg_p95': safe_float(row.get('dmg_p95')),
            'acc_p95': safe_float(row.get('acc_p95')),
            'ckda_p999': safe_float(crow.get('ckda_p999')),
            'ckda_p99': safe_float(crow.get('ckda_p99')),
            'cacc_p999': safe_float(crow.get('cacc_p999')),
            'cacc_p99': safe_float(crow.get('cacc_p99')),
        }
    except SQLAlchemyError:
        data = None
    _SUS_THRESH_CACHE['ts'] = now
    _SUS_THRESH_CACHE['data'] = data
    return data


def _sus_careers(engine, match_id):
    """Career history (every game in OUR captured lobbies, chronological) for
    the players in this match — powers the repeat-offender, smurf, and
    suddenly-got-good signals."""
    sql = """
        SELECT player_xuid, match_date, kda,
               CASE WHEN accuracy <= 1 THEN accuracy * 100 ELSE accuracy END AS acc,
               csr
        FROM halo_match_players
        WHERE COALESCE(is_bot, FALSE) = FALSE AND kda IS NOT NULL
          AND playlist ILIKE :pl
          AND player_xuid IN (
            SELECT player_xuid FROM halo_match_players WHERE match_id = :mid)
        ORDER BY player_xuid, match_date
    """
    out = {}
    try:
        with engine.connect() as conn:
            for r in conn.execute(text(sql), {'mid': match_id, 'pl': '%rank%'}):
                m = r._mapping
                e = out.setdefault(str(m['player_xuid']),
                                   {'n': 0, 'kdas': [], 'accs': []})
                e['n'] += 1
                e['kdas'].append(safe_float(m['kda']))
                if m['acc'] is not None:
                    e['accs'].append(safe_float(m['acc']))
    except SQLAlchemyError:
        return {}
    for e in out.values():
        e['avg_kda'] = sum(e['kdas']) / e['n'] if e['n'] else 0.0
        e['avg_acc'] = sum(e['accs']) / len(e['accs']) if e['accs'] else 0.0
    return out


def _sus_eval(r, acc_pct, th, career, lobby_avg_csr):
    """Evaluate one (non-tracked) lobby player. Returns (signals, kind):
    signals = human-readable outlier findings; kind = 'cheat' | 'smurf' |
    'sus' | '' (chip severity). History across our lobbies separates
    'suddenly got good' (fits new cheats) from 'always been this good'
    (fits a smurf or a genuinely strong player)."""
    signals = []
    kda = safe_float(r.get('kda'))
    dmg = safe_float(r.get('damage_dealt'))
    csr = safe_float(r.get('csr'))
    n_all = th.get('n') or 0
    hard = 0  # this-game statistical outliers

    if th.get('acc_p999') and acc_pct >= max(th['acc_p999'], 65):
        hard += 1
        signals.append(f"{acc_pct:.0f}% accuracy this game — above the top 0.5% "
                       f"of all {format_int(n_all)} player-games we've captured (top 0.5%)")
    if th.get('kda_p999') and kda >= max(th['kda_p999'], 8):
        hard += 1
        signals.append(f"{kda:.1f} KDA this game — top 0.5% outlier across every "
                       "lobby we've recorded")
    if th.get('dmg_p999') and dmg >= th['dmg_p999'] and dmg > 0:
        hard += 1
        signals.append(f"{format_int(dmg)} damage — top 0.5% of any player-game "
                       "we've captured")

    # JOINT outlier: ~top-1% on several stats AT ONCE. Each alone slips
    # under the single-stat bars, but together the odds multiply — this is
    # what catches the "39 kills, 67% acc, 10k damage, nothing flagged" game.
    strong, soft = 0, []
    if th.get('kda_p95') and kda >= max(th['kda_p95'], 6):
        soft.append(f"{kda:.1f} KDA")
        strong += bool(th.get('kda_p99')) and kda >= th['kda_p99']
    if th.get('acc_p95') and acc_pct >= max(th['acc_p95'], 58):
        soft.append(f"{acc_pct:.0f}% accuracy")
        strong += bool(th.get('acc_p99')) and acc_pct >= th['acc_p99']
    if th.get('dmg_p95') and dmg >= th['dmg_p95'] and dmg > 0:
        soft.append(f"{format_int(dmg)} damage")
        strong += bool(th.get('dmg_p99')) and dmg >= th['dmg_p99']
    if strong >= 2 or len(soft) == 3 or (strong >= 1 and len(soft) >= 2):
        hard += 1 + (strong >= 2)
        signals.append(f"elite on {len(soft)} fronts at once — {' + '.join(soft)} — "
                       "each alone is top 1-5% of every ranked player-game we've "
                       "captured; hitting them together is far rarer")

    # Smurf pattern: elite output far below the lobby's rating
    smurf = (csr > 0 and lobby_avg_csr and lobby_avg_csr - csr >= 150
             and th.get('kda_p95') and kda >= th['kda_p95'])
    if smurf:
        signals.append(f"crushing the lobby at {format_int(csr)} CSR "
                       f"({format_int(lobby_avg_csr - csr)} below the lobby average) "
                       "— smurf / alt-account pattern")

    # Ringer context (mitigating): far ABOVE the lobby's rating — a monster
    # game from a much higher-ranked player is expected, not suspicious.
    notes = []
    if (csr > 0 and lobby_avg_csr and csr - lobby_avg_csr >= 150
            and th.get('kda_p95') and kda >= th['kda_p95']):
        notes.append(f"context: rated {format_int(csr)} CSR, "
                       f"{format_int(csr - lobby_avg_csr)} ABOVE the lobby average — "
                       "could simply be a much higher-ranked player in this lobby")

    # Career across every game of theirs we've captured
    c = career or {}
    cn = safe_int(c.get('n'))
    always_good = False
    spike = False
    if cn >= 3:
        cavg_kda = safe_float(c.get('avg_kda'))
        cavg_acc = safe_float(c.get('avg_acc'))
        if th.get('ckda_p99') and cavg_kda >= th['ckda_p99']:
            hard += 1
            signals.append(f"averages {cavg_kda:.1f} KDA over {cn} games in our "
                           "lobbies — top 1% of every player we've ever faced")
        if th.get('cacc_p99') and cavg_acc >= max(th['cacc_p99'], 58):
            hard += 1
            signals.append(f"averages {cavg_acc:.0f}% accuracy over {cn} games vs us "
                           "— top 1% of everyone we've faced")
        always_good = bool(th.get('ckda_p99')) and cavg_kda >= th['ckda_p99']
    # History trend: did they SUDDENLY get good, or were they always good?
    kdas = c.get('kdas') or []
    if len(kdas) >= 6:
        half = len(kdas) // 2
        early = sum(kdas[:half]) / half
        late = sum(kdas[half:]) / (len(kdas) - half)
        if late >= max(early, 0.5) * 1.75 and late >= 4:
            spike = True
            signals.append(f"📈 suddenly got good: averaged {early:.1f} KDA over their "
                           f"first {half} games vs us, {late:.1f} over their last "
                           f"{len(kdas) - half} — a jump like that fits new cheats "
                           "or account sharing")
        elif always_good:
            signals.append(f"has played at this level in all {cn} games we've seen "
                           "— long-term smurf or just a genuinely strong player, "
                           "less likely new cheats")

    # Verdict chip
    if smurf and hard < 2 and not spike:
        kind = 'smurf'
    elif hard >= 2 or (spike and (hard or smurf)):
        kind = 'cheat'
    elif signals:
        kind = 'sus'
    else:
        kind = ''
    return (signals + notes) if kind else [], kind


_SUS_KIND_RANK = {'cheat': 3, 'smurf': 2, 'sus': 1, '': 0}


def _sus_flags_for_matches(engine, match_ids):
    """Automatic cheat-check across MANY matches at once (one rows query +
    one careers query total) — powers the ⚠️ flags on the dashboards'
    game-by-game breakdown without opening each match page. Returns
    {match_id: {'kind': worst verdict, 'names': [...], 'tip': ...}}."""
    match_ids = [str(m) for m in match_ids if m]
    th = _sus_thresholds(engine)
    if not th or not match_ids:
        return {}
    sql = text("""
        SELECT match_id, gamertag, player_xuid, is_tracked, kda, accuracy,
               damage_dealt, csr
        FROM halo_match_players WHERE match_id IN :mids
    """).bindparams(bindparam('mids', expanding=True))
    try:
        with engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(sql, {'mids': match_ids})]
    except SQLAlchemyError:
        return {}
    if not rows:
        return {}
    # Careers for every player seen across these matches, one query
    career_sql = text("""
        SELECT player_xuid, match_date, kda,
               CASE WHEN accuracy <= 1 THEN accuracy * 100 ELSE accuracy END AS acc
        FROM halo_match_players
        WHERE COALESCE(is_bot, FALSE) = FALSE AND kda IS NOT NULL
          AND playlist ILIKE :pl
          AND player_xuid IN :xuids
        ORDER BY player_xuid, match_date
    """).bindparams(bindparam('xuids', expanding=True))
    xuids = sorted({str(r['player_xuid']) for r in rows if r.get('player_xuid')})
    careers = {}
    try:
        with engine.connect() as conn:
            for r in conn.execute(career_sql, {'pl': '%rank%', 'xuids': xuids}):
                m = r._mapping
                e = careers.setdefault(str(m['player_xuid']),
                                       {'n': 0, 'kdas': [], 'accs': []})
                e['n'] += 1
                e['kdas'].append(safe_float(m['kda']))
                if m['acc'] is not None:
                    e['accs'].append(safe_float(m['acc']))
    except SQLAlchemyError:
        careers = {}
    for e in careers.values():
        e['avg_kda'] = sum(e['kdas']) / e['n'] if e['n'] else 0.0
        e['avg_acc'] = sum(e['accs']) / len(e['accs']) if e['accs'] else 0.0

    by_match: dict = {}
    for r in rows:
        by_match.setdefault(str(r['match_id']), []).append(r)
    out = {}
    for mid, mrows in by_match.items():
        csrs = [safe_float(r.get('csr')) for r in mrows if safe_float(r.get('csr')) > 0]
        lobby_avg = sum(csrs) / len(csrs) if csrs else 0
        worst, names, tips = '', [], []
        for r in mrows:
            if r.get('is_tracked'):
                continue
            acc = safe_float(r.get('accuracy'))
            if acc <= 1.0:
                acc *= 100
            sigs, kind = _sus_eval(r, acc, th, careers.get(str(r.get('player_xuid'))), lobby_avg)
            if kind:
                names.append(r.get('gamertag') or '?')
                tips.append(f"{r.get('gamertag')}: {sigs[0] if sigs else kind}")
                if _SUS_KIND_RANK[kind] > _SUS_KIND_RANK[worst]:
                    worst = kind
        if worst:
            out[mid] = {'kind': worst, 'names': names, 'tip': ' · '.join(tips)}
    return out


def build_full_scoreboard(engine, match_id):
    """Two-team scoreboard for a match from halo_match_players, if captured."""
    sql = """
        SELECT gamertag, player_xuid, team_id, outcome, is_tracked, kills, deaths,
               assists, kda, accuracy, damage_dealt, csr, playlist, map, match_date
        FROM halo_match_players WHERE match_id = :mid
        ORDER BY team_id, kda DESC NULLS LAST
    """
    try:
        with engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(text(sql), {'mid': match_id})]
    except SQLAlchemyError:
        return None
    if not rows:
        return None
    # Lobby-wide KDA ranking so each player gets a "beat the lobby" standing.
    n = len(rows)
    kdas = [safe_float(r.get('kda')) for r in rows]

    def _pct_rank(v):
        below = sum(1 for x in kdas if x < v)
        equal = sum(1 for x in kdas if x == v)
        return (below + 0.5 * equal) / n * 100 if n else 0
    rank_of = {}
    for pos, i in enumerate(sorted(range(n), key=lambda i: kdas[i], reverse=True), 1):
        rank_of[i] = pos

    # Cheat-suspicion context: global outlier thresholds + careers of this
    # lobby's players (both cheap: thresholds cached 1h, careers one query).
    sus_th = _sus_thresholds(engine)
    sus_careers = _sus_careers(engine, match_id) if sus_th else {}
    _csrs = [safe_float(r.get('csr')) for r in rows if safe_float(r.get('csr')) > 0]
    lobby_avg_csr = sum(_csrs) / len(_csrs) if _csrs else 0

    teams = {}
    team_tot = {}
    meta = {'map': '', 'playlist': '', 'date': ''}
    for i, r in enumerate(rows):
        meta['map'] = normalize_map_name(r.get('map')) or meta['map']
        meta['playlist'] = r.get('playlist') or meta['playlist']
        if r.get('match_date'):
            meta['date'] = format_date(r.get('match_date'))
        acc = safe_float(r.get('accuracy'))
        if acc <= 1.0:
            acc *= 100
        grade = compute_match_grade(
            kda=r.get('kda'), accuracy=acc, dmg_dealt=r.get('damage_dealt'),
            outcome=r.get('outcome'),
        ) or {}
        tid = int(r.get('team_id') or 0)
        k, d, a = safe_int(r.get('kills')), safe_int(r.get('deaths')), safe_int(r.get('assists'))
        # Cheat check — enemies / randoms only, never the tracked squad
        sus, sus_kind = [], ''
        if sus_th and not r.get('is_tracked'):
            sus, sus_kind = _sus_eval(r, acc, sus_th,
                                      sus_careers.get(str(r.get('player_xuid'))),
                                      lobby_avg_csr)
        teams.setdefault(tid, []).append({
            'player': r.get('gamertag') or '',
            'xuid': str(r.get('player_xuid') or ''),
            'is_tracked': r.get('is_tracked'),
            'sus_kind': sus_kind,
            'sus_signals': sus,
            'sus_tip': ' · '.join(sus),
            'outcome': str(r.get('outcome') or '').title(),
            'outcome_class': outcome_class(r.get('outcome')),
            'kills': format_int(r.get('kills')),
            'deaths': format_int(r.get('deaths')),
            'assists': format_int(r.get('assists')),
            'kda': format_float(r.get('kda'), 2),
            'accuracy': f'{acc:.0f}%',
            'damage': format_int(r.get('damage_dealt')),
            'csr': format_int(r.get('csr')) if safe_float(r.get('csr')) > 0 else '',
            'grade': grade.get('grade', ''),
            'grade_class': grade.get('grade_class', ''),
            'grade_tip': grade.get('grade_tip', ''),
            'lobby_rank': rank_of[i],
            'lobby_n': n,
            'lobby_pct': round(_pct_rank(kdas[i])),
        })
        tt = team_tot.setdefault(tid, {'kills': 0, 'deaths': 0, 'assists': 0, 'kda_sum': 0.0, 'n': 0})
        tt['kills'] += k; tt['deaths'] += d; tt['assists'] += a
        tt['kda_sum'] += kdas[i]; tt['n'] += 1
    team_blocks = []
    for tid in sorted(teams.keys()):
        members = teams[tid]
        tt = team_tot[tid]
        won = any(m['outcome'].lower() == 'win' for m in members)
        team_blocks.append({
            'team_id': tid, 'won': won, 'players': members,
            'total_kills': tt['kills'], 'total_deaths': tt['deaths'], 'total_assists': tt['assists'],
            'avg_kda': format_float(tt['kda_sum'] / tt['n'] if tt['n'] else 0, 2),
        })
    return {'teams': team_blocks, 'meta': meta}


def _warm_caches():
    """Background pre-warmer: keep EVERY heavy page cache hot so a user never
    lands on a cold build. Fires internal GETs (via test_client) to each page on
    a loop — each request populates that route's caches ahead of real traffic.
    get_cached_page_payload only rebuilds what's actually stale, so idle cycles
    are cheap; right after a new game it rebuilds proactively in the background."""
    import time as _t
    import urllib.parse as _up
    interval = int(os.getenv('HALO_WARM_INTERVAL', '20'))
    _t.sleep(int(os.getenv('HALO_WARM_STARTUP_DELAY', '6')))  # let the DB come up first
    # EVERY page ("super snappy across the board"): with stale-while-revalidate
    # in get_cached_page_payload a warm GET is ~free when fresh and merely kicks
    # a background single-flight rebuild when stale — so covering all pages
    # costs idle cycles almost nothing while guaranteeing no visitor ever
    # blocks on a cold or stale build.
    hot_paths = ['/', '/combat', '/sessions', '/live', '/report', '/winning', '/lifetime']
    rest_paths = ['/maps', '/veto', '/nemesis', '/trends', '/climb', '/heatmap',
                  '/recap', '/hall', '/leaderboard', '/highlights', '/medals',
                  '/compare', '/weapons', '/advanced', '/coach', '/insights',
                  '/session-card.png']
    cycle = 0
    while True:
        try:
            cnt = count_cache.get()
            # Warm every cycle so no cache ever expires uncovered (cheap on a hit —
            # get_cached_page_payload only rebuilds what's actually stale).
            if cnt > 0:
                try:
                    players = unique_sorted(cache.get()['player_gamertag'])
                except Exception:
                    players = []
                paths = list(hot_paths) + [f'/player/{_up.quote(str(p))}' for p in players]
                # Long-tail pages every other cycle — their SWR rebuilds are
                # background threads anyway; this just keeps them from expiring.
                if cycle % 2 == 0:
                    paths += rest_paths
                with app.test_client() as c:
                    for path in paths:
                        try:
                            c.get(path)
                        except Exception:
                            pass
            cycle += 1
        except Exception as exc:
            logger.warning('cache warmer cycle error: %s', exc)
        _t.sleep(interval)


if __name__ == "__main__":
    port = int(os.getenv('HALO_WEB_PORT', '8091'))
    threads = int(os.getenv('HALO_WEB_THREADS', '8'))
    if os.getenv('HALO_WARM_CACHE', '1') not in ('0', 'false', 'no'):
        import threading as _threading
        _threading.Thread(target=_warm_caches, daemon=True, name='cache-warmer').start()
        print("🔥 Cache warmer thread started")
    try:
        from waitress import serve
        print(f"🚀 Starting Waitress on port {port} with {threads} threads")
        serve(app, host='0.0.0.0', port=port, threads=threads,
              channel_timeout=120, recv_bytes=65536,
              send_bytes=65536)
    except ImportError:
        print("⚠️ Waitress not installed, falling back to Flask dev server")
        app.run(host='0.0.0.0', port=port, debug=False)
