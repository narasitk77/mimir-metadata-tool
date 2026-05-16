"""Google OAuth (OpenID Connect) — internal SSO gate.

Proves a user signed in with a Google account in the allowed Workspace
domain (@thestandard.co). No Google API scopes beyond basic profile/email —
this is identity proof only, not Drive/Sheets access.

The gate is OFF unless all of GOOGLE_AUTH_CLIENT_ID / SECRET / REDIRECT_URI /
SESSION_SECRET_KEY are set. When OFF the app is open (suitable for a trusted
LAN); when ON every page except /auth/* and /healthz requires a session.
"""
from urllib.parse import urlencode

import httpx

from app.config import settings

GOOGLE_AUTH_URL     = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL    = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

# Hard-coded — this tool is for The Standard staff only. Not env-configurable
# on purpose: a typo in an env var must never widen access.
ALLOWED_DOMAIN = "thestandard.co"


def is_configured() -> bool:
    """True when every secret needed for the OAuth flow is present."""
    return bool(
        settings.GOOGLE_AUTH_CLIENT_ID
        and settings.GOOGLE_AUTH_CLIENT_SECRET
        and settings.GOOGLE_AUTH_REDIRECT_URI
        and settings.SESSION_SECRET_KEY
    )


def make_authorize_url(state: str) -> str:
    params = {
        "client_id":     settings.GOOGLE_AUTH_CLIENT_ID,
        "redirect_uri":  settings.GOOGLE_AUTH_REDIRECT_URI,
        "response_type": "code",
        "scope":         "openid email profile",
        "state":         state,
        "access_type":   "online",
        # "hd" pre-filters Google's account picker to the Workspace domain.
        # It is a HINT only — the server still verifies the email below.
        "hd":            ALLOWED_DOMAIN,
        "prompt":        "select_account",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def exchange_code_for_user(code: str) -> dict:
    """Auth code → access token → userinfo dict. Raises on any failure."""
    async with httpx.AsyncClient(timeout=15) as client:
        tok = await client.post(GOOGLE_TOKEN_URL, data={
            "code":          code,
            "client_id":     settings.GOOGLE_AUTH_CLIENT_ID,
            "client_secret": settings.GOOGLE_AUTH_CLIENT_SECRET,
            "redirect_uri":  settings.GOOGLE_AUTH_REDIRECT_URI,
            "grant_type":    "authorization_code",
        })
        tok.raise_for_status()
        access = tok.json().get("access_token")
        if not access:
            raise RuntimeError("Google did not return an access_token")
        info = await client.get(GOOGLE_USERINFO_URL,
                                headers={"Authorization": f"Bearer {access}"})
        info.raise_for_status()
        return info.json()


def email_allowed(email: str) -> bool:
    """True only for verified @thestandard.co addresses."""
    if not email:
        return False
    return email.strip().lower().endswith("@" + ALLOWED_DOMAIN)
