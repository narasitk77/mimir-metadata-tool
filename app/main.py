"""FastAPI app entrypoint.

Optional Google SSO gate: when GOOGLE_AUTH_* + SESSION_SECRET_KEY are set,
every page except /auth/* and /healthz requires a Google login with an
@thestandard.co account. When unset the app is open (trusted-LAN mode).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.database import Base, engine, run_migrations
from app.models.person import Person  # noqa: F401 — register with Base
from app.models.audit_log import AuditLog  # noqa: F401 — register with Base
from app.models.mimir_option import MimirOption  # noqa: F401 — register with Base
from app.services import google_auth as _google_auth
from app.views.routes import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
_log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    run_migrations()
    if _google_auth.is_configured():
        _log.info(f"Google SSO gate ENABLED — @{_google_auth.ALLOWED_DOMAIN} accounts only")
    else:
        _log.warning("Google SSO gate DISABLED — app is open. Set GOOGLE_AUTH_* + "
                     "SESSION_SECRET_KEY to require login.")
    # Recover assets stuck in 'processing' from a previous crashed run.
    from app.database import SessionLocal
    from app.models.asset import Asset
    db = SessionLocal()
    try:
        stuck = db.query(Asset).filter(Asset.status == "processing").all()
        for a in stuck:
            a.status = "pending"
        if stuck:
            db.commit()
            _log.warning(f"Reset {len(stuck)} stuck 'processing' assets to pending")
    finally:
        db.close()
    yield


app = FastAPI(title="Mimir Metadata AI Tool", version="2.1.0", lifespan=lifespan)

# Paths reachable without a session — the login flow, the auth-state probe,
# and the healthcheck.
_PUBLIC_PATHS    = {"/auth/login", "/auth/start", "/auth/callback", "/auth/logout",
                    "/auth/me", "/healthz"}
_PUBLIC_PREFIXES = ("/static/", "/favicon")


class AuthGateMiddleware(BaseHTTPMiddleware):
    """Require a Google session for everything except the public paths.
    No-op when SSO is not configured."""
    async def dispatch(self, request: Request, call_next):
        if not _google_auth.is_configured():
            return await call_next(request)
        path = request.url.path
        if path in _PUBLIC_PATHS or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)
        user = request.session.get("user")
        if user and user.get("email"):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return RedirectResponse(url="/auth/login")


# Middleware added last wraps outermost — SessionMiddleware must wrap the gate
# so request.session is populated before AuthGate reads it.
app.add_middleware(AuthGateMiddleware)
if settings.SESSION_SECRET_KEY:
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.SESSION_SECRET_KEY,
        max_age=14 * 24 * 3600,   # 14-day session
        same_site="lax",
        https_only=False,         # TLS is terminated by Caddy in front
    )

app.include_router(router)
