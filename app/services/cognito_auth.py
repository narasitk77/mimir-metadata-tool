"""
Cognito SRP authentication for Mimir API.
Mirrors the Node.js mimirAuth.js — authenticates once then caches the token,
auto-refreshes 5 minutes before expiry (Cognito ID tokens last 1 hour).
"""
import asyncio
import logging
import time

from app.config import settings

logger = logging.getLogger(__name__)

_cached_token: str = ""
_token_expires_at: float = 0.0
_TOKEN_TTL = 55 * 60  # 55 min cache (token valid 60 min)


def _authenticate_sync() -> str:
    """Synchronous Cognito SRP auth via pycognito (runs in thread executor)."""
    from pycognito import Cognito  # type: ignore
    u = Cognito(
        user_pool_id=settings.MIMIR_COGNITO_USER_POOL_ID,
        client_id=settings.MIMIR_COGNITO_CLIENT_ID,
        username=settings.MIMIR_USERNAME,
    )
    u.authenticate(password=settings.MIMIR_PASSWORD)
    return u.id_token  # type: ignore[return-value]


async def get_token() -> str:
    """Return a valid Cognito ID token, refreshing if needed."""
    global _cached_token, _token_expires_at
    now = time.time()
    if _cached_token and now < _token_expires_at:
        return _cached_token
    logger.info("Refreshing Mimir Cognito token via SRP...")
    token = await asyncio.to_thread(_authenticate_sync)
    _cached_token = token
    _token_expires_at = now + _TOKEN_TTL
    logger.info("Mimir Cognito token refreshed (valid ~55 min)")
    return token


async def force_refresh() -> str:
    """Clear cache and force a fresh Cognito authentication."""
    global _cached_token, _token_expires_at
    _cached_token = ""
    _token_expires_at = 0.0
    return await get_token()
