"""
Google OAuth (OpenID Connect) for internal SSO gate.

Verifies a user signs in with a Google account whose email belongs to
ALLOWED_EMAIL_DOMAIN (e.g. @thestandard.co). No Google API permissions
beyond basic profile/email — used purely as identity proof.
"""
from urllib.parse import urlencode

import httpx

from app.config import settings

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def _client_id() -> str:
    return settings.GOOGLE_AUTH_CLIENT_ID or settings.GOOGLE_OAUTH_CLIENT_ID


def _client_secret() -> str:
    return settings.GOOGLE_AUTH_CLIENT_SECRET or settings.GOOGLE_OAUTH_CLIENT_SECRET


def _redirect_uri() -> str:
    # Prefer the SSO-specific URI; fall back to the Sheets one if a single OAuth
    # client is being shared (callback path differs, but if user only set OAUTH
    # vars they likely registered both /auth/callback and /api/sheets/callback).
    return settings.GOOGLE_AUTH_REDIRECT_URI or settings.GOOGLE_REDIRECT_URI


def is_configured() -> bool:
    return bool(
        _client_id()
        and _client_secret()
        and _redirect_uri()
        and settings.SESSION_SECRET_KEY
    )


def make_authorize_url(state: str) -> str:
    params = {
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        # Google "hd" (hosted domain) hint — pre-filters Google account picker
        "hd": settings.ALLOWED_EMAIL_DOMAIN,
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def exchange_code_for_user(code: str) -> dict:
    """Exchange auth code → access token → userinfo. Returns userinfo dict."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": _client_id(),
                "client_secret": _client_secret(),
                "redirect_uri": _redirect_uri(),
                "grant_type": "authorization_code",
            },
        )
        r.raise_for_status()
        access = r.json().get("access_token")
        if not access:
            raise RuntimeError("Google did not return an access_token")
        u = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access}"},
        )
        u.raise_for_status()
        return u.json()


def email_allowed(email: str) -> bool:
    if not email:
        return False
    domain = settings.ALLOWED_EMAIL_DOMAIN.lower().lstrip("@")
    return email.lower().endswith("@" + domain)
