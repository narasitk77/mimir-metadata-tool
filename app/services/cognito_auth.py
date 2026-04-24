"""
Cognito SRP authentication for Mimir API.

Supports two credential sources:
  1. Runtime session credentials (entered via UI) — in-memory, persists until
     explicit logout or container restart (stay-logged-in)
  2. Environment variables (fallback) — MIMIR_USERNAME / MIMIR_PASSWORD

The Cognito ID token is cached in-process and auto-refreshed every ~55 min
(Cognito TTL is 60 min). Credentials themselves live only in process memory —
never written to disk, DB, or logs, so a container restart ends the session.
"""
import asyncio
import logging
import time

from app.config import settings

logger = logging.getLogger(__name__)

_TOKEN_TTL = 55 * 60  # refresh Cognito ID token every 55 min (valid 60)

_session_username: str = ""
_session_password: str = ""
_session_started_at: float = 0.0

_cached_token: str = ""
_token_expires_at: float = 0.0


def _is_session_active() -> bool:
    return bool(_session_username)


def _wipe_session() -> None:
    global _session_username, _session_password, _session_started_at
    global _cached_token, _token_expires_at
    _session_username = ""
    _session_password = ""
    _session_started_at = 0.0
    _cached_token = ""
    _token_expires_at = 0.0


def _current_creds() -> tuple[str, str]:
    """Return (username, password) — runtime session preferred, else env fallback."""
    if _is_session_active():
        return _session_username, _session_password
    return settings.MIMIR_USERNAME, settings.MIMIR_PASSWORD


def _authenticate_sync(username: str, password: str) -> str:
    from pycognito import Cognito  # type: ignore
    u = Cognito(
        user_pool_id=settings.MIMIR_COGNITO_USER_POOL_ID,
        client_id=settings.MIMIR_COGNITO_CLIENT_ID,
        username=username,
    )
    u.authenticate(password=password)
    return u.id_token  # type: ignore[return-value]


async def get_token() -> str:
    """Return a valid Cognito ID token. Uses runtime session creds if active, else env."""
    global _cached_token, _token_expires_at

    now = time.time()
    if _cached_token and now < _token_expires_at:
        return _cached_token

    username, password = _current_creds()
    if not username or not password:
        raise RuntimeError("No Mimir credentials — login via UI or set MIMIR_USERNAME/MIMIR_PASSWORD env")

    logger.info("Refreshing Mimir Cognito token via SRP...")
    token = await asyncio.to_thread(_authenticate_sync, username, password)
    _cached_token = token
    _token_expires_at = now + _TOKEN_TTL
    logger.info(f"Mimir Cognito token refreshed (valid until {time.strftime('%H:%M:%S', time.localtime(_token_expires_at))})")
    return token


async def force_refresh() -> str:
    """Clear token cache (NOT credentials) and force a fresh Cognito authentication."""
    global _cached_token, _token_expires_at
    _cached_token = ""
    _token_expires_at = 0.0
    return await get_token()


async def login(username: str, password: str) -> dict:
    """Start a runtime session (persists until explicit logout or container restart)."""
    global _session_username, _session_password, _session_started_at
    # Validate credentials first (will raise on bad password)
    await asyncio.to_thread(_authenticate_sync, username, password)
    _session_username = username
    _session_password = password
    _session_started_at = time.time()
    # Force a token refresh on next call so cache uses new creds
    global _cached_token, _token_expires_at
    _cached_token = ""
    _token_expires_at = 0.0
    return get_status()


def logout() -> dict:
    _wipe_session()
    return get_status()


def get_status() -> dict:
    """Return current session status for UI."""
    active = _is_session_active()
    return {
        "active": active,
        "username": _session_username if active else "",
        "started_at": _session_started_at if active else 0,
        "session_age_sec": max(0, int(time.time() - _session_started_at)) if active else 0,
        "token_valid_until": _token_expires_at if _cached_token else 0,
        "env_fallback": bool(settings.MIMIR_USERNAME and settings.MIMIR_PASSWORD),
    }
