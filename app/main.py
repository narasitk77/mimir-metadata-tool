from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.database import Base, engine, run_migrations, _ensure_person_table
from app.models.person import Person  # noqa: F401 — registers Person with Base
from app.models.allowed_user import AllowedUser  # noqa: F401
from app.services import google_auth as _google_auth
from app.views.routes import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
_log = logging.getLogger(__name__)

try:
    from app.services import vector_service as _vs
    _QDRANT = True
except Exception:
    _vs = None
    _QDRANT = False


async def _startup_vector_index():
    if not _QDRANT:
        return
    await asyncio.sleep(8)
    from app.database import SessionLocal
    from app.models.asset import Asset
    db = SessionLocal()
    try:
        assets = db.query(Asset).filter(Asset.status == "done").all()
        if not assets:
            return
        indexed = errors = 0
        for asset in assets:
            try:
                if _vs.index_asset(asset):
                    indexed += 1
            except Exception as e:
                errors += 1
                _log.debug(f"Startup vector index error for {asset.item_id[:8]}: {e}")
        _log.info(f"Startup vector index: {indexed}/{len(assets)} assets indexed ({errors} errors)")
    except Exception as e:
        _log.warning(f"Startup vector index failed: {e}")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    run_migrations()
    _ensure_person_table()
    if _google_auth.is_configured():
        _log.info(f"Google SSO gate ENABLED — restricted to @{settings.ALLOWED_EMAIL_DOMAIN}")
    else:
        _log.warning(
            "Google SSO gate is DISABLED — app is open to anyone. "
            "Set GOOGLE_AUTH_CLIENT_ID / SECRET / REDIRECT_URI / SESSION_SECRET_KEY to enable."
        )
    if _QDRANT:
        try:
            _vs.init_collection()
        except Exception as e:
            _log.warning(f"Qdrant not available at startup (will retry on first use): {e}")
    from app.database import SessionLocal
    from app.models.asset import Asset
    db = SessionLocal()
    try:
        stuck = db.query(Asset).filter(Asset.status == "processing").all()
        for a in stuck:
            a.status = "pending"
            _log.warning(f"Reset stuck processing asset: {a.title or a.item_id}")
        if stuck:
            db.commit()
    finally:
        db.close()
    if _QDRANT:
        asyncio.create_task(_startup_vector_index())
    yield


app = FastAPI(title="Mimir Metadata AI Tool", version="1.0.0", lifespan=lifespan)

# ── Google SSO gate (only active when GOOGLE_AUTH_CLIENT_ID configured) ───────
_PUBLIC_PATHS = {"/auth/login", "/auth/start", "/auth/callback", "/auth/logout", "/auth/denied", "/healthz"}
_PUBLIC_PREFIXES = ("/static/", "/favicon")


class AuthGateMiddleware(BaseHTTPMiddleware):
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
        return RedirectResponse(url=(settings.APP_ROOT_PATH or "") + "/auth/login")


def _canonical_parts():
    """Returns (scheme, netloc) parsed from GOOGLE_AUTH_REDIRECT_URI, or (None, None)."""
    uri = settings.GOOGLE_AUTH_REDIRECT_URI or ""
    if not uri:
        return None, None
    p = urlparse(uri)
    return (p.scheme or None), (p.netloc or None)


class CanonicalHostMiddleware(BaseHTTPMiddleware):
    """Force every browser request onto the host/scheme registered in the OAuth
    redirect URI — otherwise the session cookie set during /auth/start
    won't be sent to /auth/callback (cross-host) and login fails with
    'Invalid auth state'.

    The scheme is taken from GOOGLE_AUTH_REDIRECT_URI so this works correctly
    behind a TLS-terminating proxy (e.g. Caddy → HTTP-only app)."""
    async def dispatch(self, request: Request, call_next):
        if not _google_auth.is_configured():
            return await call_next(request)
        path = request.url.path
        # Healthchecks / static assets should bypass — they hit localhost
        if path == "/healthz" or path.startswith("/static/"):
            return await call_next(request)
        canonical_scheme, canonical_host = _canonical_parts()
        if not canonical_host:
            return await call_next(request)
        actual = (request.headers.get("host") or "").lower()
        # Allow internal probes (Docker healthcheck uses localhost:8000)
        if actual.startswith(("localhost", "127.0.0.1", "[::1]")):
            return await call_next(request)
        if actual != canonical_host.lower():
            target = f"{canonical_scheme or request.url.scheme}://{canonical_host}{path}"
            if request.url.query:
                target += "?" + request.url.query
            return RedirectResponse(url=target, status_code=307)
        return await call_next(request)


# Middleware order: outermost added last. Session must wrap everything so
# downstream middlewares/routes see request.session populated.
app.add_middleware(AuthGateMiddleware)
app.add_middleware(CanonicalHostMiddleware)
if settings.SESSION_SECRET_KEY:
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.SESSION_SECRET_KEY,
        max_age=30 * 24 * 3600,  # 30 days
        same_site="lax",
        https_only=False,
    )

app.include_router(router)
