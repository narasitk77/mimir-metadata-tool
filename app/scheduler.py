"""Automation scheduler — poll Mimir watch folders for new items, auto-batch.

Design:
  • APScheduler tick every N minutes (default 15) — `poll_all_folders()`
  • Each enabled WatchFolder → call existing `fetch_all_items()` which upserts
    Asset rows. Count delta = number of new pendings added.
  • If any new pending appeared AND no manual batch is running → kick off
    `run_batch_internal()` to analyze them with Gemini.
  • Audit log writes on every tick (heartbeat) + on every fetch result + on
    every auto-batch — so an absence of ticks is itself a visible signal.
  • Global kill-switch (`set_paused(True)`) for emergency stop without
    disabling each folder one by one.

Safety: NO auto-push. Items move through pending → done. A human still
clicks "Push All" in the UI.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.audit import log as audit_log, set_current_user
from app.controllers.mimir_controller import fetch_all_items
from app.database import SessionLocal
from app.models.asset import Asset
from app.models.watch_folder import WatchFolder

_log = logging.getLogger(__name__)

_scheduler: Optional[AsyncIOScheduler] = None
_paused: bool = False
_interval_minutes: int = 15
_last_heartbeat_at: Optional[datetime] = None
_last_tick_summary: dict = {}

SCHEDULER_USER = "__scheduler__"


def is_paused() -> bool:
    return _paused


def set_paused(v: bool) -> None:
    global _paused
    _paused = bool(v)
    audit_log("automation_pause" if _paused else "automation_resume",
              target="all", message="Automation " + ("paused" if _paused else "resumed"),
              user=SCHEDULER_USER)


def status() -> dict:
    """Snapshot for the UI's automation panel."""
    return {
        "running":           _scheduler is not None and _scheduler.running,
        "paused":            _paused,
        "interval_minutes":  _interval_minutes,
        "last_heartbeat_at": _last_heartbeat_at.isoformat() if _last_heartbeat_at else None,
        "last_tick":         _last_tick_summary,
    }


async def _poll_folder(folder_id: str) -> int:
    """Run one poll: invoke fetch_all_items, return count of NEW assets added.

    We count by querying `assets.folder_id == folder_id` before and after the
    generator drains — `fetch_all_items` only INSERTs new rows (existing items
    are left alone), so delta == new pending count.
    """
    db = SessionLocal()
    try:
        before = db.query(Asset).filter(Asset.folder_id == folder_id).count()
    finally:
        db.close()

    last_err: Optional[str] = None
    async for event in fetch_all_items(folder_id):
        if event.get("type") == "error":
            last_err = event.get("message", "unknown error")

    db = SessionLocal()
    try:
        after = db.query(Asset).filter(Asset.folder_id == folder_id).count()
    finally:
        db.close()

    if last_err and after == before:
        raise RuntimeError(last_err)
    return max(0, after - before)


async def poll_all_folders() -> None:
    """Scheduler tick: poll every enabled folder, then maybe trigger batch."""
    global _last_heartbeat_at, _last_tick_summary
    if _paused:
        _log.info("Scheduler tick skipped — automation paused")
        _last_tick_summary = {"skipped": "paused", "at": datetime.utcnow().isoformat()}
        return
    _last_heartbeat_at = datetime.utcnow()
    set_current_user(SCHEDULER_USER)

    db = SessionLocal()
    try:
        folders = db.query(WatchFolder).filter(WatchFolder.enabled == True).all()
        folder_data = [(f.id, f.folder_id, f.label or f.folder_id) for f in folders]
    finally:
        db.close()

    total_new = 0
    folders_ok = 0
    folders_err = 0
    for wf_id, folder_id, label in folder_data:
        try:
            n = await _poll_folder(folder_id)
            db = SessionLocal()
            try:
                wf = db.query(WatchFolder).get(wf_id)
                if wf:
                    wf.last_polled_at = datetime.utcnow()
                    wf.last_new_count = n
                    wf.last_error     = ""
                    db.commit()
            finally:
                db.close()
            if n > 0:
                audit_log("auto_fetch", target=folder_id, status="ok",
                          message=f"{label}: {n} new pending",
                          details={"folder_id": folder_id, "new_count": n, "label": label},
                          user=SCHEDULER_USER)
                total_new += n
            folders_ok += 1
        except Exception as e:
            folders_err += 1
            err = str(e)[:500]
            db = SessionLocal()
            try:
                wf = db.query(WatchFolder).get(wf_id)
                if wf:
                    wf.last_polled_at = datetime.utcnow()
                    wf.last_error     = err
                    db.commit()
            finally:
                db.close()
            audit_log("auto_fetch", target=folder_id, status="error",
                      message=f"{label}: {err}",
                      details={"folder_id": folder_id, "label": label, "error": err},
                      user=SCHEDULER_USER)

    audit_log("scheduler_tick", target="poll",
              message=f"Polled {len(folder_data)} folder(s) → {total_new} new ({folders_ok} ok, {folders_err} err)",
              details={"folders": len(folder_data), "new": total_new,
                       "ok": folders_ok, "err": folders_err},
              user=SCHEDULER_USER)
    _last_tick_summary = {
        "at":         _last_heartbeat_at.isoformat(),
        "folders":    len(folder_data),
        "new":        total_new,
        "folders_ok": folders_ok,
        "folders_err": folders_err,
    }

    # Kick off auto-batch if anything new + no manual batch in flight.
    if total_new > 0:
        # Lazy import to avoid circular: routes imports usage which imports audit.
        from app.views.routes import run_batch_internal, _running
        if _running.get("batch"):
            audit_log("auto_batch", target="all", status="skipped",
                      message="Skipped auto-batch — manual batch already running",
                      user=SCHEDULER_USER)
            return
        try:
            result = await run_batch_internal(user=SCHEDULER_USER, source="scheduler")
            audit_log("auto_batch", target="all", status="ok",
                      message=f"Auto-batch result: {result}",
                      details=result, user=SCHEDULER_USER)
        except Exception as e:
            audit_log("auto_batch", target="all", status="error",
                      message=str(e)[:500], user=SCHEDULER_USER)


def start(interval_minutes: int = 15) -> None:
    global _scheduler, _interval_minutes
    if _scheduler is not None:
        return
    _interval_minutes = max(1, int(interval_minutes))
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        poll_all_folders,
        IntervalTrigger(minutes=_interval_minutes),
        id="poll_watch_folders",
        max_instances=1,   # don't overlap if a tick runs long
        coalesce=True,     # if missed ticks pile up, just run one catch-up
        next_run_time=datetime.utcnow(),  # run once at startup so first tick isn't delayed
    )
    _scheduler.start()
    _log.info(f"Automation scheduler started — polling every {_interval_minutes} min")


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None
