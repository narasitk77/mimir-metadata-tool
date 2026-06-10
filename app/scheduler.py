"""Automation scheduler — poll Mimir watch folders for new items, auto-batch.

Design:
  • APScheduler tick every N minutes (default 15) — `poll_all_folders()`
    - Each enabled WatchFolder → call `fetch_all_items()` which upserts Asset rows.
    - If any NEW pending appeared OR existing pending items remain → auto-batch.
    - This ensures items stuck as `pending` from a previous incomplete run are
      always retried on the next tick, not just when fresh items arrive.
  • Daily sweep job (configurable UTC hour, default 2 AM) — `daily_sweep()`
    - Polls all folders unconditionally, then forces a full Gemini batch on
      ALL pending items, guaranteeing daily completeness.
  • Audit log writes on every tick (heartbeat) + every fetch + every batch.
  • Global kill-switch (`set_paused(True)`) for emergency stop.

Safety: NO auto-push. Items move through pending → done. A human still
clicks "Push All" in the UI.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.audit import log as audit_log, set_current_user
from app.controllers.mimir_controller import fetch_all_items
from app.database import SessionLocal
from app.models.asset import Asset
from app.models.usage_history import UsageHistory
from app.models.watch_folder import WatchFolder

# Daily spend warning threshold (USD). Doesn't pause — just turns the banner
# yellow + writes an audit warning so a human notices. Override via env.
DAILY_WARN_USD = float(os.getenv("AUTOMATION_DAILY_WARN_USD", "5.0"))

_log = logging.getLogger(__name__)

_scheduler: Optional[AsyncIOScheduler] = None
_paused: bool = False
_interval_minutes: int = 15
_last_heartbeat_at: Optional[datetime] = None
_last_tick_summary: dict = {}
_cost_warned_date: Optional[str] = None  # YYYY-MM-DD — audit warns once per day
# External-trigger state (n8n mode): expose progress + result so the external
# scheduler can poll status instead of holding an hours-long HTTP connection.
_poll_running: bool = False
_sweep_running: bool = False
_last_sweep_summary: dict = {}

SCHEDULER_USER = "__scheduler__"


def is_paused() -> bool:
    return _paused


def set_paused(v: bool) -> None:
    global _paused
    _paused = bool(v)
    audit_log("automation_pause" if _paused else "automation_resume",
              target="all", message="Automation " + ("paused" if _paused else "resumed"),
              user=SCHEDULER_USER)


def heartbeat_age_seconds() -> Optional[int]:
    if _last_heartbeat_at is None:
        return None
    return int((datetime.utcnow() - _last_heartbeat_at).total_seconds())


def is_healthy() -> bool:
    """Healthy = scheduler exists, .running, and heartbeat is recent.

    Heartbeat tolerance = 2× interval + 1 min slack — gives APScheduler room
    to delay a tick under load without flapping the health signal.

    External (n8n) mode: the internal scheduler is intentionally off, so the
    absence of a running scheduler is healthy by definition.
    """
    from app.config import settings as _cfg
    if not _cfg.AUTOMATION_SCHEDULER_ENABLED:
        return True
    if _scheduler is None or not _scheduler.running:
        return False
    if _paused:
        # Paused is intentional → still report healthy (the user did this).
        return True
    age = heartbeat_age_seconds()
    if age is None:
        return True  # not run yet — give it grace until first tick
    return age < (_interval_minutes * 60 * 2 + 60)


def ensure_running(interval_minutes: Optional[int] = None) -> dict:
    """Self-heal: if scheduler was never started OR died, (re)start it.

    Called on every `/api/automation/status` request so the moment a user
    opens the Automation modal or the navbar polls the dot, we recover.
    Returns {restarted: bool, healthy: bool}.

    External (n8n) mode: never start/restart — the internal scheduler must
    stay off or it would double-trigger alongside the external one.
    """
    from app.config import settings as _cfg
    if not _cfg.AUTOMATION_SCHEDULER_ENABLED:
        return {"restarted": False, "healthy": True}
    iv = interval_minutes if interval_minutes is not None else _interval_minutes
    restarted = False
    if _scheduler is None:
        start(iv)
        restarted = True
    elif not _scheduler.running:
        try:
            stop()
        except Exception:
            pass
        start(iv)
        restarted = True
    if restarted:
        audit_log("scheduler_restart", target="auto",
                  message="Scheduler was not running — auto-restarted",
                  details={"interval_minutes": iv}, user=SCHEDULER_USER)
    return {"restarted": restarted, "healthy": is_healthy()}


def _today_cost_usd() -> float:
    """Sum usage_history.cost_usd for the current UTC day."""
    db = SessionLocal()
    try:
        from sqlalchemy import func
        start_of_day = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        total = db.query(func.coalesce(func.sum(UsageHistory.cost_usd), 0.0)).filter(
            UsageHistory.timestamp >= start_of_day
        ).scalar()
        return float(total or 0.0)
    except Exception:
        return 0.0
    finally:
        db.close()


def status() -> dict:
    """Snapshot for the UI's automation panel + health dashboard.

    Also consumed by the external scheduler (n8n): poll_running/sweep_running
    tell it when an async-triggered job finishes, last_sweep carries the
    numbers for the notification message.
    """
    from app.config import settings as _cfg
    today_cost = _today_cost_usd()
    return {
        "running":             _scheduler is not None and _scheduler.running,
        "scheduler_enabled":   _cfg.AUTOMATION_SCHEDULER_ENABLED,
        "mode":                "internal" if _cfg.AUTOMATION_SCHEDULER_ENABLED else "n8n",
        "paused":              _paused,
        "healthy":             is_healthy(),
        "interval_minutes":    _interval_minutes,
        "last_heartbeat_at":   _last_heartbeat_at.isoformat() if _last_heartbeat_at else None,
        "heartbeat_age_sec":   heartbeat_age_seconds(),
        "last_tick":           _last_tick_summary,
        "poll_running":        _poll_running,
        "sweep_running":       _sweep_running,
        "last_sweep":          _last_sweep_summary,
        "today_cost_usd":      round(today_cost, 4),
        "today_cost_thb":      round(today_cost * 34, 2),
        "today_warn_usd":      DAILY_WARN_USD,
        "today_warn_exceeded": today_cost >= DAILY_WARN_USD,
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

    # Count pending items in DB (includes backlog from previous incomplete runs)
    db_pending = SessionLocal()
    try:
        pending_count = db_pending.query(Asset).filter(Asset.status == "pending").count()
    finally:
        db_pending.close()

    audit_log("scheduler_tick", target="poll",
              message=(f"Polled {len(folder_data)} folder(s) → {total_new} new, "
                       f"{pending_count} pending total ({folders_ok} ok, {folders_err} err)"),
              details={"folders": len(folder_data), "new": total_new,
                       "pending": pending_count,
                       "ok": folders_ok, "err": folders_err},
              user=SCHEDULER_USER)
    _last_tick_summary = {
        "at":          _last_heartbeat_at.isoformat(),
        "folders":     len(folder_data),
        "new":         total_new,
        "pending":     pending_count,
        "folders_ok":  folders_ok,
        "folders_err": folders_err,
    }

    # Daily cost warn — once per day, not on every tick. Doesn't pause.
    global _cost_warned_date
    today = _last_heartbeat_at.strftime("%Y-%m-%d")
    today_cost = _today_cost_usd()
    if today_cost >= DAILY_WARN_USD and _cost_warned_date != today:
        _cost_warned_date = today
        audit_log("automation_cost_warn", target="all", status="error",
                  message=(f"Today's spend ${today_cost:.4f} crossed warn "
                           f"threshold ${DAILY_WARN_USD:.2f} — automation still running"),
                  details={"today_cost_usd": round(today_cost, 4),
                           "warn_usd": DAILY_WARN_USD},
                  user=SCHEDULER_USER)

    # Kick off auto-batch when there are NEW items OR a pending backlog from a
    # previous incomplete run.  Previously this only fired on total_new > 0,
    # which meant stuck-pending items were never retried until fresh items arrived.
    if total_new > 0 or pending_count > 0:
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


async def daily_sweep() -> None:
    """Daily guaranteed-completeness sweep.

    Runs at a configured UTC hour (AUTOMATION_DAILY_HOUR, default 2 AM).
    Polls ALL enabled watch folders for new items, then forces a full Gemini
    batch on every pending asset regardless of whether new items were found.
    This catches:
      - New files added during the day that missed the interval poll
      - Assets stuck as 'pending' from rate-limit interruptions or crashes

    After processing completes, sends a Discord notification (if configured)
    so a human can review and push.
    """
    if _paused:
        _log.info("Daily sweep skipped — automation paused")
        return

    set_current_user(SCHEDULER_USER)
    audit_log("daily_sweep_start", target="all",
              message="Daily sweep starting — polling all folders + full batch",
              user=SCHEDULER_USER)

    # Snapshot done count before so we can report the delta at the end
    db_snap = SessionLocal()
    try:
        done_before = db_snap.query(Asset).filter(Asset.status == "done").count()
    finally:
        db_snap.close()

    # Poll all watch folders (fetches new items + auto-batches if anything pending)
    await poll_all_folders()
    new_found = _last_tick_summary.get("new", 0)

    # Second pass: process any items still pending (e.g. if poll's batch was skipped
    # because a manual batch was running at the time)
    db_check = SessionLocal()
    try:
        pending_count = db_check.query(Asset).filter(Asset.status == "pending").count()
    finally:
        db_check.close()

    if pending_count > 0:
        from app.views.routes import run_batch_internal, _running
        if not _running.get("batch"):
            try:
                result = await run_batch_internal(user=SCHEDULER_USER, source="daily_sweep")
                audit_log("daily_sweep_done", target="all", status="ok",
                          message=f"Daily sweep second-pass: {pending_count} pending processed — {result}",
                          details=result, user=SCHEDULER_USER)
            except Exception as e:
                audit_log("daily_sweep_done", target="all", status="error",
                          message=f"Daily sweep second-pass error: {str(e)[:500]}",
                          user=SCHEDULER_USER)

    # Final stats for the notification
    db_final = SessionLocal()
    try:
        done_after  = db_final.query(Asset).filter(Asset.status == "done").count()
        error_total = db_final.query(Asset).filter(Asset.status == "error").count()
    finally:
        db_final.close()

    newly_done = max(0, done_after - done_before)

    audit_log("daily_sweep_done", target="all",
              message=(f"Daily sweep complete — {newly_done} newly done, "
                       f"{error_total} errors, {new_found} new files found"),
              details={"newly_done": newly_done, "errors": error_total, "new_found": new_found},
              user=SCHEDULER_USER)

    # Discord notification — only when there's something for a human to review
    from app.config import settings as _cfg
    if _cfg.DISCORD_WEBHOOK_URL and (newly_done > 0 or new_found > 0):
        try:
            from app.services.discord import send_daily_summary
            await send_daily_summary(
                _cfg.DISCORD_WEBHOOK_URL,
                done=newly_done,
                errors=error_total,
                new_found=new_found,
                source="daily_sweep",
                app_url=_cfg.APP_BASE_URL or "",
            )
        except Exception as exc:
            _log.warning(f"Discord notification failed (non-fatal): {exc}")


def start(interval_minutes: int = 15) -> None:
    global _scheduler, _interval_minutes
    if _scheduler is not None:
        return
    _interval_minutes = max(1, int(interval_minutes))
    daily_hour   = int(os.getenv("AUTOMATION_DAILY_HOUR",   "2"))
    daily_minute = int(os.getenv("AUTOMATION_DAILY_MINUTE", "0"))

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        poll_all_folders,
        IntervalTrigger(minutes=_interval_minutes),
        id="poll_watch_folders",
        max_instances=1,   # don't overlap if a tick runs long
        coalesce=True,     # if missed ticks pile up, just run one catch-up
        next_run_time=datetime.utcnow(),  # run once at startup so first tick isn't delayed
    )
    _scheduler.add_job(
        daily_sweep,
        CronTrigger(hour=daily_hour, minute=daily_minute),
        id="daily_sweep",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    _log.info(
        f"Automation scheduler started — polling every {_interval_minutes} min, "
        f"daily sweep at {daily_hour:02d}:{daily_minute:02d} UTC"
    )


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None
