import asyncio
import json
import logging
import re as _re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import httpx

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
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
from app.controllers.mimir_controller import discover_hires_folders, extract_folder_id, fetch_all_items, push_metadata_to_mimir, _auth_header
from app.controllers._shared import cache_stats, clear_image_cache, extract_event_from_path, extract_path_context
from app.services.cognito_auth import (
    force_refresh as _force_cognito_refresh,
    login as _cognito_login,
    logout as _cognito_logout,
    get_status as _cognito_status,
)
from app.services import google_auth as _google_auth
import secrets as _py_secrets

import urllib.parse
import xml.etree.ElementTree as _ET
from app.database import SessionLocal, get_db
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
# Timestamp of POST /api/batch — if stream never connects within STREAM_GRACE_SEC
# the flag auto-clears so the user isn't permanently locked out by a stuck flag.
_batch_started_at: Optional[float] = None
_STREAM_GRACE_SEC = 15.0


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


# ── API: Mimir reconnect (force new Cognito token) ────────────────────────────

@router.post("/api/reconnect")
async def reconnect_mimir():
    """Force a fresh Cognito authentication with Mimir."""
    if settings.MIMIR_TOKEN:
        return {"ok": True, "method": "static_token", "message": "Using static token — no refresh needed"}
    status = _cognito_status()
    if not status["active"] and not status["env_fallback"]:
        raise HTTPException(status_code=400, detail="No Mimir credentials — login first")
    try:
        await _force_cognito_refresh()
        return {"ok": True, "method": "cognito", "message": "Reconnected to Mimir successfully"}
    except Exception as e:
        logger.error(f"Mimir reconnect failed: {e}")
        raise HTTPException(status_code=502, detail=f"Reconnect failed: {e}")


# ── API: Mimir runtime login (20-min session, credentials never persisted) ────

class MimirLogin(BaseModel):
    username: str
    password: str

@router.post("/api/mimir-auth/login")
async def mimir_login(body: MimirLogin):
    if not settings.MIMIR_COGNITO_USER_POOL_ID or not settings.MIMIR_COGNITO_CLIENT_ID:
        raise HTTPException(status_code=400, detail="MIMIR_COGNITO_USER_POOL_ID / CLIENT_ID not configured in env")
    try:
        return await _cognito_login(body.username, body.password)
    except Exception as e:
        logger.warning(f"Mimir login failed for {body.username}: {e}")
        raise HTTPException(status_code=401, detail=f"Login failed: {e}")

@router.post("/api/mimir-auth/logout")
async def mimir_logout():
    return _cognito_logout()

@router.get("/api/mimir-auth/status")
async def mimir_auth_status():
    return _cognito_status()


# ── Google SSO gate (internal users only) ──────────────────────────────────────

@router.get("/auth/login", response_class=HTMLResponse)
async def auth_login(request: Request, error: str = ""):
    """Login portal — shows a 'Sign in with Google' button."""
    if not _google_auth.is_configured():
        return HTMLResponse(
            "<h1>SSO not configured</h1><p>GOOGLE_AUTH_CLIENT_ID / SECRET / REDIRECT_URI / SESSION_SECRET_KEY required</p>",
            status_code=500,
        )
    return templates.TemplateResponse("login.html", {
        "request": request,
        "root_path": settings.APP_ROOT_PATH.rstrip("/"),
        "error": error,
        "allowed_domain": settings.ALLOWED_EMAIL_DOMAIN,
    })


@router.get("/auth/start")
async def auth_start(request: Request):
    """Initiate the OAuth redirect to Google. Called when user clicks the portal button."""
    if not _google_auth.is_configured():
        raise HTTPException(status_code=500, detail="SSO not configured")
    state = _py_secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    return RedirectResponse(_google_auth.make_authorize_url(state))


@router.get("/auth/callback", response_class=HTMLResponse)
async def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return HTMLResponse(f"<h1>Auth error</h1><p>{error}</p>", status_code=400)
    expected = request.session.pop("oauth_state", None)
    if not code or not state or state != expected:
        return HTMLResponse("<h1>Invalid auth state</h1><p>Try logging in again.</p>", status_code=400)
    try:
        info = await _google_auth.exchange_code_for_user(code)
    except Exception as e:
        logger.warning(f"Google token exchange failed: {e}")
        return HTMLResponse(f"<h1>Token exchange failed</h1><p>{e}</p>", status_code=502)
    email = (info.get("email") or "").lower()
    if not info.get("email_verified", False):
        return HTMLResponse("<h1>Email not verified</h1>", status_code=403)
    if not _google_auth.email_allowed(email):
        return HTMLResponse(
            f"""<div style="font-family:system-ui;max-width:480px;margin:80px auto;padding:24px;border:1px solid #f5c6cb;background:#fff5f5;border-radius:8px">
                <h2 style="margin-top:0;color:#c1232b">Access denied</h2>
                <p><strong>{email}</strong> ไม่ใช่บัญชี <code>@{settings.ALLOWED_EMAIL_DOMAIN}</code></p>
                <p style="color:#777;font-size:.9em">เครื่องมือนี้สำหรับพนักงาน The Standard เท่านั้น</p>
                <a href="{(settings.APP_ROOT_PATH or '') + '/auth/login'}" style="display:inline-block;margin-top:8px">← ลองใหม่ด้วยอีเมลอื่น</a>
            </div>""",
            status_code=403,
        )
    request.session["user"] = {
        "email": email,
        "name": info.get("name", ""),
        "picture": info.get("picture", ""),
        "is_admin": _google_auth.email_is_admin(email),
    }
    return RedirectResponse(settings.APP_ROOT_PATH or "/")


@router.post("/auth/logout")
async def auth_logout(request: Request):
    request.session.pop("user", None)
    return {"ok": True}


@router.get("/auth/me")
async def auth_me(request: Request):
    u = request.session.get("user")
    if u:
        return {"authenticated": True, **u}
    return {"authenticated": False, "configured": _google_auth.is_configured()}


# ── Admin: manage allowed users ───────────────────────────────────────────────

def _require_admin(request: Request):
    u = request.session.get("user") or {}
    if not _google_auth.email_is_admin(u.get("email", "")):
        raise HTTPException(status_code=403, detail="Admin only")
    return u


@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(request: Request):
    _require_admin(request)
    return templates.TemplateResponse("admin_users.html", {
        "request": request,
        "root_path": settings.APP_ROOT_PATH.rstrip("/"),
    })


class AllowedUserAdd(BaseModel):
    email: str
    is_admin: bool = False


@router.get("/api/admin/users")
async def list_allowed_users(request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    from app.models.allowed_user import AllowedUser
    rows = db.query(AllowedUser).order_by(AllowedUser.email).all()
    env_emails = _google_auth._env_allowed_emails()
    env_admins = _google_auth._env_admin_emails()
    env_entries = sorted(env_emails - {r.email.lower() for r in rows})
    return {
        "env": [{"email": e, "is_admin": e in env_admins, "source": "env"} for e in env_entries],
        "db":  [r.to_dict() | {"source": "db"} for r in rows],
    }


@router.post("/api/admin/users")
async def add_allowed_user(body: AllowedUserAdd, request: Request, db: Session = Depends(get_db)):
    me = _require_admin(request)
    from app.models.allowed_user import AllowedUser
    email = body.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="invalid email")
    domain = settings.ALLOWED_EMAIL_DOMAIN.lower().lstrip("@")
    if domain and not email.endswith("@" + domain):
        raise HTTPException(status_code=400, detail=f"email must end with @{domain}")
    existing = db.query(AllowedUser).filter(AllowedUser.email == email).first()
    if existing:
        existing.is_admin = bool(body.is_admin)
        db.commit()
        return existing.to_dict()
    row = AllowedUser(email=email, is_admin=bool(body.is_admin), added_by=me.get("email", ""))
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.to_dict()


@router.delete("/api/admin/users/{user_id}")
async def remove_allowed_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    from app.models.allowed_user import AllowedUser
    row = db.query(AllowedUser).filter(AllowedUser.id == user_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    db.delete(row)
    db.commit()
    return {"ok": True}


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
    Return albums grouped by folder_id so each fetched folder URL = one album pill.
    """
    rows = db.query(Asset.folder_id, Asset.ingest_path, Asset.context_text).all()

    folders: dict[str, dict] = {}
    for folder_id, path, ctx_text in rows:
        fid = folder_id or "__unknown__"
        if fid not in folders:
            event_name = extract_event_from_path(path or "") or (ctx_text or "").split("\n")[0][:40] or fid[:8]
            folders[fid] = {
                "name": event_name,
                "album_key": fid,
                "count": 0,
                "context_text": ctx_text or "",
            }
        folders[fid]["count"] += 1

    result = sorted(folders.values(), key=lambda x: x["name"])
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
        q = q.filter(Asset.folder_id == album_key)
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
    # Auto-save report snapshot before deleting so history is never lost
    if db.query(Asset).filter(Asset.status == "done").count() > 0:
        await _save_report_snapshot(db)
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
    """Return per-album stats grouped by folder_id."""
    price_in, price_out = _get_pricing()

    rows = db.query(
        Asset.folder_id,
        Asset.ingest_path,
        Asset.context_text,
        Asset.status,
        Asset.tokens_input,
        Asset.tokens_output,
    ).all()

    albums: dict[str, dict] = {}
    for folder_id, path, ctx_text, status, ti, to in rows:
        fid = folder_id or "__unknown__"
        if fid not in albums:
            name = extract_event_from_path(path or "") or (ctx_text or "").split("\n")[0][:40] or fid[:8]
            albums[fid] = {"name": name, "album_key": fid, "total": 0, "done": 0, "pending": 0, "error": 0,
                           "tokens_in": 0.0, "tokens_out": 0.0}
        a = albums[fid]
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
    """Push all done assets in selected albums (album_keys = folder_ids)."""
    if not body.album_keys:
        raise HTTPException(status_code=400, detail="album_keys is empty")

    key_set = set(body.album_keys)
    all_done = db.query(Asset).filter(
        Asset.status == "done",
        Asset.folder_id.in_(key_set),
    ).all()
    selected = [(a.item_id, a.folder_id) for a in all_done]
    return {"ok": True, "total": len(selected), "item_ids": [s[0] for s in selected],
            "album_map": {s[0]: s[1] for s in selected}}


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
            headers=await _auth_header(),
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
                headers=await _auth_header(),
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
        try:
            _vs.index_asset(asset)
        except Exception as ve:
            logger.warning(f"Vector index skipped for {item_id}: {ve}")
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
    if not settings.MIMIR_TOKEN and not settings.MIMIR_USERNAME:
        raise HTTPException(status_code=400, detail="MIMIR_TOKEN or MIMIR_USERNAME/PASSWORD not set")
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
                    headers=await _auth_header(),
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


# ── API: Import individual Mimir items by URL ──────────────────────────────────

_ITEM_UUID_RE = _re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', _re.I)

class ImportItemsRequest(BaseModel):
    item_urls: List[str]
    context_text: str = ""


@router.post("/api/items/import")
async def import_items(body: ImportItemsRequest, db: Session = Depends(get_db)):
    """Import individual Mimir item URLs (or raw item IDs) into the DB as pending."""
    results = []
    async with httpx.AsyncClient(timeout=30) as client:
        for raw_url in body.item_urls:
            url = raw_url.strip()
            if not url:
                continue
            m = _ITEM_UUID_RE.search(url)
            if not m:
                results.append({"url": url, "ok": False, "error": "No item ID (UUID) found"})
                continue
            item_id = m.group(0)

            existing = db.query(Asset).filter(Asset.item_id == item_id).first()
            if existing:
                results.append({"url": url, "item_id": item_id, "ok": True, "status": "already_exists"})
                continue

            r = await client.get(
                f"{settings.MIMIR_BASE_URL}/api/v1/items/{item_id}",
                headers=await _auth_header(),
                follow_redirects=True,
            )
            if r.status_code != 200:
                results.append({"url": url, "item_id": item_id, "ok": False, "error": f"Mimir HTTP {r.status_code}"})
                continue

            item = r.json()
            fd  = item.get("metadata", {}).get("formData", {})
            tfd = item.get("technicalMetadata", {}).get("formData", {})
            db.add(Asset(
                item_id=item_id,
                folder_id="__imported__",
                thumbnail_url=item.get("thumbnail", ""),
                proxy_url=item.get("proxy", ""),
                status="pending",
                title=fd.get("title") or item.get("originalFileName", ""),
                item_type=item.get("itemType", ""),
                media_created_on=fd.get("mediaCreatedOn") or fd.get("createdOn", ""),
                file_type=tfd.get("technical_image_file_type") or item.get("mediaType", ""),
                width=str(tfd.get("technical_image_width", "")),
                height=str(tfd.get("technical_image_height", "")),
                aspect_ratio=tfd.get("technical_media_display_aspect_ratio", ""),
                filesize_mb=round(item["mediaSize"] / 1048576, 2) if item.get("mediaSize") else None,
                ingest_path=item.get("ingestSourceFullPath", ""),
                exif_url=item.get("exifTagsUrl", ""),
                rights="THE STANDARD/All Rights Reserved",
                context_text=body.context_text,
            ))
            results.append({"url": url, "item_id": item_id, "ok": True, "status": "imported"})

        db.commit()

    imported = sum(1 for r in results if r.get("status") == "imported")
    return {"ok": True, "imported": imported, "results": results}


# ── API: AI Batch (SSE) — รองรับ Gemini และ Claude ────────────────────────────

class BatchStartRequest(BaseModel):
    album_keys: List[str] = []   # empty list = process all pending albums
    force: bool = False


@router.get("/api/diag")
async def diagnostics(db: Session = Depends(get_db)):
    """Diagnostic endpoint — surface common batch-failure causes."""
    from app.controllers.gemini_controller import get_daily_usage, check_rate_limit
    provider = settings.AI_PROVIDER.lower()
    pending = db.query(Asset).filter(Asset.status == "pending").count()
    processing = db.query(Asset).filter(Asset.status == "processing").count()
    issues = []
    if provider == "gemini" and not settings.GEMINI_API_KEY:
        issues.append("GEMINI_API_KEY not set")
    if provider == "claude" and not settings.ANTHROPIC_API_KEY:
        issues.append("ANTHROPIC_API_KEY not set")
    if pending == 0:
        issues.append("No pending assets — run Fetch first")
    rate_msg = check_rate_limit() if provider == "gemini" else None
    if rate_msg:
        issues.append(f"Rate limit: {rate_msg}")
    if _running["batch"]:
        issues.append("Batch flag stuck (running=True) — POST /api/batch/reset to clear")
    return {
        "provider": provider,
        "model": settings.ANTHROPIC_MODEL if provider == "claude" else settings.GEMINI_MODEL,
        "gemini_key_set": bool(settings.GEMINI_API_KEY),
        "anthropic_key_set": bool(settings.ANTHROPIC_API_KEY),
        "google_sso_configured": _google_auth.is_configured(),
        "allowed_email_domain": settings.ALLOWED_EMAIL_DOMAIN,
        "pending_assets": pending,
        "processing_assets": processing,
        "batch_running": _running["batch"],
        "fetch_running": _running["fetch"],
        "daily_usage": get_daily_usage() if provider == "gemini" else None,
        "issues": issues,
        "ok": not issues,
    }


@router.post("/api/batch")
async def start_batch(body: BatchStartRequest = BatchStartRequest()):
    global _batch_album_keys, _batch_started_at
    import time as _time
    # Auto-clear a stuck flag if the previous POST never had a stream connect
    if (_running["batch"] and _batch_started_at
            and _time.time() - _batch_started_at > _STREAM_GRACE_SEC):
        logger.warning("Auto-clearing stuck batch flag (stream never connected)")
        _running["batch"] = False
    if _running["batch"] and not body.force:
        raise HTTPException(status_code=409, detail="Batch already running")
    provider = settings.AI_PROVIDER.lower()
    if provider == "claude" and not settings.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=400, detail="ANTHROPIC_API_KEY not set")
    if provider == "gemini" and not settings.GEMINI_API_KEY:
        raise HTTPException(status_code=400, detail="GEMINI_API_KEY not set")
    pending_count = SessionLocal().query(Asset).filter(Asset.status == "pending").count()
    if pending_count == 0:
        raise HTTPException(status_code=400, detail="No pending assets — กด Fetch ก่อน")
    _batch_album_keys = body.album_keys
    _running["batch"] = True
    _batch_started_at = _time.time()
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


_REPORTS_DIR = Path(__file__).parent.parent.parent / "data" / "reports"
_REPORTS_DIR.mkdir(parents=True, exist_ok=True)


async def _save_report_snapshot(db_session) -> Optional[str]:
    """Save current report as JSON to data/reports/. Returns filename or None on error."""
    from sqlalchemy import func, case as _case
    try:
        price_in, price_out = _get_pricing()
        provider = settings.AI_PROVIDER.lower()
        model = settings.ANTHROPIC_MODEL if provider == "claude" else settings.GEMINI_MODEL

        row = db_session.query(
            func.count(Asset.item_id).label("total"),
            func.sum(_case((Asset.status == "done",    1), else_=0)).label("done"),
            func.sum(_case((Asset.status == "pending", 1), else_=0)).label("pending"),
            func.sum(_case((Asset.status == "error",   1), else_=0)).label("error"),
            func.sum(Asset.tokens_input).label("tokens_in"),
            func.sum(Asset.tokens_output).label("tokens_out"),
            func.min(Asset.processed_at).label("first_at"),
            func.max(Asset.processed_at).label("last_at"),
        ).first()

        total_in  = row.tokens_in  or 0
        total_out = row.tokens_out or 0
        cost_usd  = (total_in / 1e6) * price_in + (total_out / 1e6) * price_out
        elapsed_sec = None
        if row.first_at and row.last_at:
            elapsed_sec = int((row.last_at - row.first_at).total_seconds())

        rows = db_session.query(
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
                "folder": a["folder"], "total": a["total"],
                "done": a["done"], "error": a["error"],
                "tokens_total": int(a["tokens_in"] + a["tokens_out"]),
                "cost_usd": round(c, 5), "cost_thb": round(c * 34, 3),
            })

        by_day = []
        for d in sorted(day_map.values(), key=lambda x: x["date"], reverse=True)[:60]:
            d["cost_thb"] = round(d["cost_usd"] * 34, 4)
            by_day.append(d)

        report = {
            "generated_at": datetime.utcnow().isoformat(),
            "provider": provider,
            "model": model,
            "price_input_per_m":  price_in,
            "price_output_per_m": price_out,
            "summary": {
                "total_assets":    row.total   or 0,
                "total_done":      row.done    or 0,
                "total_pending":   row.pending or 0,
                "total_error":     row.error   or 0,
                "total_folders":   len(albums),
                "tokens_in":       int(total_in),
                "tokens_out":      int(total_out),
                "tokens_total":    int(total_in + total_out),
                "cost_usd":        round(cost_usd, 5),
                "cost_thb":        round(cost_usd * 34, 4),
                "first_processed": row.first_at.isoformat() if row.first_at else None,
                "last_processed":  row.last_at.isoformat()  if row.last_at  else None,
                "elapsed_sec":     elapsed_sec,
            },
            "by_folder": by_folder,
            "by_day": by_day,
        }

        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = f"report_{ts}.json"
        (_REPORTS_DIR / filename).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        logger.info(f"Report saved: {filename}")

        # Push to Google Sheets if authenticated
        from app.services.sheets_service import is_connected, push_report_to_sheets
        if is_connected():
            import asyncio as _asyncio
            try:
                sheets_result = await _asyncio.get_event_loop().run_in_executor(
                    None, push_report_to_sheets, report
                )
                if sheets_result.get("ok"):
                    logger.info(f"Report pushed to Sheets: {sheets_result.get('url')}")
                else:
                    logger.warning(f"Sheets push failed: {sheets_result.get('error')}")
            except Exception as se:
                logger.warning(f"Sheets push error: {se}")

        return filename
    except Exception as e:
        logger.warning(f"Report auto-save failed: {e}")
        return None


@router.get("/api/batch/stream")
async def batch_stream():
    global _batch_started_at
    provider = settings.AI_PROVIDER.lower()
    album_keys = list(_batch_album_keys)   # snapshot at stream open
    _cancel["batch"] = False               # reset cancel flag for this run
    _batch_started_at = None               # stream connected → cancel watchdog

    async def generate():
        try:
            batch_fn = run_claude_batch if provider == "claude" else run_gemini_batch
            async for event in batch_fn(album_keys=album_keys or None, cancel_flag=_cancel):
                # Reset flag before yielding done so next POST /api/batch doesn't get 409
                if event.get("type") in ("done", "rate_limit", "cancelled"):
                    _running["batch"] = False
                if event.get("type") == "done":
                    db = SessionLocal()
                    try:
                        fname = await _save_report_snapshot(db)
                        if fname:
                            event["report_saved"] = fname
                    finally:
                        db.close()
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




# ── API: CSV export ───────────────────────────────────────────────────────────

@router.get("/api/report/export.csv")
async def export_report_csv(db: Session = Depends(get_db)):
    """Export full report as CSV — Summary + by_folder + by_day in one file."""
    import csv, io
    from fastapi.responses import StreamingResponse as _SR
    from sqlalchemy import func, case as _case

    price_in, price_out = _get_pricing()
    provider = settings.AI_PROVIDER.lower()
    model = settings.ANTHROPIC_MODEL if provider == "claude" else settings.GEMINI_MODEL

    row = db.query(
        func.count(Asset.item_id).label("total"),
        func.sum(_case((Asset.status == "done",    1), else_=0)).label("done"),
        func.sum(_case((Asset.status == "pending", 1), else_=0)).label("pending"),
        func.sum(_case((Asset.status == "error",   1), else_=0)).label("error"),
        func.sum(Asset.tokens_input).label("tokens_in"),
        func.sum(Asset.tokens_output).label("tokens_out"),
        func.min(Asset.processed_at).label("first_at"),
        func.max(Asset.processed_at).label("last_at"),
    ).first()

    rows = db.query(Asset.ingest_path, Asset.status,
                    Asset.tokens_input, Asset.tokens_output, Asset.processed_at).all()

    albums: dict = {}
    day_map: dict = {}
    for path, status, ti, to, proc_at in rows:
        key = extract_event_from_path(path or "") or "—"
        if key not in albums:
            albums[key] = {"folder": key, "total": 0, "done": 0, "error": 0,
                           "tokens_in": 0.0, "tokens_out": 0.0}
        albums[key]["total"] += 1
        if status == "done":
            albums[key]["done"] += 1
            albums[key]["tokens_in"]  += ti or 0
            albums[key]["tokens_out"] += to or 0
        elif status == "error":
            albums[key]["error"] += 1
        if proc_at and status == "done":
            day = proc_at.strftime("%Y-%m-%d")
            if day not in day_map:
                day_map[day] = {"date": day, "done": 0, "tokens": 0, "cost_usd": 0.0}
            day_map[day]["done"] += 1
            day_map[day]["tokens"] += int((ti or 0) + (to or 0))
            day_map[day]["cost_usd"] += ((ti or 0) / 1e6) * price_in + ((to or 0) / 1e6) * price_out

    total_in  = row.tokens_in  or 0
    total_out = row.tokens_out or 0
    cost_usd  = (total_in / 1e6) * price_in + (total_out / 1e6) * price_out
    elapsed_min = ""
    if row.first_at and row.last_at:
        elapsed_min = round((row.last_at - row.first_at).total_seconds() / 60, 1)

    buf = io.StringIO()
    w = csv.writer(buf)

    # ── Section 1: Summary ──
    w.writerow(["=== SUMMARY ==="])
    w.writerow(["Generated At", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")])
    w.writerow(["Provider", provider.upper()])
    w.writerow(["Model", model])
    w.writerow(["Total Assets", row.total or 0])
    w.writerow(["Done", row.done or 0])
    w.writerow(["Pending", row.pending or 0])
    w.writerow(["Error", row.error or 0])
    w.writerow(["Folders", len(albums)])
    w.writerow(["Tokens In", int(total_in)])
    w.writerow(["Tokens Out", int(total_out)])
    w.writerow(["Tokens Total", int(total_in + total_out)])
    w.writerow(["Cost USD", round(cost_usd, 5)])
    w.writerow(["Cost THB", round(cost_usd * 34, 4)])
    w.writerow(["First Processed", row.first_at.strftime("%Y-%m-%d %H:%M:%S") if row.first_at else ""])
    w.writerow(["Last Processed",  row.last_at.strftime("%Y-%m-%d %H:%M:%S")  if row.last_at  else ""])
    w.writerow(["Elapsed (min)", elapsed_min])
    w.writerow([])

    # ── Section 2: By Folder ──
    w.writerow(["=== BY FOLDER ==="])
    w.writerow(["Album / Folder", "Total", "Done", "Error", "Tokens Total", "Cost USD", "Cost THB"])
    for a in sorted(albums.values(), key=lambda x: x["folder"]):
        c = (a["tokens_in"] / 1e6) * price_in + (a["tokens_out"] / 1e6) * price_out
        w.writerow([a["folder"], a["total"], a["done"], a["error"],
                    int(a["tokens_in"] + a["tokens_out"]), round(c, 5), round(c * 34, 3)])
    w.writerow([])

    # ── Section 3: By Day ──
    w.writerow(["=== BY DAY ==="])
    w.writerow(["Date", "Done", "Tokens", "Cost USD", "Cost THB"])
    for d in sorted(day_map.values(), key=lambda x: x["date"], reverse=True):
        w.writerow([d["date"], d["done"], d["tokens"],
                    round(d["cost_usd"], 5), round(d["cost_usd"] * 34, 4)])

    buf.seek(0)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return _SR(
        iter([buf.getvalue().encode("utf-8-sig")]),  # utf-8-sig = BOM for Excel/Sheets
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=report_{ts}.csv"},
    )


@router.get("/api/reports/{filename}/export.csv")
async def export_saved_report_csv(filename: str):
    """Export a saved report JSON as CSV."""
    import csv, io
    from fastapi.responses import StreamingResponse as _SR

    p = _REPORTS_DIR / filename
    if not p.exists() or not p.name.startswith("report_") or not p.name.endswith(".json"):
        raise HTTPException(status_code=404, detail="Report not found")
    report = json.loads(p.read_text())
    s = report.get("summary", {})
    elapsed_min = round(s.get("elapsed_sec", 0) / 60, 1) if s.get("elapsed_sec") else ""

    buf = io.StringIO()
    w = csv.writer(buf)

    w.writerow(["=== SUMMARY ==="])
    w.writerow(["Generated At", report.get("generated_at", "")])
    w.writerow(["Provider", report.get("provider", "")])
    w.writerow(["Model", report.get("model", "")])
    for k, v in [("Total Assets", "total_assets"), ("Done", "total_done"),
                 ("Pending", "total_pending"), ("Error", "total_error"),
                 ("Folders", "total_folders"), ("Tokens In", "tokens_in"),
                 ("Tokens Out", "tokens_out"), ("Tokens Total", "tokens_total"),
                 ("Cost USD", "cost_usd"), ("Cost THB", "cost_thb"),
                 ("First Processed", "first_processed"), ("Last Processed", "last_processed")]:
        w.writerow([k, s.get(v, "")])
    w.writerow(["Elapsed (min)", elapsed_min])
    w.writerow([])

    w.writerow(["=== BY FOLDER ==="])
    w.writerow(["Album / Folder", "Total", "Done", "Error", "Tokens Total", "Cost USD", "Cost THB"])
    for f in report.get("by_folder", []):
        w.writerow([f.get("folder",""), f.get("total",0), f.get("done",0), f.get("error",0),
                    f.get("tokens_total",0), f.get("cost_usd",0), f.get("cost_thb",0)])
    w.writerow([])

    w.writerow(["=== BY DAY ==="])
    w.writerow(["Date", "Done", "Tokens", "Cost USD", "Cost THB"])
    for d in report.get("by_day", []):
        w.writerow([d.get("date",""), d.get("done",0), d.get("tokens",0),
                    d.get("cost_usd",0), d.get("cost_thb",0)])

    buf.seek(0)
    csv_name = filename.replace(".json", ".csv")
    return _SR(
        iter([buf.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={csv_name}"},
    )


# ── API: Saved reports ─────────────────────────────────────────────────────────

@router.get("/api/reports")
async def list_reports():
    """List all auto-saved batch report snapshots."""
    files = sorted(_REPORTS_DIR.glob("report_*.json"), reverse=True)
    result = []
    for f in files:
        try:
            data = json.loads(f.read_text())
            result.append({
                "filename": f.name,
                "generated_at": data.get("generated_at"),
                "provider": data.get("provider"),
                "model": data.get("model"),
                "total_assets": data.get("summary", {}).get("total_assets", 0),
                "total_done": data.get("summary", {}).get("total_done", 0),
                "cost_thb": data.get("summary", {}).get("cost_thb", 0),
                "total_folders": data.get("summary", {}).get("total_folders", 0),
            })
        except Exception:
            pass
    return result


@router.get("/api/reports/{filename}")
async def get_saved_report(filename: str):
    """Get a specific saved report by filename."""
    p = _REPORTS_DIR / filename
    if not p.exists() or not p.name.startswith("report_") or not p.name.endswith(".json"):
        raise HTTPException(status_code=404, detail="Report not found")
    return json.loads(p.read_text())



# ── API: Google Sheets OAuth ──────────────────────────────────────────────────

@router.get("/api/sheets/status")
async def sheets_status():
    from app.services.sheets_service import is_connected
    return {"connected": is_connected()}


@router.get("/api/sheets/auth")
async def sheets_auth():
    from app.services.sheets_service import get_auth_url
    try:
        url = get_auth_url()
        return {"url": url}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/sheets/callback")
async def sheets_callback(code: str = "", error: str = ""):
    from fastapi.responses import RedirectResponse
    from app.services.sheets_service import complete_auth
    root = settings.APP_ROOT_PATH.rstrip("/")
    if error or not code:
        return RedirectResponse(url=f"{root}/?sheets=error&msg={error or 'no_code'}")
    result = complete_auth(code)
    if result.get("ok"):
        return RedirectResponse(url=f"{root}/?sheets=connected")
    import urllib.parse as _up
    return RedirectResponse(url=f"{root}/?sheets=error&msg={_up.quote(result.get('error','unknown'))}")


@router.delete("/api/sheets/disconnect")
async def sheets_disconnect():
    from app.services.sheets_service import _TOKEN_FILE
    if _TOKEN_FILE.exists():
        _TOKEN_FILE.unlink()
    return {"ok": True}


# ── API: Push to Mimir ─────────────────────────────────────────────────────────

@router.get("/api/debug/mimir/{item_id}")
async def debug_mimir_item(item_id: str, db: Session = Depends(get_db)):
    """Debug: compare what's in Mimir vs what we'd push."""
    from app.database import SessionLocal as _SL
    asset = db.query(Asset).filter(Asset.item_id == item_id).first()

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{settings.MIMIR_BASE_URL}/api/v1/items/{item_id}",
            headers=await _auth_header(),
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
    headers = await _auth_header()
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
    headers = await _auth_header()
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


from app.services import vector_service as _vs


# ── API: Vector Search (Qdrant) ────────────────────────────────────────────────

@router.get("/api/vector/stats")
async def vector_stats():
    """สถิติ Vector DB — จำนวน vectors ที่ index แล้ว."""
    try:
        return _vs.collection_info()
    except Exception as e:
        return {"vectors_count": 0, "points_count": 0, "status": "unavailable", "error": str(e)}


@router.get("/api/vector/search")
async def vector_search(q: str, limit: int = 20, item_type: Optional[str] = None):
    """Semantic search ด้วย Vector DB — รองรับภาษาไทยและอังกฤษ."""
    if not q.strip():
        raise HTTPException(status_code=400, detail="Query is required")
    try:
        results = _vs.search(q.strip(), limit=min(limit, 100), item_type=item_type or None)
        return {"query": q, "results": results, "count": len(results)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Vector search error: {e}")


@router.post("/api/vector/index/{item_id}")
async def vector_index_one(item_id: str, db: Session = Depends(get_db)):
    """Index asset เดี่ยวเข้า Vector DB."""
    asset = db.query(Asset).filter(Asset.item_id == item_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    try:
        ok = _vs.index_asset(asset)
        return {"ok": ok, "item_id": item_id}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Vector index error: {e}")


@router.post("/api/vector/index-all")
async def vector_index_all(db: Session = Depends(get_db)):
    """Index assets ทั้งหมดที่ status=done เข้า Vector DB (SSE stream)."""
    from app.database import SessionLocal as _SL
    asset_ids = [a.item_id for a in db.query(Asset).filter(Asset.status == "done").all()]
    total = len(asset_ids)

    async def generate():
        indexed = errors = 0
        for i, item_id in enumerate(asset_ids):
            _db = _SL()
            try:
                asset = _db.query(Asset).filter(Asset.item_id == item_id).first()
                if asset and _vs.index_asset(asset):
                    indexed += 1
            except Exception as e:
                errors += 1
                logger.warning(f"Vector index error for {item_id}: {e}")
            finally:
                _db.close()
            yield f"data: {json.dumps({'processed': i+1, 'indexed': indexed, 'errors': errors, 'total': total})}\n\n"
            await asyncio.sleep(0)
        yield f"data: {json.dumps({'type': 'done', 'indexed': indexed, 'errors': errors, 'total': total})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.delete("/api/vector/{item_id}")
async def vector_delete(item_id: str):
    """ลบ asset ออกจาก Vector index."""
    try:
        _vs.delete_asset(item_id)
        return {"ok": True, "item_id": item_id}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Vector delete error: {e}")


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
