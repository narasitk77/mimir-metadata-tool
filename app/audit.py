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

import json
import logging
from typing import Any, Optional

from app.database import SessionLocal
from app.models.audit_log import AuditLog

_log = logging.getLogger(__name__)


def log(
    action: str,
    target: str = "",
    status: str = "ok",
    message: str = "",
    details: Optional[Any] = None,
) -> None:
    """Append one audit row. Never raises — failures only hit the app log."""
    try:
        db = SessionLocal()
        try:
            row = AuditLog(
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
