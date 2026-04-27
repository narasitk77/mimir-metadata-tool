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


def is_configured() -> bool:
    return bool(
        settings.GOOGLE_AUTH_CLIENT_ID
        and settings.GOOGLE_AUTH_CLIENT_SECRET
        and settings.GOOGLE_AUTH_REDIRECT_URI
        and settings.SESSION_SECRET_KEY
    )


def make_authorize_url(state: str) -> str:
    params = {
        "client_id": settings.GOOGLE_AUTH_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_AUTH_REDIRECT_URI,
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
                "client_id": settings.GOOGLE_AUTH_CLIENT_ID,
                "client_secret": settings.GOOGLE_AUTH_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_AUTH_REDIRECT_URI,
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
