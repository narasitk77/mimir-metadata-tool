"""Usage history helper — record one row per batch / push / clear snapshot.

Mirrors app/audit.py's design: one INSERT, swallow errors, never raises into
the caller's hot path. Reads the current user from audit's contextvar so the
caller doesn't have to pass it explicitly.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.audit import get_current_user
from app.config import settings
from app.database import SessionLocal
from app.models.usage_history import UsageHistory

_log = logging.getLogger(__name__)


def record(
    event: str,
    *,
    folder_label: str = "",
    assets_count: int = 0,
    tokens_input: float = 0.0,
    tokens_output: float = 0.0,
    cost_usd: float = 0.0,
    duration_sec: int = 0,
    notes: str = "",
    user: Optional[str] = None,
) -> None:
    """Insert one usage_history row. Never raises — failures only hit the app log.

    `user` defaults to the SSO-logged-in user from the request context.
    `gemini_model` is read from settings at write time (so the row is
    reproducible even if the model is changed later).
    """
    try:
        who = user if user is not None else get_current_user()
        db = SessionLocal()
        try:
            row = UsageHistory(
                user=(who or "")[:128],
                event=event[:32],
                folder_label=(folder_label or "")[:256],
                assets_count=int(assets_count or 0),
                tokens_input=float(tokens_input or 0),
                tokens_output=float(tokens_output or 0),
                cost_usd=float(cost_usd or 0),
                gemini_model=(settings.GEMINI_MODEL or "")[:64],
                duration_sec=int(duration_sec or 0),
                notes=(notes or "")[:2000],
            )
            db.add(row)
            db.commit()
        finally:
            db.close()
    except Exception as e:
        _log.warning(f"usage record failed for event={event}: {e}")
