"""Audit logging helper — one-line fire-and-forget calls from any endpoint.

Usage:
    from app.audit import log

    log("push", target=item_id, status="ok" if r["ok"] else "error",
        message=r.get("error") or "Push to Mimir succeeded",
        details={"uuid_fields_sent": r.get("uuid_fields_sent")})

The helper swallows any DB error so audit logging never breaks the
caller. Reads happen via /api/audit-log.
"""
from __future__ import annotations

import contextvars
import json
import logging
from typing import Any, Optional

from app.database import SessionLocal
from app.models.audit_log import AuditLog

_log = logging.getLogger(__name__)

# Per-request current user (email). Set by AuthGateMiddleware on each request;
# contextvars keep it isolated per async task so concurrent requests don't mix.
_current_user: contextvars.ContextVar[str] = contextvars.ContextVar("audit_current_user", default="")


def set_current_user(email: str) -> None:
    """Record the logged-in user for the current request context."""
    _current_user.set(email or "")


def get_current_user() -> str:
    """Return the user email recorded for the current request context."""
    return _current_user.get()


def log(
    action: str,
    target: str = "",
    status: str = "ok",
    message: str = "",
    details: Optional[Any] = None,
    user: Optional[str] = None,
) -> None:
    """Append one audit row. Never raises — failures only hit the app log.

    `user` defaults to the logged-in user from the current request context.
    Pass it explicitly only when logging outside a request (background tasks).
    """
    try:
        who = user if user is not None else _current_user.get()
        db = SessionLocal()
        try:
            row = AuditLog(
                user=(who or "")[:128],
                action=action[:64],
                target=(target or "")[:256],
                status=status[:16],
                message=(message or "")[:4000],
                details=json.dumps(details, ensure_ascii=False, default=str)[:8000] if details else "",
            )
            db.add(row)
            db.commit()
        finally:
            db.close()
    except Exception as e:
        _log.warning(f"audit log failed for action={action} target={target}: {e}")
