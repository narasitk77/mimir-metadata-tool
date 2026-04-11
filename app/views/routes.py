import asyncio
import json
import logging
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session


class AssetUpdate(BaseModel):
    ai_title: Optional[str] = None
    ai_description: Optional[str] = None
    ai_category: Optional[str] = None
    ai_subcat: Optional[str] = None
    ai_keyword: Optional[str] = None
    ai_editorial_categories: Optional[str] = None
    ai_location: Optional[str] = None
    ai_persons: Optional[str] = None
    ai_episode_segment: Optional[str] = None
    ai_event_occasion: Optional[str] = None
    ai_emotion_mood: Optional[str] = None
    ai_language: Optional[str] = None
    ai_department: Optional[str] = None
    ai_project_series: Optional[str] = None
    ai_right_license: Optional[str] = None
    ai_deliverable_type: Optional[str] = None
    ai_subject_tags: Optional[str] = None
    ai_technical_tags: Optional[str] = None
    ai_visual_attributes: Optional[str] = None
    exif_photographer: Optional[str] = None
    exif_camera_model: Optional[str] = None
    exif_credit_line: Optional[str] = None
    rights: Optional[str] = None


class BulkUpdate(BaseModel):
    item_ids: List[str]
    fields: AssetUpdate

from app.config import settings
from app.controllers.gemini_controller import run_gemini_batch
from app.controllers.claude_controller import run_claude_batch
from app.controllers.mimir_controller import extract_folder_id, fetch_all_items, push_metadata_to_mimir
from app.database import get_db
from app.models.asset import Asset

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# In-memory task lock + active folder_ids (multi-folder support)
_running: dict[str, bool] = {"fetch": False, "batch": False}
_active_folder_ids: List[str] = []


# ── Views ──────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    provider = settings.AI_PROVIDER.lower()
    model = settings.ANTHROPIC_MODEL if provider == "claude" else settings.GEMINI_MODEL
    return templates.TemplateResponse("index.html", {
        "request": request,
        "mimir_url": settings.MIMIR_BASE_URL,
        "gemini_model": settings.GEMINI_MODEL,  # kept for compat
        "ai_provider": provider.title(),
        "ai_model": model,
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


# ── API: Token usage & cost ────────────────────────────────────────────────────

# Pricing per provider (USD per 1M tokens)
_PRICING = {
    "gemini": {"input": 0.15,  "output": 0.60},
    "claude": {"input": 0.80,  "output": 4.00},  # claude-haiku-4-5
}

def _get_pricing():
    p = _PRICING.get(settings.AI_PROVIDER.lower(), _PRICING["gemini"])
    return p["input"], p["output"]

PRICE_INPUT_PER_M, PRICE_OUTPUT_PER_M = _get_pricing()

@router.get("/api/token-stats")
async def get_token_stats(db: Session = Depends(get_db)):
    from sqlalchemy import func
    from datetime import datetime, date
    from app.controllers.gemini_controller import get_daily_usage

    row = db.query(
        func.sum(Asset.tokens_input).label("total_input"),
        func.sum(Asset.tokens_output).label("total_output"),
        func.count(Asset.item_id).filter(Asset.tokens_input != None).label("analyzed"),
    ).first()

    total_input  = row.total_input  or 0
    total_output = row.total_output or 0
    analyzed     = row.analyzed     or 0

    cost_input  = (total_input  / 1_000_000) * PRICE_INPUT_PER_M
    cost_output = (total_output / 1_000_000) * PRICE_OUTPUT_PER_M
    cost_total  = cost_input + cost_output

    avg_input  = round(total_input  / analyzed, 1) if analyzed else 0
    avg_output = round(total_output / analyzed, 1) if analyzed else 0

    today = get_daily_usage()
    rpd_pct = round(today["requests"] / settings.FREE_TIER_RPD * 100, 1)
    tpd_pct = round(today["tokens"]   / settings.FREE_TIER_TPD * 100, 1)

    return {
        "analyzed":      analyzed,
        "total_input":   int(total_input),
        "total_output":  int(total_output),
        "total_tokens":  int(total_input + total_output),
        "avg_input":     avg_input,
        "avg_output":    avg_output,
        "cost_input_usd":  round(cost_input,  6),
        "cost_output_usd": round(cost_output, 6),
        "cost_total_usd":  round(cost_total,  6),
        "cost_total_thb":  round(cost_total * 34, 4),
        "model":           settings.GEMINI_MODEL,
        "price_input_per_m":  PRICE_INPUT_PER_M,
        "price_output_per_m": PRICE_OUTPUT_PER_M,
        "today_requests": today["requests"],
        "today_tokens":   today["tokens"],
        "rpd_limit":      settings.FREE_TIER_RPD,
        "tpd_limit":      settings.FREE_TIER_TPD,
        "rpd_pct":        rpd_pct,
        "tpd_pct":        tpd_pct,
        "warn_pct":       int(settings.FREE_TIER_WARN_PCT * 100),
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


@router.patch("/api/assets/{item_id}")
async def update_asset(item_id: str, body: AssetUpdate, db: Session = Depends(get_db)):
    asset = db.query(Asset).filter(Asset.item_id == item_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(asset, field, value)
    if asset.status not in ("done", "error"):
        asset.status = "done"
    db.commit()
    return {"ok": True}


@router.post("/api/assets/bulk-edit")
async def bulk_edit(body: BulkUpdate, db: Session = Depends(get_db)):
    """อัพเดท fields เดียวกันให้ assets หลายตัวพร้อมกัน"""
    if not body.item_ids:
        raise HTTPException(status_code=400, detail="item_ids is empty")
    updated = 0
    fields = body.fields.model_dump(exclude_none=True)
    for item_id in body.item_ids:
        asset = db.query(Asset).filter(Asset.item_id == item_id).first()
        if not asset:
            continue
        for field, value in fields.items():
            setattr(asset, field, value)
        if asset.status not in ("done", "error"):
            asset.status = "done"
        updated += 1
    db.commit()
    return {"ok": True, "updated": updated}


@router.post("/api/assets/bulk-push")
async def bulk_push(item_ids: List[str] = Body(..., embed=True)):
    """Push หลาย assets ขึ้น Mimir พร้อมกัน (SSE)"""
    async def generate():
        ok_count = errors = 0
        for item_id in item_ids:
            result = await push_metadata_to_mimir(item_id)
            if result["ok"]:
                ok_count += 1
            else:
                errors += 1
            yield f"data: {json.dumps({'item_id': item_id, 'ok': result['ok'], 'ok_total': ok_count, 'errors': errors, 'total': len(item_ids)})}\n\n"
            await asyncio.sleep(0.3)
        yield f"data: {json.dumps({'type': 'done', 'ok_total': ok_count, 'errors': errors})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.patch("/api/assets/{item_id}/reset")
async def reset_asset(item_id: str, db: Session = Depends(get_db)):
    asset = db.query(Asset).filter(Asset.item_id == item_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    asset.status = "pending"
    asset.error_log = ""
    db.commit()
    return {"ok": True}


# ── API: Fetch from Mimir (SSE, multi-folder) ─────────────────────────────────

@router.post("/api/fetch")
async def start_fetch(folder_urls: List[str] = Body(..., embed=True)):
    global _active_folder_ids
    if _running["fetch"]:
        raise HTTPException(status_code=409, detail="Fetch already running")
    if not settings.MIMIR_TOKEN:
        raise HTTPException(status_code=400, detail="MIMIR_TOKEN not set")
    if not folder_urls:
        raise HTTPException(status_code=400, detail="No folder URLs provided")

    ids = []
    for url in folder_urls:
        url = url.strip()
        if not url:
            continue
        try:
            ids.append(extract_folder_id(url))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    if not ids:
        raise HTTPException(status_code=400, detail="No valid folder URLs")

    _active_folder_ids = ids
    _running["fetch"] = True
    return {"ok": True, "folder_ids": ids, "count": len(ids)}


@router.get("/api/fetch/stream")
async def fetch_stream():
    async def generate():
        total_folders = len(_active_folder_ids)
        try:
            for i, folder_id in enumerate(_active_folder_ids):
                yield f"data: {json.dumps({'type': 'folder_start', 'folder_id': folder_id, 'folder_index': i+1, 'folder_total': total_folders})}\n\n"
                async for event in fetch_all_items(folder_id):
                    # แนบข้อมูล folder เข้าไปใน event
                    event["folder_index"] = i + 1
                    event["folder_total"] = total_folders
                    event["folder_id"] = folder_id[:8]
                    yield f"data: {json.dumps(event)}\n\n"
                    await asyncio.sleep(0)
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        finally:
            _running["fetch"] = False

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── API: AI Batch (SSE) — รองรับ Gemini และ Claude ────────────────────────────

@router.post("/api/batch")
async def start_batch():
    if _running["batch"]:
        raise HTTPException(status_code=409, detail="Batch already running")
    provider = settings.AI_PROVIDER.lower()
    if provider == "claude" and not settings.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=400, detail="ANTHROPIC_API_KEY not set")
    if provider == "gemini" and not settings.GEMINI_API_KEY:
        raise HTTPException(status_code=400, detail="GEMINI_API_KEY not set")
    _running["batch"] = True
    return {"ok": True, "provider": provider, "message": f"Batch started ({provider}) — connect to /api/batch/stream"}


@router.get("/api/batch/stream")
async def batch_stream():
    provider = settings.AI_PROVIDER.lower()

    async def generate():
        try:
            batch_fn = run_claude_batch if provider == "claude" else run_gemini_batch
            async for event in batch_fn():
                yield f"data: {json.dumps(event)}\n\n"
                await asyncio.sleep(0)
                if event.get("type") == "rate_limit":
                    return
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
