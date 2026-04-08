import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
from app.controllers.gemini_controller import run_gemini_batch
from app.controllers.mimir_controller import extract_folder_id, fetch_all_items, push_metadata_to_mimir
from app.database import get_db
from app.models.asset import Asset

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# In-memory task lock + active folder_id (single-instance tool)
_running: dict[str, bool] = {"fetch": False, "batch": False}
_active_folder_id: str = ""


# ── Views ──────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "folder_id": settings.FOLDER_ID,
        "mimir_url": settings.MIMIR_BASE_URL,
        "gemini_model": settings.GEMINI_MODEL,
    })


# ── API: Stats ─────────────────────────────────────────────────────────────────

@router.get("/api/stats")
async def get_stats(db: Session = Depends(get_db)):
    total = db.query(Asset).count()
    pending = db.query(Asset).filter(Asset.status == "pending").count()
    processing = db.query(Asset).filter(Asset.status == "processing").count()
    done = db.query(Asset).filter(Asset.status == "done").count()
    error = db.query(Asset).filter(Asset.status == "error").count()
    return {
        "total": total,
        "pending": pending,
        "processing": processing,
        "done": done,
        "error": error,
        "fetch_running": _running["fetch"],
        "batch_running": _running["batch"],
    }


# ── API: Assets list ───────────────────────────────────────────────────────────

@router.get("/api/assets")
async def list_assets(
    status: str = "all",
    page: int = 1,
    per_page: int = 50,
    db: Session = Depends(get_db),
):
    q = db.query(Asset)
    if status != "all":
        q = q.filter(Asset.status == status)
    total = q.count()
    assets = q.offset((page - 1) * per_page).limit(per_page).all()
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "items": [a.to_dict() for a in assets],
    }


@router.get("/api/assets/{item_id}")
async def get_asset(item_id: str, db: Session = Depends(get_db)):
    asset = db.query(Asset).filter(Asset.item_id == item_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return asset.to_dict()


@router.delete("/api/assets")
async def clear_assets(db: Session = Depends(get_db)):
    if _running["fetch"] or _running["batch"]:
        raise HTTPException(status_code=409, detail="Cannot clear while a task is running")
    count = db.query(Asset).count()
    db.query(Asset).delete()
    db.commit()
    return {"deleted": count}


@router.patch("/api/assets/{item_id}/reset")
async def reset_asset(item_id: str, db: Session = Depends(get_db)):
    asset = db.query(Asset).filter(Asset.item_id == item_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    asset.status = "pending"
    asset.error_log = ""
    db.commit()
    return {"ok": True}


# ── API: Fetch from Mimir (SSE) ────────────────────────────────────────────────

@router.post("/api/fetch")
async def start_fetch(folder_url: str = Body(..., embed=True)):
    global _active_folder_id
    if _running["fetch"]:
        raise HTTPException(status_code=409, detail="Fetch already running")
    if not settings.MIMIR_TOKEN:
        raise HTTPException(status_code=400, detail="MIMIR_TOKEN not set")
    try:
        _active_folder_id = extract_folder_id(folder_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _running["fetch"] = True
    return {"ok": True, "folder_id": _active_folder_id}


@router.get("/api/fetch/stream")
async def fetch_stream():
    async def generate():
        try:
            async for event in fetch_all_items(_active_folder_id):
                yield f"data: {json.dumps(event)}\n\n"
                await asyncio.sleep(0)
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        finally:
            _running["fetch"] = False

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── API: Gemini Batch (SSE) ────────────────────────────────────────────────────

@router.post("/api/batch")
async def start_batch():
    if _running["batch"]:
        raise HTTPException(status_code=409, detail="Batch already running")
    if not settings.GEMINI_API_KEY:
        raise HTTPException(status_code=400, detail="GEMINI_API_KEY not set")
    _running["batch"] = True
    return {"ok": True, "message": "Batch started — connect to /api/batch/stream"}


@router.get("/api/batch/stream")
async def batch_stream():
    async def generate():
        try:
            async for event in run_gemini_batch():
                yield f"data: {json.dumps(event)}\n\n"
                await asyncio.sleep(0)
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        finally:
            _running["batch"] = False

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── API: Push to Mimir ─────────────────────────────────────────────────────────

@router.post("/api/assets/{item_id}/push")
async def push_one(item_id: str):
    result = await push_metadata_to_mimir(item_id)
    if not result["ok"]:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


@router.post("/api/push-all")
async def push_all(db: Session = Depends(get_db)):
    """Push all 'done' assets to Mimir sequentially."""
    done_ids = [a.item_id for a in db.query(Asset).filter(Asset.status == "done").all()]

    async def generate():
        ok_count = errors = 0
        for item_id in done_ids:
            result = await push_metadata_to_mimir(item_id)
            if result["ok"]:
                ok_count += 1
            else:
                errors += 1
            yield f"data: {json.dumps({'item_id': item_id, 'ok': result['ok'], 'ok_total': ok_count, 'errors': errors, 'total': len(done_ids)})}\n\n"
            await asyncio.sleep(0.3)
        yield f"data: {json.dumps({'type': 'done', 'ok_total': ok_count, 'errors': errors})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
