"""FastAPI app entrypoint.

Internal-LAN tool — no auth gate, no proxy middleware. Run behind the
LAN firewall (or a reverse proxy with HTTPS) when exposing publicly.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.database import Base, engine, run_migrations
from app.models.person import Person  # noqa: F401 — register with Base
from app.models.audit_log import AuditLog  # noqa: F401 — register with Base
from app.models.mimir_option import MimirOption  # noqa: F401 — register with Base
from app.views.routes import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
_log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    run_migrations()
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


app = FastAPI(title="Mimir Metadata AI Tool", version="2.0.0", lifespan=lifespan)
app.include_router(router)
