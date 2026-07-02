"""Direct Twitch Helix live-status client — removes the MultiTwitch dependency.

The /live embeds are plain player.twitch.tv iframes (browser → Twitch direct),
so the ONLY thing the server needs is "which channels are live". This asks
Twitch Helix itself using an app access token (client-credentials grant),
so the MultiTwitch app can be stopped without breaking any halo live feature.

Config (env):
  TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET  app creds (same ones MultiTwitch uses)

Returns the same shape MultiTwitch's /api/live used:
  {login: {live, streamTitle, streamGame, viewers}}
Best-effort everywhere: any failure returns {} / None, never raises.
"""
import logging
import os
import threading
import time

import requests

logger = logging.getLogger("twitch_live")

_TOKEN = {'value': '', 'expires': 0.0}
_LOCK = threading.Lock()


def creds_configured() -> bool:
    return bool(os.getenv('TWITCH_CLIENT_ID') and os.getenv('TWITCH_CLIENT_SECRET'))


def _get_token() -> str:
    """App access token, cached until ~1h before expiry (tokens last ~60 days)."""
    now = time.time()
    if _TOKEN['value'] and now < _TOKEN['expires'] - 3600:
        return _TOKEN['value']
    with _LOCK:
        if _TOKEN['value'] and now < _TOKEN['expires'] - 3600:
            return _TOKEN['value']
        try:
            r = requests.post('https://id.twitch.tv/oauth2/token', data={
                'client_id': os.getenv('TWITCH_CLIENT_ID', ''),
                'client_secret': os.getenv('TWITCH_CLIENT_SECRET', ''),
                'grant_type': 'client_credentials',
            }, timeout=8)
            r.raise_for_status()
            j = r.json()
            _TOKEN['value'] = j.get('access_token', '')
            _TOKEN['expires'] = now + float(j.get('expires_in', 0))
        except Exception as e:
            logger.warning('twitch_token_failed error=%s', e)
            _TOKEN['value'] = ''
    return _TOKEN['value']


def fetch_live_map(logins: list) -> dict:
    """{login: {live, streamTitle, streamGame, viewers}} for the given channel
    logins, straight from Helix /streams. Only live channels come back from the
    API; everyone else is marked live=False. {} on any failure."""
    logins = [str(c).lower() for c in logins if c]
    if not logins or not creds_configured():
        return {}
    token = _get_token()
    if not token:
        return {}
    try:
        r = requests.get(
            'https://api.twitch.tv/helix/streams',
            params=[('user_login', c) for c in logins[:100]],
            headers={'Client-Id': os.getenv('TWITCH_CLIENT_ID', ''),
                     'Authorization': f'Bearer {token}'},
            timeout=6)
        if r.status_code == 401:          # token revoked/expired early → one retry
            _TOKEN['value'] = ''
            token = _get_token()
            if not token:
                return {}
            r = requests.get(
                'https://api.twitch.tv/helix/streams',
                params=[('user_login', c) for c in logins[:100]],
                headers={'Client-Id': os.getenv('TWITCH_CLIENT_ID', ''),
                         'Authorization': f'Bearer {token}'},
                timeout=6)
        r.raise_for_status()
        data = r.json().get('data', [])
    except Exception as e:
        logger.warning('twitch_streams_failed error=%s', e)
        return {}
    out = {c: {'live': False} for c in logins}
    for s in data:
        login = str(s.get('user_login', '')).lower()
        out[login] = {
            'live': True,
            'streamTitle': s.get('title', ''),
            'streamGame': s.get('game_name', ''),
            'viewers': s.get('viewer_count'),
        }
    return out
