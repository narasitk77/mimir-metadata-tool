"""
Cognito SRP authentication for Mimir API.

Supports two credential sources:
  1. Runtime session credentials (entered via UI) — in-memory, auto-cleared after 20 min
  2. Environment variables (fallback) — MIMIR_USERNAME / MIMIR_PASSWORD

The token is cached in-process; when session expires, both token and credentials
are wiped and the user must log in again via UI.
"""
import asyncio
import logging
import time

from app.config import settings

logger = logging.getLogger(__name__)

_SESSION_TTL = 20 * 60  # 20 minutes — credentials + token wiped after this

_session_username: str = ""
_session_password: str = ""
_session_expires_at: float = 0.0

_cached_token: str = ""
_token_expires_at: float = 0.0


def _is_session_active() -> bool:
    return bool(_session_username) and time.time() < _session_expires_at


def _wipe_session() -> None:
    global _session_username, _session_password, _session_expires_at
    global _cached_token, _token_expires_at
    _session_username = ""
    _session_password = ""
    _session_expires_at = 0.0
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

    # Session expired → wipe everything
    if _session_username and time.time() >= _session_expires_at:
        logger.info("Session expired (20 min) — wiping credentials and token")
        _wipe_session()

    now = time.time()
    if _cached_token and now < _token_expires_at:
        return _cached_token

    username, password = _current_creds()
    if not username or not password:
        raise RuntimeError("No Mimir credentials — login via UI or set MIMIR_USERNAME/MIMIR_PASSWORD env")

    logger.info("Refreshing Mimir Cognito token via SRP...")
    token = await asyncio.to_thread(_authenticate_sync, username, password)
    _cached_token = token
    # Token cache expires when session does (or 55 min if env-based, whichever is sooner)
    env_ttl = now + 55 * 60
    if _is_session_active():
        _token_expires_at = min(_session_expires_at, env_ttl)
    else:
        _token_expires_at = env_ttl
    logger.info(f"Mimir Cognito token refreshed (valid until {time.strftime('%H:%M:%S', time.localtime(_token_expires_at))})")
    return token


async def force_refresh() -> str:
    """Clear token cache (NOT credentials) and force a fresh Cognito authentication."""
    global _cached_token, _token_expires_at
    _cached_token = ""
    _token_expires_at = 0.0
    return await get_token()


async def login(username: str, password: str) -> dict:
    """Start a runtime session. Validates by doing a Cognito auth right away."""
    global _session_username, _session_password, _session_expires_at
    # Validate credentials first (will raise on bad password)
    await asyncio.to_thread(_authenticate_sync, username, password)
    _session_username = username
    _session_password = password
    _session_expires_at = time.time() + _SESSION_TTL
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
        "expires_at": _session_expires_at if active else 0,
        "expires_in_sec": max(0, int(_session_expires_at - time.time())) if active else 0,
        "session_ttl_sec": _SESSION_TTL,
        "env_fallback": bool(settings.MIMIR_USERNAME and settings.MIMIR_PASSWORD),
    }
