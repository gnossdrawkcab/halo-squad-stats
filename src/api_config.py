"""Shared Azure API credentials + in-app Xbox Live OAuth helpers.

Credential load priority (highest first):
  1. api_config.json in the data dir (written by the /setup page)
  2. HALO_CLIENT_ID / HALO_CLIENT_SECRET env vars (optional/advanced)
  3. nothing — the /setup page walks the user through creating an Azure app

Callers must read credentials lazily (call get_credentials() at use time, not
import time) so credentials saved via /setup apply without a restart.

This module also implements the in-app "Authorize with Xbox Live" flow used by
the /setup page: build the Microsoft OAuth authorize URL, then exchange the
pasted redirect URL/code for the full Halo token chain and write tokens.json.
The token exchange uses the spnkr library's auth helpers — imported lazily
inside functions, because the webapp test environment doesn't ship spnkr.

tokens.json is written in exactly the shape src/stats.py and src/entrypoint.py
expect: the raw OAuth response (access_token, refresh_token, expires_in, ...)
plus user_token, xsts_token, spartan_token, clearance_token, xuid, expires_at.
"""
import json
import logging
import os
import time
import urllib.parse

from halo_paths import data_path

logger = logging.getLogger(__name__)

API_CONFIG_PATH = data_path("api_config.json")
TOKENS_PATH = data_path("tokens.json")

# Same scopes spnkr uses (spnkr.auth.oauth.DEFAULT_SCOPES).
SCOPES = "Xboxlive.signin Xboxlive.offline_access"


# ── Credentials ──────────────────────────────────────────────────────────────

def stored_credentials() -> dict | None:
    """Credentials from api_config.json only, or None when absent/invalid.

    Server-side use only — never render the secret back into a page.
    """
    try:
        if not API_CONFIG_PATH.exists():
            return None
        raw = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("api_config_read_failed path=%s error=%s", API_CONFIG_PATH, exc)
        return None
    if not isinstance(raw, dict):
        return None
    client_id = str(raw.get("client_id") or "").strip()
    client_secret = str(raw.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        return None
    return {"client_id": client_id, "client_secret": client_secret}


def get_credentials() -> tuple[str | None, str | None]:
    """(client_id, client_secret) — api_config.json first, env fallback."""
    cfg = stored_credentials()
    if cfg:
        return cfg["client_id"], cfg["client_secret"]
    return (os.getenv("HALO_CLIENT_ID") or None,
            os.getenv("HALO_CLIENT_SECRET") or None)


def credentials_configured() -> bool:
    client_id, client_secret = get_credentials()
    return bool(client_id and client_secret)


def credentials_source() -> str | None:
    """'file' | 'env' | None — where the active credentials come from."""
    if stored_credentials():
        return "file"
    if os.getenv("HALO_CLIENT_ID") and os.getenv("HALO_CLIENT_SECRET"):
        return "env"
    return None


def save_credentials(client_id: str, client_secret: str) -> None:
    """Persist Azure app credentials to api_config.json (0600, atomic-ish)."""
    client_id = str(client_id or "").strip()
    client_secret = str(client_secret or "").strip()
    if not client_id or not client_secret:
        raise ValueError("client_id and client_secret are both required")
    tmp = API_CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"client_id": client_id,
                               "client_secret": client_secret}, indent=2),
                   encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(API_CONFIG_PATH)
    logger.info("api_config_saved client_id=%s", client_id[:8] + "…")


def get_redirect_uri() -> str:
    return os.getenv("HALO_REDIRECT_URI", "http://localhost")


# ── OAuth authorize URL ──────────────────────────────────────────────────────

def build_authorize_url() -> str | None:
    """Microsoft OAuth authorize URL for the configured client, or None.

    Uses spnkr.auth.oauth.generate_authorization_url when spnkr is available;
    otherwise builds the identical URL locally (keeps GET /setup working in
    environments without spnkr, e.g. the test env).
    """
    client_id, client_secret = get_credentials()
    if not client_id:
        return None
    redirect_uri = get_redirect_uri()
    try:
        from spnkr.auth.app import AzureApp
        from spnkr.auth.oauth import generate_authorization_url
        return generate_authorization_url(
            AzureApp(client_id, client_secret or "", redirect_uri))
    except ImportError:
        params = {
            "client_id": client_id,
            "response_type": "code",
            "approval_prompt": "auto",
            "scope": SCOPES,
            "redirect_uri": redirect_uri,
        }
        return ("https://login.live.com/oauth20_authorize.srf?"
                + urllib.parse.urlencode(params))


def extract_auth_code(raw: str) -> str:
    """Pull the authorization code out of a pasted redirect URL, or pass a
    bare code straight through."""
    raw = str(raw or "").strip()
    if not raw:
        return ""
    if "code=" in raw:
        try:
            parsed = urllib.parse.urlparse(raw)
            for part in (parsed.query, parsed.fragment, raw.split("?", 1)[-1]):
                code = (urllib.parse.parse_qs(part).get("code") or [""])[0].strip()
                if code:
                    return code
        except ValueError:
            pass
    return raw


# ── Code → tokens.json ───────────────────────────────────────────────────────

def _xuid_from_claims(*payloads) -> str | None:
    """Extract a numeric XUID from XSTS/user-token DisplayClaims payloads."""
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for claim in (payload.get("DisplayClaims") or {}).get("xui") or []:
            if isinstance(claim, dict):
                xid = claim.get("xid") or claim.get("id")
                if xid:
                    return str(xid)
    return None


async def _mint_tokens(client_id: str, client_secret: str, redirect_uri: str,
                       code: str) -> dict:
    """Exchange an authorization code for the full Halo token chain via spnkr.

    spnkr functions used (spnkr.auth.*): oauth.request_oauth_token,
    xbox.request_user_token, xbox.request_xsts_token (Halo Waypoint audience),
    halo.request_spartan_token, halo.request_clearance_token.
    """
    from aiohttp import ClientSession
    from spnkr.auth.app import AzureApp
    from spnkr.auth.core import XSTS_V3_HALO_AUDIENCE
    from spnkr.auth.halo import request_clearance_token, request_spartan_token
    from spnkr.auth.oauth import request_oauth_token
    from spnkr.auth.xbox import request_user_token, request_xsts_token

    app = AzureApp(client_id, client_secret, redirect_uri)
    async with ClientSession() as session:
        oauth = await request_oauth_token(session, code, app)
        user = await request_user_token(session, oauth.access_token)
        if "Token" not in user.raw:
            raise RuntimeError(f"Xbox user token request failed: {user.raw}")
        xsts = await request_xsts_token(session, user.token, XSTS_V3_HALO_AUDIENCE)
        if "Token" not in xsts.raw:
            raise RuntimeError(f"XSTS token request failed: {xsts.raw}")
        spartan = await request_spartan_token(session, xsts.token)
        if "SpartanToken" not in spartan.raw:
            raise RuntimeError(f"Spartan token request failed: {spartan.raw}")
        clearance = await request_clearance_token(session, spartan.token)

    # Exact tokens.json shape stats.py / entrypoint.py / auth.py expect.
    tokens = dict(oauth.raw)  # access_token, refresh_token, expires_in, ...
    tokens["user_token"] = user.token
    tokens["xsts_token"] = xsts.token
    tokens["spartan_token"] = spartan.token
    clearance_token = (clearance.raw or {}).get("FlightConfigurationId")
    if clearance_token:
        tokens["clearance_token"] = clearance_token
    else:
        logger.warning("clearance_token_missing payload=%s", clearance.raw)
    xuid = _xuid_from_claims(xsts.raw, user.raw)
    if xuid:
        tokens["xuid"] = xuid
    tokens["expires_at"] = time.time() + float(tokens.get("expires_in") or 3600)
    return tokens


def save_tokens(tokens: dict) -> None:
    with open(TOKENS_PATH, "w") as f:
        json.dump(tokens, f, indent=4)
    try:
        os.chmod(TOKENS_PATH, 0o600)
    except OSError:
        pass


def mint_tokens_from_code(raw_code: str) -> dict:
    """Full in-app OAuth exchange: pasted redirect URL/code → tokens.json.

    Returns the token dict on success; raises with a user-facing message on
    failure. Synchronous wrapper — runs the async spnkr exchange to completion.
    """
    client_id, client_secret = get_credentials()
    if not client_id or not client_secret:
        raise RuntimeError("API credentials are not configured yet — complete step 1 first.")
    code = extract_auth_code(raw_code)
    if not code:
        raise RuntimeError("No authorization code found — paste the full "
                           "localhost URL (or just the code= value) from your browser.")
    import asyncio
    tokens = asyncio.run(_mint_tokens(client_id, client_secret,
                                      get_redirect_uri(), code))
    if not tokens.get("xuid"):
        # Best-effort fallback so the scraper's clearance calls have an XUID.
        try:
            from auth import resolve_fallback_xuid
            xuid = resolve_fallback_xuid(tokens)
            if xuid:
                tokens["xuid"] = xuid
        except Exception:
            pass
    save_tokens(tokens)
    logger.info("tokens_json_minted xuid_present=%s clearance_present=%s",
                bool(tokens.get("xuid")), bool(tokens.get("clearance_token")))
    return tokens


# ── Token status (for the /setup page) ───────────────────────────────────────

def tokens_status() -> dict:
    """{'present': bool, 'complete': bool, 'expired': bool} for tokens.json."""
    status = {"present": False, "complete": False, "expired": False}
    try:
        if not TOKENS_PATH.exists():
            return status
        tokens = json.loads(TOKENS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return status
    if not isinstance(tokens, dict):
        return status
    status["present"] = True
    status["complete"] = all(tokens.get(k) for k in
                             ("access_token", "refresh_token", "spartan_token"))
    status["expired"] = time.time() > float(tokens.get("expires_at") or 0)
    return status
