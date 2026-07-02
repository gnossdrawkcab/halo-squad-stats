import requests
import json
import time
import os
import urllib.parse
from halo_paths import data_path

# Azure AD app credentials are read lazily via api_config (api_config.json in
# the data dir — written by the /setup page — with HALO_CLIENT_ID /
# HALO_CLIENT_SECRET env vars as fallback), so credentials saved in-app apply
# without a restart and importing this module never requires them.
from api_config import get_credentials, get_redirect_uri

SCOPES = 'XboxLive.signin XboxLive.offline_access'
TOKEN_FILE = data_path("tokens.json")
REQUEST_TIMEOUT = int(os.getenv("HALO_HTTP_TIMEOUT", "20"))


def require_credentials():
    """(client_id, client_secret), or raise with a pointer to /setup."""
    client_id, client_secret = get_credentials()
    if not client_id or not client_secret:
        raise RuntimeError(
            "Azure API credentials are not configured. Save them on the app's "
            "/setup page (recommended), or set HALO_CLIENT_ID/HALO_CLIENT_SECRET."
        )
    return client_id, client_secret


def redact_secret(value, keep: int = 6) -> str:
    if not value:
        return "not available"
    text = str(value)
    if len(text) <= keep:
        return "[redacted]"
    return f"{text[:keep]}...[redacted]"


def redact_token_payload(payload):
    if isinstance(payload, dict):
        redacted = {}
        for key, value in payload.items():
            if any(word in key.lower() for word in ("token", "secret", "authorization", "flightconfigurationid")):
                redacted[key] = redact_secret(value)
            else:
                redacted[key] = redact_token_payload(value)
        return redacted
    if isinstance(payload, list):
        return [redact_token_payload(item) for item in payload]
    return payload


def resolve_fallback_xuid(tokens: dict | None = None) -> str | None:
    existing_xuid = str((tokens or {}).get("xuid") or "").strip()
    if existing_xuid:
        return existing_xuid

    env_xuid = os.getenv("HALO_XUID_FALLBACK", "").strip()
    if env_xuid:
        return env_xuid

    # Shared roster helper: players.json (from /setup) > HALO_TRACKED_PLAYERS > []
    try:
        from players import load_players
        for player in load_players():
            xuid = str(player.get("xuid") or "").strip()
            if xuid:
                return xuid
    except Exception:
        pass
    return None

def load_tokens():
    """Load saved tokens from the file"""
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, 'r') as f:
            return json.load(f)
    return None

def save_tokens(tokens):
    """Save tokens to the file"""
    with open(TOKEN_FILE, 'w') as f:
        json.dump(tokens, f, indent=4)
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass

def refresh_tokens(refresh_token):
    """Use the refresh token to get a new access token and user token"""
    client_id, client_secret = require_credentials()
    token_resp = requests.post(
        "https://login.live.com/oauth20_token.srf",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "redirect_uri": get_redirect_uri(),
            "scope": SCOPES,
        },
        timeout=REQUEST_TIMEOUT,
    )
    token_resp.raise_for_status()
    tokens = token_resp.json()
    return tokens

def authenticate():
    """Authenticate the user if no tokens exist"""
    try:
        client_id, client_secret = require_credentials()
    except RuntimeError as exc:
        print(f"❌ {exc}")
        return None
    redirect_uri = get_redirect_uri()

    # Step 1: Build Microsoft login URL
    auth_url = (
        "https://login.live.com/oauth20_authorize.srf?"
        + urllib.parse.urlencode({
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
        })
    )

    print("🔗 Visit this URL and log in with your Microsoft account:")
    print(auth_url)
    print("\n📋 After logging in, copy the 'code' from the URL and paste it below.")
    
    try:
        from api_config import extract_auth_code
        auth_code = extract_auth_code(input("🔑 Enter authorization code (or the full localhost URL): "))
    except EOFError:
        print("\n❌ ERROR: Running in non-interactive mode (Docker container).")
        print("📌 Use the in-app flow instead: open the web UI's /setup page and")
        print("   complete the 'Authorize with Xbox Live' step — it creates")
        print("   tokens.json in the data dir for you.")
        print("   (Alternative: run 'python src/auth.py' on your local machine and")
        print("   mount the resulting tokens.json into the container at /data.)")
        return None

    # Step 2: Exchange code for access + refresh token
    token_resp = requests.post(
        "https://login.live.com/oauth20_token.srf",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": auth_code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
        },
        timeout=REQUEST_TIMEOUT,
    )
    token_resp.raise_for_status()
    tokens = token_resp.json()

    access_token = tokens["access_token"]
    refresh_token = tokens["refresh_token"]
    print(f"✅ Access Token: {redact_secret(access_token)}")
    print(f"🔁 Refresh Token: {redact_secret(refresh_token)}")

    # Save tokens to the file
    save_tokens(tokens)

    return tokens

def get_user_token(access_token, tokens: dict | None = None):
    """Get the user token and XUID"""
    user_token_resp = requests.post(
        "https://user.auth.xboxlive.com/user/authenticate",
        headers={"x-xbl-contract-version": "1"},
        json={
            "Properties": {
                "AuthMethod": "RPS",
                "SiteName": "user.auth.xboxlive.com",
                "RpsTicket": f"d={access_token}"
            },
            "RelyingParty": "http://auth.xboxlive.com",
            "TokenType": "JWT"
        },
        timeout=REQUEST_TIMEOUT,
    )
    user_token_resp.raise_for_status()
    user_data = user_token_resp.json()
    user_token = user_data.get("Token")
    if not user_token:
        print("❌ 'Token' missing from user token response")
        return None, None
    display_claims = user_data.get("DisplayClaims") or {}
    xui_list = display_claims.get("xui") or []
    xui_data = xui_list[0] if xui_list else {}
    user_hash = xui_data.get("uhs")
    if not user_hash:
        print("❌ 'uhs' missing from DisplayClaims.xui[0]")
        return None, None
    xuid = None
    if "xid" in xui_data:
        xuid = xui_data["xid"]
    elif "id" in xui_data:
        xuid = xui_data["id"]
    else:
        print("⚠️ Could not find XUID in user data")
        fallback_xuid = resolve_fallback_xuid(tokens)
        if fallback_xuid:
            print("📌 Using fallback XUID from configuration")
            xuid = fallback_xuid
        
    print(f"✅ User Hash: {user_hash}")
    if xuid:
        print(f"✅ XUID: {xuid}")
    
    return user_token, xuid

def get_xsts_token(user_token):
    """Get the XSTS token, also extracting XUID from DisplayClaims when present."""
    xsts_resp = requests.post(
        "https://xsts.auth.xboxlive.com/xsts/authorize",
        headers={"x-xbl-contract-version": "1"},
        json={
            "Properties": {
                "SandboxId": "RETAIL",
                "UserTokens": [user_token]
            },
            "RelyingParty": "https://prod.xsts.halowaypoint.com/",
            "TokenType": "JWT"
        },
        timeout=REQUEST_TIMEOUT,
    )
    xsts_resp.raise_for_status()
    xsts_data = xsts_resp.json()
    xsts_token = xsts_data.get("Token")
    if not xsts_token:
        print("❌ 'Token' missing from XSTS response")
        return None, None
    print(f"✅ XSTS Token: {redact_secret(xsts_token)}")

    # Try to extract XUID from XSTS DisplayClaims (Halo relying party often includes xid)
    xuid_from_xsts = None
    try:
        xui = xsts_data.get("DisplayClaims", {}).get("xui", [])
        if xui:
            xuid_from_xsts = xui[0].get("xid") or xui[0].get("id")
            if xuid_from_xsts:
                print(f"✅ XUID from XSTS: {xuid_from_xsts}")
    except Exception:
        pass

    return xsts_token, xuid_from_xsts

def get_spartan_token(xsts_token):
    """Get the Spartan v4 token"""
    spartan_url = "https://settings.svc.halowaypoint.com/spartan-token"
    headers = {
        "User-Agent": "HaloInfinite/6.10022.0.0 (Windows;10;;Professional, x64)",
        "Accept": "application/json"
    }
    payload = {
        "Audience": "urn:343:s3:services",
        "MinVersion": "4",
        "Proof": [
            {
                "Token": xsts_token,
                "TokenType": "Xbox_XSTSv3"
            }
        ]
    }
    spartan_resp = requests.post(spartan_url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
    if not spartan_resp.ok:
        print(f"❌ Spartan Token Request Failed: {spartan_resp.status_code}")
        print(spartan_resp.text)
        return None

    spartan_data = spartan_resp.json()
    spartan_token = spartan_data.get("SpartanToken")
    if not spartan_token:
        print("❌ 'SpartanToken' missing from spartan token response")
        return None
    print(f"✅ Spartan Token: {redact_secret(spartan_token)}")
    return spartan_token

def get_clearance_token(spartan_token, xuid):
    """Get the clearance token"""
    if not xuid:
        print("❌ XUID is missing, cannot get clearance token")
        return None
        
    # Ensure XUID is a string
    xuid = str(xuid)
        
    clearance_url = f"https://settings.svc.halowaypoint.com/oban/flight-configurations/titles/hi/audiences/RETAIL/players/xuid({xuid})/active"
    
    headers = {
        "User-Agent": "HaloInfinite/6.10022.0.0 (Windows;10;;Professional, x64)",
        "Accept": "application/json",
        "x-343-authorization-spartan": spartan_token
    }
    
    params = {
        "sandbox": "UNUSED",
        "build": "210921.22.01.10.1706-0"
    }
    
    try:
        print(f"🔍 Requesting clearance token from: {clearance_url}")
        redacted_headers = dict(headers)
        if redacted_headers.get("x-343-authorization-spartan"):
            redacted_headers["x-343-authorization-spartan"] = "<redacted>"
        print(f"🔐 Using headers: {redacted_headers}")
        print(f"🔍 Using params: {params}")
        
        clearance_resp = requests.get(clearance_url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        
        print(f"📋 Response status code: {clearance_resp.status_code}")
        print(f"📋 Response headers: {dict(clearance_resp.headers)}")
        
        if not clearance_resp.ok:
            print(f"❌ Clearance Token Request Failed: {clearance_resp.status_code}")
            print(clearance_resp.text)
            return None
        
        try:
            clearance_data = clearance_resp.json()
            print(f"📋 Response data: {json.dumps(redact_token_payload(clearance_data), indent=2)}")
            
            clearance_token = clearance_data.get("FlightConfigurationId")
            if clearance_token:
                print(f"✅ Clearance Token: {redact_secret(clearance_token)}")
                return clearance_token
            else:
                print("❌ Clearance token not found in response")
                print(json.dumps(redact_token_payload(clearance_data), indent=2))
                return None
        except json.JSONDecodeError:
            print("❌ Could not parse JSON response")
            print(f"📋 Raw response omitted ({len(clearance_resp.text)} bytes)")
            return None
            
    except requests.RequestException as e:
        print(f"❌ Error getting clearance token: {str(e)}")
        return None

def main():
    """Main entry point"""
    print("🔑 Halo Stats Authentication")
    print("=" * 50)
    
    tokens = load_tokens()
    if tokens:
        # Check if the token is expired
        expiration_time = tokens.get("expires_at", 0)
        current_time = time.time()
        if current_time > expiration_time:
            print("⚠️ Tokens have expired, refreshing...")
            # Preserve fields that Microsoft's refresh response won't include
            saved_xuid = tokens.get("xuid")
            try:
                tokens = refresh_tokens(tokens["refresh_token"])
            except RuntimeError as exc:  # credentials missing — point at /setup
                print(f"❌ {exc}")
                return
            if saved_xuid and not tokens.get("xuid"):
                tokens["xuid"] = saved_xuid
        else:
            print("✅ Using existing tokens from tokens.json...")

        access_token = tokens["access_token"]
        refresh_token = tokens["refresh_token"]
    else:
        # If no tokens saved, start authentication
        print("📌 This is your first time running this app.")
        print("📌 You need to authenticate with your Xbox account.\n")
        
        tokens = authenticate()
        if not tokens:
            print("\n❌ Authentication failed.")
            print("⏳ Exiting without creating tokens.json")
            return
        access_token = tokens["access_token"]
        refresh_token = tokens["refresh_token"]

    # Get User Token and XUID
    user_token, xuid = get_user_token(access_token, tokens)
    
    # Store user token and XUID in tokens dictionary
    tokens["user_token"] = user_token
    if xuid:
        tokens["xuid"] = xuid
    
    # Get XSTS Token (also tries to extract XUID from XSTS claims)
    xsts_token, xuid_from_xsts = get_xsts_token(user_token)
    tokens["xsts_token"] = xsts_token
    if not xuid and xuid_from_xsts:
        xuid = xuid_from_xsts
        print(f"📌 Using XUID extracted from XSTS token: {xuid}")
    if xuid:
        tokens["xuid"] = xuid
    
    # Get Spartan Token
    spartan_token = get_spartan_token(xsts_token)
    tokens["spartan_token"] = spartan_token
    
    # Get Clearance Token
    if not xuid:
        xuid = resolve_fallback_xuid(tokens)
        if xuid:
            tokens["xuid"] = xuid

    if spartan_token and xuid:
        clearance_token = get_clearance_token(spartan_token, xuid)
        if clearance_token:
            tokens["clearance_token"] = clearance_token
    
    # Save the updated tokens with expiration time
    tokens["expires_at"] = time.time() + tokens.get("expires_in", 3600)  # Set expiration time
    save_tokens(tokens)
    
    # Print summary of obtained tokens
    print("\n📝 Token Summary:")
    for token_type in ["access_token", "refresh_token", "user_token", "xuid", "xsts_token", "spartan_token", "clearance_token"]:
        if token_type in tokens and tokens[token_type]:
            value = tokens[token_type]
            print(f"  ✅ {token_type}: {redact_secret(value) if 'token' in token_type else value}")
        else:
            print(f"  ❌ {token_type}: Not available")
    
    print(f"\n✅ All tokens have been saved to {TOKEN_FILE}")

if __name__ == "__main__":
    main()
