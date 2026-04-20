import asyncio
import csv
import io
import json
import logging
import re as _re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import httpx

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
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
    context_urls: Optional[str] = None
    context_text: Optional[str] = None


class BulkUpdate(BaseModel):
    item_ids: List[str]
    fields: AssetUpdate


class BulkReanalyzeRequest(BaseModel):
    item_ids: List[str]
    context_urls: List[str] = []
    context_text: str = ""

from app.config import settings
from app.controllers.gemini_controller import run_gemini_batch, _analyze_one as _gemini_analyze
from app.controllers.claude_controller import run_claude_batch, _analyze_one as _claude_analyze
from app.controllers.mimir_controller import discover_hires_folders, extract_folder_id, fetch_all_items, push_metadata_to_mimir
from app.controllers._shared import cache_stats, clear_image_cache, extract_event_from_path, extract_path_context

import urllib.parse
import xml.etree.ElementTree as _ET
from app.database import get_db
from app.models.asset import Asset
from app.models.person import Person

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# In-memory task lock + active folder_ids (multi-folder support)
_running: dict[str, bool] = {"fetch": False, "batch": False}
_cancel:  dict[str, bool] = {"batch": False}   # set True to stop batch between assets
_active_folder_ids: List[str] = []
_active_folder_contexts: List[str] = []
_batch_album_keys: List[str] = []   # empty = run all pending


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
        "root_path": settings.APP_ROOT_PATH.rstrip("/"),
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
    from app.controllers.gemini_controller import get_daily_usage

    provider = settings.AI_PROVIDER.lower()
    price_in, price_out = _get_pricing()
    model_name = settings.ANTHROPIC_MODEL if provider == "claude" else settings.GEMINI_MODEL

    row = db.query(
        func.sum(Asset.tokens_input).label("total_input"),
        func.sum(Asset.tokens_output).label("total_output"),
        func.count(Asset.item_id).filter(Asset.tokens_input != None).label("analyzed"),
    ).first()

    total_input  = row.total_input  or 0
    total_output = row.total_output or 0
    analyzed     = row.analyzed     or 0

    cost_input  = (total_input  / 1_000_000) * price_in
    cost_output = (total_output / 1_000_000) * price_out
    cost_total  = cost_input + cost_output

    avg_input  = round(total_input  / analyzed, 1) if analyzed else 0
    avg_output = round(total_output / analyzed, 1) if analyzed else 0

    # Daily usage (from DB — ใช้ได้กับทั้ง Gemini และ Claude)
    today = get_daily_usage()

    # Quota limits ตาม provider
    if provider == "claude":
        # Claude paid — ไม่มี hard daily limit แต่แสดง usage วันนี้
        rpd_limit = None   # ไม่มี limit แบบ free tier
        tpd_limit = None
        rpd_pct   = 0.0
        tpd_pct   = 0.0
    else:
        rpd_limit = settings.FREE_TIER_RPD
        tpd_limit = settings.FREE_TIER_TPD
        rpd_pct   = round(today["requests"] / rpd_limit * 100, 1) if rpd_limit else 0
        tpd_pct   = round(today["tokens"]   / tpd_limit * 100, 1) if tpd_limit else 0

    return {
        "provider":      provider,
        "analyzed":      analyzed,
        "total_input":   int(total_input),
        "total_output":  int(total_output),
        "total_tokens":  int(total_input + total_output),
        "avg_input":     avg_input,
        "avg_output":    avg_output,
        "cost_input_usd":    round(cost_input,  6),
        "cost_output_usd":   round(cost_output, 6),
        "cost_total_usd":    round(cost_total,  6),
        "cost_total_thb":    round(cost_total * 34, 4),
        "model":             model_name,
        "price_input_per_m":  price_in,
        "price_output_per_m": price_out,
        "today_requests": today["requests"],
        "today_tokens":   today["tokens"],
        "rpd_limit":      rpd_limit,
        "tpd_limit":      tpd_limit,
        "rpd_pct":        rpd_pct,
        "tpd_pct":        tpd_pct,
        "warn_pct":       int(settings.FREE_TIER_WARN_PCT * 100),
    }


# ── API: Assets list ───────────────────────────────────────────────────────────



@router.get("/api/folders")
async def list_folders(db: Session = Depends(get_db)):
    """
    Return albums grouped by event name extracted from ingest_path.
    Each unique event = one album pill, even if they share the same folder_id.
    """
    rows = db.query(Asset.ingest_path).filter(Asset.ingest_path != "").all()

    counts: dict[str, int] = {}
    for (path,) in rows:
        key = extract_event_from_path(path) or "—"
        counts[key] = counts.get(key, 0) + 1

    result = sorted(
        [{"name": name, "album_key": name, "count": cnt} for name, cnt in counts.items()],
        key=lambda x: x["name"],
    )
    return result


@router.get("/api/assets")
async def list_assets(
    status: str = "all",
    folder_id: str = "all",
    album_key: str = "all",
    page: int = 1,
    per_page: int = 50,
    db: Session = Depends(get_db),
):
    q = db.query(Asset)
    if status != "all":
        q = q.filter(Asset.status == status)
    if folder_id != "all":
        q = q.filter(Asset.folder_id == folder_id)
    if album_key != "all":
        # Filter by event name segment within ingest_path
        q = q.filter(Asset.ingest_path.contains(album_key))
    total = q.count()
    assets = q.offset((page - 1) * per_page).limit(per_page).all()
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "items": [a.to_dict() for a in assets],
    }


@router.delete("/api/assets")
async def clear_assets(db: Session = Depends(get_db)):
    if _running["fetch"] or _running["batch"]:
        raise HTTPException(status_code=409, detail="Cannot clear while a task is running")
    count = db.query(Asset).count()
    db.query(Asset).delete()
    db.commit()
    # Also clear image cache — no point keeping files for deleted assets
    cache_deleted = clear_image_cache()
    return {"deleted": count, "cache_cleared": cache_deleted}


# ── API: Image cache management ────────────────────────────────────────────────


@router.get("/api/cache/stats")
async def get_cache_stats():
    return cache_stats()


@router.delete("/api/cache")
async def clear_cache(item_id: Optional[str] = None):
    deleted = clear_image_cache(item_id)
    stats = cache_stats()
    return {"deleted": deleted, **stats}


# ── API: News context search ────────────────────────────────────────────────────

@router.get("/api/search-context")
async def search_context(q: str, lang: str = "th"):
    """Search Google News RSS for relevant articles. Returns top 5 title+snippet+url."""
    if not q.strip():
        return {"results": []}
    encoded = urllib.parse.quote(q.strip())
    rss_url = f"https://news.google.com/rss/search?q={encoded}&hl={lang}&gl=TH&ceid=TH:th"
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(rss_url, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return {"results": [], "error": f"HTTP {r.status_code}"}
        root = _ET.fromstring(r.text)
        ns = {"media": "http://search.yahoo.com/mrss/"}
        items = root.findall(".//item")[:6]
        results = []
        for it in items:
            title = (it.findtext("title") or "").strip()
            link  = (it.findtext("link") or "").strip()
            desc  = (it.findtext("description") or "").strip()
            # Strip HTML from description
            import re as _re
            desc = _re.sub(r"<[^>]+>", " ", desc)
            desc = _re.sub(r"\s+", " ", desc).strip()[:300]
            source = (it.findtext("source") or "").strip()
            pub    = (it.findtext("pubDate") or "").strip()
            if title:
                results.append({"title": title, "url": link, "snippet": desc,
                                 "source": source, "pub": pub})
        return {"results": results}
    except Exception as e:
        logger.warning(f"News search failed for '{q}': {e}")
        return {"results": [], "error": str(e)}


# ── API: Report (cumulative lifetime stats) ────────────────────────────────────

@router.get("/api/report")
async def get_report(db: Session = Depends(get_db)):
    """Lifetime report: folders, assets done, tokens, cost, time."""
    from sqlalchemy import func, case
    price_in, price_out = _get_pricing()
    provider = settings.AI_PROVIDER.lower()
    model = settings.ANTHROPIC_MODEL if provider == "claude" else settings.GEMINI_MODEL

    # --- Global summary ---
    row = db.query(
        func.count(Asset.item_id).label("total"),
        func.sum(case((Asset.status == "done",    1), else_=0)).label("done"),
        func.sum(case((Asset.status == "pending", 1), else_=0)).label("pending"),
        func.sum(case((Asset.status == "error",   1), else_=0)).label("error"),
        func.sum(Asset.tokens_input).label("tokens_in"),
        func.sum(Asset.tokens_output).label("tokens_out"),
        func.min(Asset.processed_at).label("first_at"),
        func.max(Asset.processed_at).label("last_at"),
    ).first()

    total_in  = row.tokens_in  or 0
    total_out = row.tokens_out or 0
    cost_usd  = (total_in / 1e6) * price_in + (total_out / 1e6) * price_out

    # Elapsed time
    elapsed_sec = None
    if row.first_at and row.last_at:
        elapsed_sec = int((row.last_at - row.first_at).total_seconds())

    # --- Per-album & per-day aggregation ---
    rows = db.query(
        Asset.ingest_path, Asset.status,
        Asset.tokens_input, Asset.tokens_output, Asset.processed_at,
    ).all()

    albums: dict = {}
    day_map: dict = {}

    for path, status, ti, to, proc_at in rows:
        key = extract_event_from_path(path or "") or "—"
        if key not in albums:
            albums[key] = {"folder": key, "total": 0, "done": 0, "error": 0,
                           "tokens_in": 0.0, "tokens_out": 0.0}
        a = albums[key]
        a["total"] += 1
        if status == "done":
            a["done"] += 1
            a["tokens_in"]  += ti or 0
            a["tokens_out"] += to or 0
        elif status == "error":
            a["error"] += 1

        if proc_at and status == "done":
            day = proc_at.strftime("%Y-%m-%d")
            if day not in day_map:
                day_map[day] = {"date": day, "done": 0, "tokens": 0, "cost_usd": 0.0}
            day_map[day]["done"] += 1
            day_map[day]["tokens"] += int((ti or 0) + (to or 0))
            day_map[day]["cost_usd"] = round(
                day_map[day]["cost_usd"]
                + ((ti or 0) / 1e6) * price_in
                + ((to or 0) / 1e6) * price_out, 6)

    by_folder = []
    for a in sorted(albums.values(), key=lambda x: x["folder"]):
        c = (a["tokens_in"] / 1e6) * price_in + (a["tokens_out"] / 1e6) * price_out
        by_folder.append({
            "folder": a["folder"],
            "total": a["total"],
            "done": a["done"],
            "error": a["error"],
            "tokens_total": int(a["tokens_in"] + a["tokens_out"]),
            "cost_usd": round(c, 5),
            "cost_thb": round(c * 34, 3),
        })

    by_day = []
    for d in sorted(day_map.values(), key=lambda x: x["date"], reverse=True)[:60]:
        d["cost_thb"] = round(d["cost_usd"] * 34, 4)
        by_day.append(d)

    return {
        "generated_at": datetime.utcnow().isoformat(),
        "provider": provider,
        "model": model,
        "price_input_per_m":  price_in,
        "price_output_per_m": price_out,
        "summary": {
            "total_assets":   row.total   or 0,
            "total_done":     row.done    or 0,
            "total_pending":  row.pending or 0,
            "total_error":    row.error   or 0,
            "total_folders":  len(albums),
            "tokens_in":      int(total_in),
            "tokens_out":     int(total_out),
            "tokens_total":   int(total_in + total_out),
            "cost_usd":       round(cost_usd, 5),
            "cost_thb":       round(cost_usd * 34, 4),
            "first_processed": row.first_at.isoformat() if row.first_at else None,
            "last_processed":  row.last_at.isoformat()  if row.last_at  else None,
            "elapsed_sec":    elapsed_sec,
        },
        "by_folder": by_folder,
        "by_day": by_day,
    }


# ── API: Album stats (per-event breakdown) ─────────────────────────────────────

@router.get("/api/album-stats")
async def album_stats(db: Session = Depends(get_db)):
    """Return per-album stats: count, done, tokens, cost."""
    from sqlalchemy import func
    price_in, price_out = _get_pricing()

    rows = db.query(
        Asset.ingest_path,
        Asset.status,
        Asset.tokens_input,
        Asset.tokens_output,
    ).all()

    albums: dict[str, dict] = {}
    for path, status, ti, to in rows:
        key = extract_event_from_path(path or "") or "—"
        if key not in albums:
            albums[key] = {"name": key, "total": 0, "done": 0, "pending": 0, "error": 0,
                           "tokens_in": 0.0, "tokens_out": 0.0}
        a = albums[key]
        a["total"] += 1
        if status == "done":
            a["done"] += 1
            a["tokens_in"]  += ti or 0
            a["tokens_out"] += to or 0
        elif status == "error":
            a["error"] += 1
        else:
            a["pending"] += 1

    result = []
    for a in sorted(albums.values(), key=lambda x: x["name"]):
        cost = (a["tokens_in"] / 1e6) * price_in + (a["tokens_out"] / 1e6) * price_out
        result.append({**a,
                        "cost_usd": round(cost, 5),
                        "cost_thb": round(cost * 34, 3),
                        "tokens_total": int(a["tokens_in"] + a["tokens_out"])})
    return result


# ── API: Push by album (SSE) ────────────────────────────────────────────────────

class PushAlbumRequest(BaseModel):
    album_keys: List[str]


@router.post("/api/push/by-album")
async def push_by_album(body: PushAlbumRequest, db: Session = Depends(get_db)):
    """Push all done assets in selected albums."""
    if not body.album_keys:
        raise HTTPException(status_code=400, detail="album_keys is empty")

    # Collect done asset IDs that belong to selected albums
    all_done = db.query(Asset).filter(Asset.status == "done").all()
    selected: list[tuple[str, str]] = []  # (item_id, album_key)
    for asset in all_done:
        key = extract_event_from_path(asset.ingest_path or "") or "—"
        if key in body.album_keys:
            selected.append((asset.item_id, key))
    return {"ok": True, "total": len(selected), "item_ids": [s[0] for s in selected],
            "album_map": {k: v for k, v in selected}}


@router.get("/api/push/by-album/stream")
async def push_by_album_stream(item_ids: str, album_map: str = "{}"):
    """SSE stream: push a comma-separated list of item_ids."""
    ids = [i.strip() for i in item_ids.split(",") if i.strip()]
    try:
        amap: dict = json.loads(album_map)
    except Exception:
        amap = {}

    async def generate():
        ok_count = errors = 0
        album_stats_: dict[str, dict] = {}
        for item_id in ids:
            album = amap.get(item_id, "—")
            if album not in album_stats_:
                album_stats_[album] = {"ok": 0, "errors": 0}
            result = await push_metadata_to_mimir(item_id)
            if result["ok"]:
                ok_count += 1
                album_stats_[album]["ok"] += 1
            else:
                errors += 1
                album_stats_[album]["errors"] += 1
            yield f"data: {json.dumps({'item_id': item_id, 'album': album, 'ok': result['ok'], 'error': result.get('error',''), 'ok_total': ok_count, 'errors': errors, 'total': len(ids), 'album_stats': album_stats_})}\n\n"
            await asyncio.sleep(0.2)
        yield f"data: {json.dumps({'type': 'done', 'ok_total': ok_count, 'errors': errors, 'total': len(ids), 'album_stats': album_stats_})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Specific bulk routes must come BEFORE /{item_id} routes ───────────────────

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


@router.post("/api/assets/bulk-reanalyze")
async def bulk_reanalyze(body: BulkReanalyzeRequest, db: Session = Depends(get_db)):
    """Save context to selected assets and reset them to pending for re-analysis."""
    if not body.item_ids:
        raise HTTPException(status_code=400, detail="item_ids is empty")
    ctx_json = json.dumps(body.context_urls)
    updated = 0
    for item_id in body.item_ids:
        asset = db.query(Asset).filter(Asset.item_id == item_id).first()
        if not asset:
            continue
        if body.context_urls:
            asset.context_urls = ctx_json
        if body.context_text:
            asset.context_text = body.context_text
        asset.status = "pending"
        asset.error_log = ""
        updated += 1
    db.commit()
    return {"ok": True, "updated": updated}


# ── Specific sub-routes (must come BEFORE parameterized /{item_id} routes) ──────

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


class ReanalyzeRequest(BaseModel):
    context_urls: List[str] = []
    context_text: str = ""


# ── Per-asset routes (parameterized — must come AFTER specific routes) ─────────

@router.get("/api/assets/{item_id}")
async def get_asset(item_id: str, db: Session = Depends(get_db)):
    asset = db.query(Asset).filter(Asset.item_id == item_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return asset.to_dict()


@router.get("/api/assets/{item_id}/image")
async def proxy_image(item_id: str, db: Session = Depends(get_db)):
    """Proxy hi-res proxy image from Mimir with auth (falls back to thumbnail)."""
    asset = db.query(Asset).filter(Asset.item_id == item_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Not found")
    url = asset.proxy_url or asset.thumbnail_url or ""
    if not url:
        raise HTTPException(status_code=404, detail="No image URL")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url,
            headers={"x-mimir-cognito-id-token": f"Bearer {settings.MIMIR_TOKEN}"},
            follow_redirects=True)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail="Image fetch failed")
        return Response(content=r.content,
                        media_type=r.headers.get("content-type", "image/jpeg"))


@router.get("/api/assets/{item_id}/video")
async def stream_video(item_id: str, db: Session = Depends(get_db)):
    """Proxy video file from Mimir with auth headers so browser can play it."""
    asset = db.query(Asset).filter(Asset.item_id == item_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    url = asset.proxy_url or ""
    if not url:
        raise HTTPException(status_code=404, detail="No video URL available")

    ext = (asset.title or "").rsplit(".", 1)[-1].lower()
    ct_map = {"mp4": "video/mp4", "mov": "video/quicktime",
              "m4v": "video/mp4", "mxf": "video/x-mxf",
              "avi": "video/x-msvideo", "mkv": "video/webm"}
    content_type = ct_map.get(ext, "video/mp4")

    async def _stream():
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "GET", url,
                headers={"x-mimir-cognito-id-token": f"Bearer {settings.MIMIR_TOKEN}"},
                timeout=120,
            ) as r:
                async for chunk in r.aiter_bytes(65536):
                    yield chunk

    return StreamingResponse(_stream(), media_type=content_type)


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


@router.patch("/api/assets/{item_id}/reset")
async def reset_asset(item_id: str, db: Session = Depends(get_db)):
    asset = db.query(Asset).filter(Asset.item_id == item_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    asset.status = "pending"
    asset.error_log = ""
    db.commit()
    return {"ok": True}


@router.post("/api/assets/{item_id}/reanalyze")
async def reanalyze_asset(item_id: str, body: ReanalyzeRequest, db: Session = Depends(get_db)):
    """Re-analyze a single asset with optional article URLs + context text."""
    asset = db.query(Asset).filter(Asset.item_id == item_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    provider = settings.AI_PROVIDER.lower()
    if provider == "claude" and not settings.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=400, detail="ANTHROPIC_API_KEY not set")
    if provider == "gemini" and not settings.GEMINI_API_KEY:
        raise HTTPException(status_code=400, detail="GEMINI_API_KEY not set")

    # Save context and status — only overwrite context if caller provided new values
    if body.context_urls:
        asset.context_urls = json.dumps(body.context_urls)
    if body.context_text:
        asset.context_text = body.context_text
    asset.status = "processing"
    db.commit()
    db.refresh(asset)  # reload all attributes after commit

    analyze_fn = _claude_analyze if provider == "claude" else _gemini_analyze

    # Use the saved asset context (may be preserved from before this call)
    saved_context_urls = json.loads(asset.context_urls or "[]")
    saved_context_text = asset.context_text or ""

    try:
        async with httpx.AsyncClient() as client:
            result = await analyze_fn(client, asset,
                                      context_urls=saved_context_urls,
                                      context_text=saved_context_text)
        exif = result.get("_exif", {})
        kw   = result.get("keywords", [])

        _VIDEO_EXTS = (".mp4", ".mov", ".mxf", ".avi", ".m4v", ".mkv", ".webm", ".ts", ".mts")
        _is_video = (
            (asset.item_type or "").lower() == "video" or
            any((asset.title or "").lower().endswith(ext) for ext in _VIDEO_EXTS)
        )
        if not _is_video:
            asset.ai_title           = result.get("title", "")
        asset.ai_description         = result.get("description", "")
        asset.ai_category            = result.get("category", "")
        asset.ai_subcat              = result.get("subcat", "")
        asset.ai_keyword             = ", ".join(kw) if isinstance(kw, list) else str(kw)
        asset.ai_editorial_categories = result.get("editorial_categories", "")
        asset.ai_location            = result.get("location", "")
        asset.ai_persons             = result.get("persons", "")
        asset.ai_event_occasion      = result.get("event_occasion", "")
        asset.ai_emotion_mood        = result.get("emotion_mood", "")
        asset.ai_language            = result.get("language", "")
        asset.ai_episode_segment     = result.get("episode_segment", "")
        asset.ai_department          = result.get("department", "")
        _series = extract_path_context(asset.ingest_path or "")["series"]
        asset.ai_project_series      = _series or result.get("project_series", "")
        asset.ai_right_license       = result.get("right_license", "")
        asset.ai_deliverable_type    = result.get("deliverable_type", "")
        asset.ai_subject_tags        = result.get("subject_tags", "")
        asset.ai_technical_tags      = result.get("technical_tags", "")
        asset.ai_visual_attributes   = result.get("visual_attributes", "")
        asset.exif_photographer      = exif.get("photographer", "") or asset.exif_photographer
        asset.exif_camera_model      = exif.get("camera_model", "") or asset.exif_camera_model
        asset.exif_credit_line       = exif.get("credit_line", "") or asset.exif_credit_line
        asset.tokens_input           = result.get("_tokens_input", 0)
        asset.tokens_output          = result.get("_tokens_output", 0)
        asset.processed_at           = datetime.utcnow()
        asset.status                 = "done"
        asset.error_log              = ""
        db.commit()
        db.refresh(asset)
        return {"ok": True, "asset": asset.to_dict()}
    except Exception as exc:
        logger.error(f"Reanalyze error for {item_id}: {exc}", exc_info=True)
        try:
            asset.status    = "error"
            asset.error_log = str(exc)
            db.commit()
        except Exception:
            pass
        raise HTTPException(status_code=502, detail=str(exc))


# ── API: Fetch from Mimir (SSE, multi-folder) ─────────────────────────────────

class FetchRequest(BaseModel):
    folder_urls: List[str]
    context_texts: List[str] = []


@router.post("/api/fetch")
async def start_fetch(body: FetchRequest):
    global _active_folder_ids, _active_folder_contexts
    if _running["fetch"]:
        raise HTTPException(status_code=409, detail="Fetch already running")
    if not settings.MIMIR_TOKEN:
        raise HTTPException(status_code=400, detail="MIMIR_TOKEN not set")
    if not body.folder_urls:
        raise HTTPException(status_code=400, detail="No folder URLs provided")

    ids = []
    for url in body.folder_urls:
        url = url.strip()
        if not url:
            continue
        try:
            ids.append(extract_folder_id(url))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    if not ids:
        raise HTTPException(status_code=400, detail="No valid folder URLs")

    # Pad context_texts to match length of ids
    ctxs = list(body.context_texts) + [""] * len(ids)
    _active_folder_ids = ids
    _active_folder_contexts = ctxs[:len(ids)]
    _running["fetch"] = True
    return {"ok": True, "folder_ids": ids, "count": len(ids)}


@router.get("/api/fetch/stream")
async def fetch_stream():
    async def generate():
        total_input_folders = len(_active_folder_ids)
        try:
            # Expand each input folder into its Hires subfolders (if any)
            expanded: list[tuple[str, str]] = []  # [(folder_id, ctx_text)]
            for i, folder_id in enumerate(_active_folder_ids):
                ctx_text = _active_folder_contexts[i] if i < len(_active_folder_contexts) else ""
                yield f"data: {json.dumps({'type': 'scanning', 'folder_id': folder_id[:8], 'folder_index': i+1, 'folder_total': total_input_folders})}\n\n"
                hires = await discover_hires_folders(folder_id)
                if hires:
                    yield f"data: {json.dumps({'type': 'hires_found', 'count': len(hires), 'folder_id': folder_id[:8], 'names': [h['name'] for h in hires]})}\n\n"
                    for h in hires:
                        expanded.append((h["id"], ctx_text))
                else:
                    # Use original folder as-is (already a Hires folder or has no subfolders)
                    expanded.append((folder_id, ctx_text))

            total_folders = len(expanded)
            for j, (folder_id, ctx_text) in enumerate(expanded):
                yield f"data: {json.dumps({'type': 'folder_start', 'folder_id': folder_id[:8], 'folder_index': j+1, 'folder_total': total_folders})}\n\n"
                async for event in fetch_all_items(folder_id, context_text=ctx_text):
                    event["folder_index"] = j + 1
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


# ── API: Backfill proxy URLs for existing assets ───────────────────────────────

@router.post("/api/assets/backfill-proxy")
async def backfill_proxy(db: Session = Depends(get_db)):
    """Fetch proxy_url from Mimir for assets that don't have one yet."""
    missing = db.query(Asset).filter(
        (Asset.proxy_url == None) | (Asset.proxy_url == "")
    ).limit(500).all()

    if not missing:
        return {"ok": True, "updated": 0, "message": "ทุก asset มี proxy_url แล้ว"}

    async with httpx.AsyncClient(timeout=30) as client:
        updated = 0
        for asset in missing:
            try:
                r = await client.get(
                    f"{settings.MIMIR_BASE_URL}/api/v1/items/{asset.item_id}",
                    headers={"x-mimir-cognito-id-token": f"Bearer {settings.MIMIR_TOKEN}"},
                )
                if r.status_code == 200:
                    data = r.json()
                    proxy = data.get("proxy", "")
                    if proxy:
                        asset.proxy_url = proxy
                        updated += 1
            except Exception:
                pass
        db.commit()

    return {"ok": True, "updated": updated, "total_missing": len(missing)}


# ── API: AI Batch (SSE) — รองรับ Gemini และ Claude ────────────────────────────

class BatchStartRequest(BaseModel):
    album_keys: List[str] = []   # empty list = process all pending albums
    force: bool = False


@router.post("/api/batch")
async def start_batch(body: BatchStartRequest = BatchStartRequest()):
    global _batch_album_keys
    if _running["batch"] and not body.force:
        raise HTTPException(status_code=409, detail="Batch already running")
    provider = settings.AI_PROVIDER.lower()
    if provider == "claude" and not settings.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=400, detail="ANTHROPIC_API_KEY not set")
    if provider == "gemini" and not settings.GEMINI_API_KEY:
        raise HTTPException(status_code=400, detail="GEMINI_API_KEY not set")
    _batch_album_keys = body.album_keys
    _running["batch"] = True
    scope = f"{len(body.album_keys)} albums" if body.album_keys else "all pending"
    return {"ok": True, "provider": provider,
            "message": f"Batch started ({provider}, {scope}) — connect to /api/batch/stream"}


@router.delete("/api/batch/reset")
async def reset_batch_flag():
    """Force-reset stuck batch running flag (also signals cancel if running)."""
    _cancel["batch"] = True
    _running["batch"] = False
    return {"ok": True, "message": "Batch cancelled"}


@router.post("/api/batch/cancel")
async def cancel_batch():
    """Signal the running batch to stop after the current asset finishes."""
    _cancel["batch"] = True
    _running["batch"] = False
    return {"ok": True, "message": "Cancel signal sent"}


@router.get("/api/batch/stream")
async def batch_stream():
    provider = settings.AI_PROVIDER.lower()
    album_keys = list(_batch_album_keys)   # snapshot at stream open
    _cancel["batch"] = False               # reset cancel flag for this run

    async def generate():
        try:
            batch_fn = run_claude_batch if provider == "claude" else run_gemini_batch
            async for event in batch_fn(album_keys=album_keys or None, cancel_flag=_cancel):
                # Reset flag before yielding done so next POST /api/batch doesn't get 409
                if event.get("type") in ("done", "rate_limit", "cancelled"):
                    _running["batch"] = False
                yield f"data: {json.dumps(event)}\n\n"
                await asyncio.sleep(0)
                if event.get("type") in ("rate_limit", "cancelled"):
                    return
        except Exception as exc:
            _running["batch"] = False
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        finally:
            _running["batch"] = False  # safety net

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── API: Push to Mimir ─────────────────────────────────────────────────────────

@router.get("/api/debug/mimir/{item_id}")
async def debug_mimir_item(item_id: str, db: Session = Depends(get_db)):
    """Debug: compare what's in Mimir vs what we'd push."""
    from app.database import SessionLocal as _SL
    asset = db.query(Asset).filter(Asset.item_id == item_id).first()

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{settings.MIMIR_BASE_URL}/api/v1/items/{item_id}",
            headers={"x-mimir-cognito-id-token": f"Bearer {settings.MIMIR_TOKEN}"},
        )
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.text[:500])
        data = r.json()
        mimir_fd = data.get("metadata", {}).get("formData", {})

    our_payload = {}
    if asset:
        our_payload = {
            "default_title":              asset.ai_title or asset.title,
            "default_description":        asset.ai_description,
            "default_category":           asset.ai_category,
            "default_subCategory":        asset.ai_subcat,
            "default_keywords":           asset.ai_keyword,
            "default_rights":             asset.rights,
            "default_editorialCategories": asset.ai_editorial_categories,
            "default_location":           asset.ai_location,
            "default_persons":            asset.ai_persons,
            "default_episodeSegment":     asset.ai_episode_segment,
            "default_eventOccasion":      asset.ai_event_occasion,
            "default_emotionMood":        asset.ai_emotion_mood,
            "default_language":           asset.ai_language,
            "default_department":         asset.ai_department,
            "default_projectSeries":      asset.ai_project_series,
            "default_rightLicense":       asset.ai_right_license,
            "default_deliverableType":    asset.ai_deliverable_type,
            "default_subjectTags":        asset.ai_subject_tags,
            "default_technicalTags":      asset.ai_technical_tags,
            "default_visualAttributes":   asset.ai_visual_attributes,
            "default_photographer":       asset.exif_photographer,
            "default_cameraModel":        asset.exif_camera_model,
            "default_creditLine":         asset.exif_credit_line,
        }

    from app.controllers.mimir_controller import _MIMIR_UUID_FIELDS, _slug
    uuid_fields_would_send = {}
    if asset:
        for db_field, (uuid_key, transform) in _MIMIR_UUID_FIELDS.items():
            raw_val = getattr(asset, db_field, None)
            if raw_val:
                try:
                    uuid_fields_would_send[uuid_key] = {"value": transform(str(raw_val)), "from_db_field": db_field, "raw": raw_val}
                except Exception:
                    pass

    # Find UUID-keyed fields already in Mimir (non default_* keys that look like UUIDs)
    uuid_pat = _re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', _re.I)
    uuid_in_mimir = {k: v for k, v in mimir_fd.items() if uuid_pat.match(k)}

    return {
        "item_id": item_id,
        "mimir_formId": data.get("metadata", {}).get("formId", ""),
        "mimir_formData": mimir_fd,
        "mimir_keys": sorted(mimir_fd.keys()),
        "our_payload_keys": sorted(our_payload.keys()),
        "our_payload": our_payload,
        "uuid_in_mimir": uuid_in_mimir,
        "uuid_fields_would_send": uuid_fields_would_send,
        "fields_in_mimir_not_in_ours": [k for k in mimir_fd if k not in our_payload and not uuid_pat.match(k)],
        "fields_in_ours_not_in_mimir": [k for k in our_payload if k not in mimir_fd and our_payload[k]],
        "verdict": "Data IS in Mimir. Mimir UI only renders UUID-keyed fields (not default_* fields). Need UUID map for all dropdowns.",
    }


@router.get("/api/debug/mimir-discover-uuids")
async def discover_mimir_uuids(folder_id: str = "", pages: int = 3):
    """
    Search Mimir items and collect all UUID-keyed fields found.
    Items previously edited via Mimir UI will have UUID-keyed formData fields.
    Use folder_id param to search a specific folder, or leave blank to use config default.
    """
    uuid_pat = _re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', _re.I)
    all_uuid_fields: dict = {}   # uuid_key → {sample_value, from_item}
    headers = {"x-mimir-cognito-id-token": f"Bearer {settings.MIMIR_TOKEN}"}
    fid = folder_id.strip() or settings.FOLDER_ID or ""

    async with httpx.AsyncClient(timeout=30) as client:
        for page in range(pages):
            params: dict = {
                "searchString": "*",
                "itemsPerPage": 50,
                "from": page * 50,
                "includeSubfolders": "true",
                "includeFolders": "false",
                "readableMetadataFields": "false",
            }
            if fid:
                params["folderId"] = fid
            r = await client.get(f"{settings.MIMIR_BASE_URL}/api/v1/search", params=params, headers=headers)
            if r.status_code != 200:
                continue
            items = r.json().get("_embedded", {}).get("collection", [])
            for item in items:
                fd = item.get("metadata", {}).get("formData", {})
                for k, v in fd.items():
                    if uuid_pat.match(k) and k not in all_uuid_fields:
                        all_uuid_fields[k] = {"value": v, "item_id": item.get("id", "")[:12]}

    return {
        "discovered": all_uuid_fields,
        "count": len(all_uuid_fields),
        "note": "These are UUID-keyed fields found in Mimir items. Match them to field names by value patterns.",
    }


@router.get("/api/debug/mimir-schema")
async def debug_mimir_schema():
    """Debug: try to fetch Mimir form schema to find valid taxonomy values."""
    headers = {"x-mimir-cognito-id-token": f"Bearer {settings.MIMIR_TOKEN}"}
    results = {}
    async with httpx.AsyncClient(timeout=30) as client:
        for path in [
            "/api/v1/schemas",
            "/api/v1/schemas/default",
            "/api/v1/metadata-schemas",
            "/api/v1/forms",
            "/api/v1/forms/default",
        ]:
            try:
                r = await client.get(f"{settings.MIMIR_BASE_URL}{path}", headers=headers)
                results[path] = {"status": r.status_code, "body": r.json() if r.status_code == 200 else r.text[:500]}
            except Exception as e:
                results[path] = {"error": str(e)}
    return results


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


# ── Export ────────────────────────────────────────────────────────────────────

@router.get("/api/export/csv")
def export_csv(status: str = "all", db: Session = Depends(get_db)):
    q = db.query(Asset)
    if status != "all":
        q = q.filter(Asset.status == status)
    assets = q.order_by(Asset.ingest_path).all()

    fields = [
        ("item_id",                "Item ID"),
        ("status",                 "Status"),
        ("ingest_path",            "Ingest Path"),
        ("title",                  "Original Title"),
        ("item_type",              "Type"),
        ("ai_title",               "AI Title"),
        ("ai_description",         "Description"),
        ("ai_category",            "Category"),
        ("ai_subcat",              "Sub-category"),
        ("ai_editorial_categories","Editorial Categories"),
        ("ai_persons",             "Persons"),
        ("ai_location",            "Location"),
        ("ai_event_occasion",      "Event/Occasion"),
        ("ai_emotion_mood",        "Emotion/Mood"),
        ("ai_keyword",             "Keywords"),
        ("ai_subject_tags",        "Subject Tags"),
        ("ai_visual_attributes",   "Visual Attributes"),
        ("ai_language",            "Language"),
        ("ai_department",          "Department"),
        ("ai_project_series",      "Project/Series"),
        ("ai_episode_segment",     "Episode/Segment"),
        ("ai_right_license",       "Rights/License"),
        ("ai_deliverable_type",    "Deliverable Type"),
        ("ai_technical_tags",      "Technical Tags"),
        ("exif_photographer",      "Photographer (EXIF)"),
        ("exif_camera_model",      "Camera (EXIF)"),
        ("exif_iso",               "ISO"),
        ("exif_aperture",          "Aperture"),
        ("exif_shutter",           "Shutter"),
        ("exif_focal_length",      "Focal Length"),
        ("tokens_input",           "Tokens In"),
        ("tokens_output",          "Tokens Out"),
        ("processed_at",           "Processed At"),
    ]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([label for _, label in fields])
    for a in assets:
        writer.writerow([
            getattr(a, col, "") or "" for col, _ in fields
        ])

    filename = f"mimir_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        content="\ufeff" + buf.getvalue(),   # BOM for Excel Thai charset
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── People Directory ───────────────────────────────────────────────────────────

class PersonCreate(BaseModel):
    name:       str
    title:      Optional[str] = ""
    keywords:   Optional[str] = ""
    photo_data: Optional[str] = ""
    photo_mime: Optional[str] = "image/jpeg"

class PersonUpdate(BaseModel):
    name:       Optional[str] = None
    title:      Optional[str] = None
    keywords:   Optional[str] = None
    photo_data: Optional[str] = None
    photo_mime: Optional[str] = None


@router.get("/api/people")
def list_people(db: Session = Depends(get_db)):
    people = db.query(Person).order_by(Person.name).all()
    return [p.to_dict() for p in people]


@router.post("/api/people", status_code=201)
def create_person(body: PersonCreate, db: Session = Depends(get_db)):
    person = Person(
        name=body.name.strip(),
        title=(body.title or "").strip(),
        keywords=(body.keywords or "").strip(),
        photo_data=body.photo_data or "",
        photo_mime=body.photo_mime or "image/jpeg",
    )
    db.add(person)
    db.commit()
    db.refresh(person)
    return person.to_dict()


@router.patch("/api/people/{person_id}")
def update_person(person_id: int, body: PersonUpdate, db: Session = Depends(get_db)):
    person = db.query(Person).filter(Person.id == person_id).first()
    if not person:
        raise HTTPException(status_code=404, detail="Not found")
    if body.name       is not None: person.name       = body.name.strip()
    if body.title      is not None: person.title      = body.title.strip()
    if body.keywords   is not None: person.keywords   = body.keywords.strip()
    if body.photo_data is not None: person.photo_data = body.photo_data
    if body.photo_mime is not None: person.photo_mime = body.photo_mime
    db.commit()
    return person.to_dict()


@router.delete("/api/people/{person_id}")
def delete_person(person_id: int, db: Session = Depends(get_db)):
    person = db.query(Person).filter(Person.id == person_id).first()
    if not person:
        raise HTTPException(status_code=404, detail="Not found")
    db.delete(person)
    db.commit()
    return {"ok": True}


@router.get("/api/people/{person_id}/photo")
def get_person_photo(person_id: int, db: Session = Depends(get_db)):
    person = db.query(Person).filter(Person.id == person_id).first()
    if not person or not person.photo_data:
        raise HTTPException(status_code=404, detail="No photo")
    import base64
    return Response(
        content=base64.b64decode(person.photo_data),
        media_type=person.photo_mime or "image/jpeg",
        headers={"Cache-Control": "max-age=86400"},
    )
