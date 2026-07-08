import json
import time
import subprocess
import os
import sys
from pathlib import Path
from halo_paths import data_path

TOKEN_FILE = data_path("tokens.json")
# clearance_token and xuid are desirable but not hard-required;
# the scraper will attempt to run and log failures if they are absent.
REQUIRED_KEYS = [
    "access_token", "refresh_token", "user_token",
    "xsts_token", "spartan_token"
]

# Get the directory where this script is located
SCRIPT_DIR = Path(__file__).parent.absolute()

def load_tokens():
    try:
        with TOKEN_FILE.open('r') as f:
            return json.load(f)
    except Exception as e:
        print(f"❌ Failed to load tokens.json: {e}")
        return None

def token_status():
    if not TOKEN_FILE.exists():
        print("❌ tokens.json does not exist.")
        return "missing"

    tokens = load_tokens()
    if not tokens:
        return "invalid"

    for key in REQUIRED_KEYS:
        if not tokens.get(key):
            print(f"❌ Missing or empty token: {key}")
            return "invalid"

    if time.time() > tokens.get("expires_at", 0):
        print("⚠️ Token expired. Will refresh.")
        return "expired"

    print("✅ tokens.json is valid and all tokens are present.")
    return "valid"

def run_script(script_name):
    try:
        script_path = SCRIPT_DIR / script_name
        print(f"▶️ Running {script_name}...")
        subprocess.run([sys.executable, str(script_path)], check=True)
    except subprocess.CalledProcessError as e:
        print(f"❌ {script_name} failed with exit code {e.returncode}")

def _session_is_active():
    """True if a match was recorded recently (a play session is likely live),
    so the scraper should poll quickly to surface new games. Falls back to False
    (idle) when we can't tell. Reads update_status.json, which stats.py refreshes
    at the end of every run with the latest match time + whether new rows landed."""
    from halo_paths import data_path
    active_window_min = int(os.getenv("HALO_ACTIVE_WINDOW_MIN", "45"))
    status_path = data_path("update_status.json")
    if not status_path.exists():
        return False
    try:
        with open(status_path, "r") as f:
            status = json.load(f)
    except Exception:
        return False

    # A brand-new row last cycle almost always means someone is mid-session.
    if status.get("new_rows_added"):
        return True

    latest = status.get("latest_match_at")
    if not latest:
        return False
    try:
        from datetime import datetime, timezone
        ts = datetime.fromisoformat(str(latest).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
        return age_min <= active_window_min
    except Exception:
        return False


def get_update_interval():
    """Pick the sleep interval before the next scrape.

    Manual override wins: if settings.json sets `update_interval`, use it.
    Otherwise poll adaptively — fast while a session is active (so new games show
    up quickly, the #1 complaint) and slow when idle (to spare the Halo API)."""
    from halo_paths import data_path

    default_interval = int(os.getenv("HALO_UPDATE_INTERVAL", "60"))
    settings_path = data_path("settings.json")

    # Explicit manual override via the Settings page / settings.json.
    if settings_path.exists():
        try:
            with open(settings_path, "r") as f:
                settings = json.load(f)
            if "update_interval" in settings:
                interval = int(settings.get("update_interval"))
                if interval < 1:
                    print(f"Warning: update_interval {interval} is invalid, using default {default_interval}s")
                    return default_interval
                return interval
        except (ValueError, TypeError) as e:
            print(f"Warning: Invalid update_interval value in settings: {e}")
        except Exception as e:
            print(f"Warning: Could not load settings file: {e}")

    # Adaptive default.
    # Idle default matches the prior fixed cadence (no regression for the first
    # game of a session); active halves it so mid-session games show up sooner.
    active_interval = int(os.getenv("HALO_UPDATE_INTERVAL_ACTIVE", "15"))
    idle_interval = int(os.getenv("HALO_UPDATE_INTERVAL_IDLE",
                                  os.getenv("HALO_UPDATE_INTERVAL", "60")))
    if _session_is_active():
        return max(1, active_interval)
    return max(1, idle_interval)

def main():
    # Presence poller: daemon thread in THIS long-lived process (stats.py runs
    # as per-cycle subprocesses, so it can't own anything continuous).
    try:
        from presence import start_presence_thread
        start_presence_thread()
    except Exception as e:
        print(f"presence poller failed to start: {e}")

    while True:
        status = token_status()
        if status != "valid":
            # Check if running in Docker (HALO_DATA_DIR suggests Docker environment)
            is_docker = os.getenv("HALO_DATA_DIR") == "/data"
            
            if is_docker and status == "missing":
                print("⏳ tokens.json not found yet.")
                print("📌 To get started, open the web UI's /setup page and complete")
                print("   steps 1-2 (API credentials + Authorize with Xbox Live) —")
                print("   that creates tokens.json in the data dir, no CLI needed.")
                print("⏳ Waiting for tokens.json...")
            else:
                # Refresh tokens using auth.py (works in Docker if refresh_token exists).
                run_script("auth.py")

        if token_status() == "valid":
            run_script("stats.py")
        else:
            if not os.getenv("HALO_DATA_DIR"):
                print("❌ Unable to validate tokens after auth. Skipping this cycle.")

        # Recompute AFTER the scrape so a match that just landed (or a live
        # session) shortens this very sleep instead of waiting a full cycle.
        sleep_seconds = get_update_interval()
        print(f"Sleeping {sleep_seconds} seconds before next run...")
        time.sleep(sleep_seconds)

if __name__ == "__main__":
    main()
