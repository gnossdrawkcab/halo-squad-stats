"""Web Push (VAPID) for the Halo PWA — live-event notifications even when the
app is closed.

Self-contained: VAPID keys are generated once and cached in data/vapid.json;
browser PushSubscriptions are stored in data/push_subs.json (shared /data volume,
so the scraper process can send while the web process collects subscriptions).

All heavy imports (pywebpush, cryptography) are LAZY so importing this module can
never break app boot if the dependency isn't installed yet. Every send is
best-effort and swallows errors.
"""
import base64
import json
import logging
import os
import threading

from halo_paths import data_path

logger = logging.getLogger("push")

VAPID_PATH = data_path("vapid.json")
VAPID_PEM_PATH = data_path("vapid_private.pem")  # pywebpush wants a PEM file path
SUBS_PATH = data_path("push_subs.json")
_LOCK = threading.Lock()


def _get_vapid() -> dict:
    """Load or generate the VAPID keypair. Returns {'public': <b64url raw key>,
    'private_pem': <pkcs8 pem>}. Public key is the browser applicationServerKey."""
    try:
        with open(VAPID_PATH) as f:
            v = json.load(f)
        if v.get("public") and v.get("private_pem"):
            _ensure_pem(v)
            return v
    except Exception:
        pass
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    priv = ec.generate_private_key(ec.SECP256R1())
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    v = {"public": base64.urlsafe_b64encode(pub_raw).rstrip(b"=").decode(),
         "private_pem": priv_pem}
    try:
        with open(VAPID_PATH, "w") as f:
            json.dump(v, f)
    except Exception as e:
        logger.warning("vapid_write_failed error=%s", e)
    _ensure_pem(v)
    return v


def _ensure_pem(v: dict) -> str:
    """pywebpush loads the VAPID private key from a PEM *file path* — make sure
    the PEM exists on disk and return its path."""
    try:
        if not os.path.exists(VAPID_PEM_PATH) and v.get("private_pem"):
            with open(VAPID_PEM_PATH, "w") as f:
                f.write(v["private_pem"])
    except Exception as e:
        logger.warning("vapid_pem_write_failed error=%s", e)
    return VAPID_PEM_PATH


def public_key() -> str:
    try:
        return _get_vapid()["public"]
    except Exception as e:
        logger.warning("vapid_public_failed error=%s", e)
        return ""


def _load_subs() -> list:
    try:
        with open(SUBS_PATH) as f:
            subs = json.load(f)
        return subs if isinstance(subs, list) else []
    except Exception:
        return []


def _save_subs(subs: list) -> None:
    try:
        with open(SUBS_PATH, "w") as f:
            json.dump(subs, f)
    except Exception as e:
        logger.warning("subs_write_failed error=%s", e)


def add_sub(sub: dict) -> bool:
    ep = (sub or {}).get("endpoint")
    if not ep:
        return False
    with _LOCK:
        subs = _load_subs()
        if not any(s.get("endpoint") == ep for s in subs):
            subs.append(sub)
            _save_subs(subs)
    return True


def remove_sub(endpoint: str) -> None:
    if not endpoint:
        return
    with _LOCK:
        subs = [s for s in _load_subs() if s.get("endpoint") != endpoint]
        _save_subs(subs)


def sub_count() -> int:
    return len(_load_subs())


def send_push(title: str, body: str, url: str = "/live", tag: str = "halo") -> int:
    """Push to every stored subscription. Prunes expired (404/410). Returns the
    number delivered. Best-effort — never raises."""
    subs = _load_subs()
    if not subs:
        return 0
    try:
        from pywebpush import webpush, WebPushException
    except Exception as e:
        logger.warning("pywebpush_import_failed error=%s", e)
        return 0
    v = _get_vapid()
    pem_path = _ensure_pem(v)
    subject = os.getenv("HALO_VAPID_SUBJECT", "mailto:admin@example.com")
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag})
    keep, sent = [], 0
    for s in subs:
        try:
            # ttl: queue+retry for 12h so a phone that's asleep/locked when the
            # game ends still gets the push on wake (ttl=0, the pywebpush default,
            # makes the service DROP it — that's why 'test' works but real ones
            # miss on mobile). Urgency:high punches through Android Doze.
            webpush(subscription_info=s, data=payload,
                    vapid_private_key=pem_path, vapid_claims={"sub": subject},
                    ttl=43200, headers={"Urgency": "high"})
            keep.append(s)
            sent += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):
                continue  # expired subscription → drop it
            keep.append(s)
            logger.warning("webpush_failed code=%s error=%s", code, e)
        except Exception as e:
            keep.append(s)
            logger.warning("webpush_error error=%s", e)
    if len(keep) != len(subs):
        with _LOCK:
            _save_subs(keep)
    return sent
