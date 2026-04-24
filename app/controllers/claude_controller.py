import asyncio
import base64
import json
import logging
import re
from datetime import datetime, date
from typing import AsyncGenerator, List, Optional

import httpx
from sqlalchemy import func
from app.config import settings
from app.database import SessionLocal
from app.models.asset import Asset
from app.services.cognito_auth import get_token as _get_token
from app.controllers._shared import (
    extract_event_from_path, extract_date_from_path, extract_path_context,
    fetch_best_image, parse_gps, reverse_geocode,
    search_news_context, fetch_article_context, has_clear_faces,
)

logger = logging.getLogger(__name__)


PROMPT = """\
You are a media metadata specialist for THE STANDARD (Thai news media).

Context:
- ไฟล์: "{title}"
- Event: "{event_name}"
- Path: "{clean_path}"
- ประเภท: "{item_type}"
- ช่างภาพ (EXIF): "{exif_photographer}"
- กล้อง: "{exif_camera_model}"
- GPS: "{gps_location}"

Return ONLY a valid JSON object — no markdown, no explanation:

{{
  "title": "YYYY.MM.DD_หัวข้อ_ชื่อบุคคล/แบรนด์ที่เป็น subject (ห้ามใช้ชื่อช่างภาพ)",
  "description": "คำบรรยายใต้ภาพข่าว 1 ประโยค รูปแบบ: [ชื่อเต็ม ตำแหน่ง] [กริยา] ณ [สถานที่]",
  "category": "Photo | Footage | Audio | Graphic | Deliverable",
  "subcat": "Portrait | Event | B-Roll | Drone | BTS | Interview | Press Conference | Protest | Document | Product",
  "editorial_categories": "Politics | Business | Entertainment | Lifestyle | Sport | Tech | World | Environment | Health",
  "location": "สถานที่ถ่าย",
  "persons": "ชื่อจาก shared_context/ข่าว/context เท่านั้น — ห้ามเดาจากใบหน้า ไม่รู้ใส่ตำแหน่ง หรือว่าง",
  "event_occasion": "ชื่องาน/โอกาส",
  "emotion_mood": "Happy | Serious | Tense | Celebratory | Neutral | Sad",
  "language": "Thai | English | Other",
  "subject_tags": "แท็กหัวข้อคั่นด้วย comma",
  "visual_attributes": "Wide shot, Close-up, Candid, Studio, Outdoor ฯลฯ คั่นด้วย comma",
  "episode_segment": "ชื่อ Episode/Segment (จาก path หรือว่าง)",
  "department": "Editorial | Marketing | Social Media | Video (จาก path)",
  "project_series": "ชื่อ project/series (จาก path หรือว่าง)",
  "right_license": "THE STANDARD/All Rights Reserved",
  "deliverable_type": "Hero Image | Thumbnail | Social Post | Story | Archive",
  "technical_tags": "RAW, HDR, Flash ฯลฯ คั่นด้วย comma (ว่างถ้าไม่มี)",
  "keywords": ["คำ1","คำ2","คำ3","คำ4","คำ5"]
}}

กฎ description — สำคัญที่สุด:
- Photo: 1 ประโยคสั้น "[ชื่อเต็ม ตำแหน่ง] [กริยา] ณ [สถานที่]" เช่น "เศรษฐา ทวีสิน นายกรัฐมนตรี ให้สัมภาษณ์สื่อมวลชน ณ ทำเนียบรัฐบาล"
- Footage (thumbnail frame): มีคน → "[ชื่อ] [กริยาที่เห็น] [รายการ/สถานที่]" | B-Roll → ระบุสิ่งที่เห็นตรงๆ
- ห้ามใส่วันที่ | ห้ามขึ้นต้นด้วย ภาพ/ฉาก/ในภาพ/ภาพนี้ | ห้ามใช้คำแสดงคุณค่า (สวยงาม, ยิ่งใหญ่, หรูหรา ฯลฯ)
- ห้ามเดากิจกรรมจาก props: เห็นร่ม ≠ เล่นชายหาด, เห็นไมค์ ≠ แสดงคอนเสิร์ต — บรรยายเฉพาะ action ที่เห็นชัด (ยืน/นั่ง/พูด)
- ใช้ภาษาไทยที่ถูกต้อง สะกดคำตามพจนานุกรมราชบัณฑิตยสภา

กฎ persons: ใส่เฉพาะชื่อที่มีหลักฐานจาก shared_context/ข่าว/context — ห้ามเดาจากใบหน้าในกรณีไม่แน่ใจ
กฎ subcat (Footage): นั่ง/ยืนพูดต่อกล้อง → Interview | สถานที่/วัตถุ → B-Roll | งานหลายคน → Event | บินโดรน → Drone
กฎ title: ลงท้ายด้วยชื่อบุคคล/แบรนด์ที่เป็น subject เท่านั้น ถ้าไม่รู้ชื่อให้ตัดออก ห้ามใช้ชื่อช่างภาพจาก EXIF
กฎ cross-reference: ข่าว/shared_context ระบุชื่อ/สถานที่/วันที่ → ใช้ยืนยัน persons, location, title\
"""


_GEMINI_PERSONS_PROMPT = """\
ดูภาพนี้แล้วระบุชื่อบุคคลทุกคนที่เห็นในภาพ
ใช้ face recognition ความรู้เรื่องบุคคลสาธารณะไทยและต่างประเทศ
Context เพิ่มเติม — Event: "{event_name}", Path: "{clean_path}"
{shared_hint}

Return ONLY valid JSON: {{"persons": "ชื่อคั่นด้วย comma (ว่างถ้าไม่มีคนในภาพหรือจำไม่ได้)"}}
ห้าม return อย่างอื่น\
"""


async def _gemini_get_persons(
    client: httpx.AsyncClient,
    image_b64: str,
    mime_type: str,
    event_name: str,
    clean_path: str,
    shared_context: str,
) -> str:
    """Ask Gemini to identify people in the image (Gemini has better face recognition than Claude)."""
    import os
    api_key = os.environ.get("GEMINI_API_KEY") or settings.GEMINI_API_KEY
    if not api_key:
        return ""

    shared_hint = f"Shared context: {shared_context}" if shared_context else ""
    prompt = _GEMINI_PERSONS_PROMPT.format(
        event_name=event_name,
        clean_path=clean_path,
        shared_hint=shared_hint,
    )
    payload = {
        "contents": [{"parts": [
            {"inlineData": {"mimeType": mime_type, "data": image_b64}},
            {"text": prompt},
        ]}],
        "generationConfig": {"temperature": 0.1},
    }
    try:
        resp = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{settings.GEMINI_MODEL}:generateContent",
            params={"key": api_key},
            json=payload,
            timeout=30,
        )
        if resp.status_code != 200:
            logger.warning(f"Gemini persons pre-pass {resp.status_code} — skipping")
            return ""
        body    = resp.json()
        raw     = body["candidates"][0]["content"]["parts"][0]["text"]
        cleaned = raw.replace("```json", "").replace("```", "").strip()
        result  = json.loads(cleaned)
        persons = result.get("persons", "").strip()
        if persons:
            logger.info(f"Gemini face pre-pass → '{persons}'")
        return persons
    except Exception as e:
        logger.warning(f"Gemini face pre-pass failed: {e}")
        return ""


VERIFY_PROMPT = """\
คุณเป็นผู้ตรวจสอบความถูกต้องของชื่อบุคคลในสื่อข่าวของ THE STANDARD

ภาพนี้มีบุคคลที่ระบบระบุเบื้องต้นว่า: "{initial_persons}"
Event/งาน: "{event_name}"
Path: "{clean_path}"

ข้อมูล Cross-reference ที่มี:
{shared_context_block}
{news_context_block}

งานของคุณ: ตรวจสอบแต่ละชื่อในรายการเบื้องต้น 2 รอบ

รอบที่ 1 — Face Verification:
- ดูใบหน้าในภาพอีกครั้งอย่างละเอียด
- แต่ละใบหน้าตรงกับชื่อที่ระบุมาจริงหรือไม่?
- ถ้าตรง: HIGH confidence / ถ้าไม่ชัด: LOW confidence

รอบที่ 2 — Context Verification:
- ชื่อนั้นปรากฏใน shared_context หรือข่าวหรือไม่?
- ถ้ามีหลักฐานสนับสนุน: เพิ่ม confidence / ถ้าไม่มี: ลด confidence

กฎตัดสิน:
- KEEP: face recognition ชัด + มีหลักฐานสนับสนุนจาก context/ข่าว
- KEEP: face recognition ชัดมาก (บุคคลสาธารณะที่รู้จักดี) แม้ไม่มีใน context
- REMOVE: face recognition ไม่ชัด หรือเดาจากบริบท ไม่ใช่จากใบหน้า
- REMOVE: ชื่ออยู่ใน context แต่ใบหน้าในภาพไม่ตรง

Return ONLY a valid JSON object (ห้าม return อย่างอื่น):
{{"verified_persons": "ชื่อที่ผ่านการตรวจสอบคั่นด้วย comma (ว่างถ้าไม่มีใครผ่าน)", "removed": "ชื่อที่ตัดออก", "reason": "เหตุผลสั้นๆ"}}\
"""


async def _verify_persons(
    client: httpx.AsyncClient,
    image_b64: str,
    mime_type: str,
    initial_persons: str,
    event_name: str,
    clean_path: str,
    shared_context: str,
    news_context: str,
) -> str:
    """Second-pass person verification — re-checks face + context before committing names."""
    if not initial_persons or not initial_persons.strip():
        return ""

    shared_block = f"[Shared context จากงานเดียวกัน]:\n{shared_context}" if shared_context else "[Shared context]: ไม่มี"
    news_block   = f"[ข่าว]:\n{news_context[:1500]}" if news_context else "[ข่าว]: ไม่มี"

    prompt = VERIFY_PROMPT.format(
        initial_persons=initial_persons,
        event_name=event_name,
        clean_path=clean_path,
        shared_context_block=shared_block,
        news_context_block=news_block,
    )

    try:
        import anthropic as _anthropic
        _api_key = settings.ANTHROPIC_API_KEY
        aclient = _anthropic.AsyncAnthropic(api_key=_api_key)
        msg = await aclient.messages.create(
            model=settings.CLAUDE_MODEL,
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": image_b64}},
                    {"type": "text",  "text": prompt},
                ],
            }],
        )
        raw = msg.content[0].text
        cleaned = raw.replace("```json", "").replace("```", "").strip()
        result  = json.loads(cleaned)
        verified = result.get("verified_persons", "").strip()
        removed  = result.get("removed", "")
        reason   = result.get("reason", "")
        if removed or reason:
            logger.info(f"Person verify — kept: '{verified}' | removed: '{removed}' | reason: {reason}")
        return verified
    except Exception as e:
        logger.warning(f"Person verification failed, using initial: {e}")
        return initial_persons


async def _fetch_exif(client: httpx.AsyncClient, asset: Asset) -> dict:
    exif_url = asset.exif_url
    if not exif_url:
        r = await client.get(
            f"{settings.MIMIR_BASE_URL}/api/v1/items/{asset.item_id}",
            headers={"x-mimir-cognito-id-token": f"Bearer {settings.MIMIR_TOKEN or await _get_token()}"},
            timeout=15,
        )
        if r.status_code == 200:
            exif_url = r.json().get("exifTagsUrl", "")
            db = SessionLocal()
            try:
                a = db.query(Asset).filter(Asset.item_id == asset.item_id).first()
                if a:
                    a.exif_url = exif_url
                    db.commit()
            finally:
                db.close()
    if not exif_url:
        return {}
    try:
        r2 = await client.get(exif_url, timeout=15)
        if r2.status_code == 200:
            return r2.json()
    except Exception as e:
        logger.warning(f"EXIF fetch failed for {asset.item_id}: {e}")
    return {}


def _parse_exif(exif: dict) -> dict:
    ifd0   = exif.get("EXIF:IFD0", {})
    exif_  = exif.get("EXIF:ExifIFD", {})
    qt     = exif.get("EXIF:QuickTime", {})     # .mp4 / .mov
    xmp    = exif.get("EXIF:XMP", {})           # .mxf / .mts
    comp   = exif.get("EXIF:Composite", {})     # computed fields

    # Camera/operator — try IFD0 then QuickTime then XMP
    make  = ifd0.get("Make", "") or qt.get("Make", "") or xmp.get("xmpDM:cameraModel", "")
    model = ifd0.get("Model", "") or qt.get("Model", "") or ""
    camera = f"{make} {model}".strip()

    # Duration & fps (video-specific)
    duration = (comp.get("Duration") or qt.get("Duration") or
                xmp.get("xmpDM:duration") or "")
    fps = (comp.get("VideoFrameRate") or qt.get("VideoFrameRate") or
           xmp.get("xmpDM:videoFrameRate") or "")
    codec = (qt.get("CompressorName") or qt.get("VideoCodecID") or
             xmp.get("xmpDM:videoCompressor") or "")
    resolution = (f"{comp.get('ImageWidth', '')}x{comp.get('ImageHeight', '')}"
                  if comp.get("ImageWidth") else "")

    return {
        "photographer":  ifd0.get("Artist", "") or qt.get("Artist", ""),
        "camera_model":  camera,
        "credit_line":   ifd0.get("Copyright", "") or qt.get("Copyright", ""),
        "iso":           str(exif_.get("ISO", "")),
        "aperture":      f"f/{exif_.get('FNumber', '')}" if exif_.get("FNumber") else "",
        "shutter":       str(exif_.get("ExposureTime", "")),
        "focal_length":  str(exif_.get("FocalLength", "")),
        # video extras
        "duration":      str(duration),
        "fps":           str(fps),
        "codec":         str(codec),
        "resolution":    resolution,
    }


async def _analyze_one(client: httpx.AsyncClient, asset: Asset,
                        context_urls: List[str] = None,
                        context_text: str = "",
                        shared_context: str = "",
                        news_context: str = "") -> dict:
    # 1. EXIF
    exif_raw  = await _fetch_exif(client, asset)
    exif_data = _parse_exif(exif_raw)

    # 2. GPS → location name
    gps_coords = parse_gps(exif_raw)
    gps_location = ""
    if gps_coords:
        gps_location = await reverse_geocode(client, *gps_coords)
        if gps_location:
            logger.info(f"GPS resolved: {gps_coords} → {gps_location}")

    # 3. Fetch best available image (proxy hi-res → thumbnail fallback)
    image_bytes, mime_type = await fetch_best_image(
        client, asset, settings.MIMIR_BASE_URL
    )
    image_b64 = base64.b64encode(image_bytes).decode()
    # Claude accepts image/jpeg, image/png, image/gif, image/webp
    if mime_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        mime_type = "image/jpeg"

    # 4. Build path context first (needed for face pre-pass and prompt)
    path_parts = (asset.ingest_path or "").split("/")
    if len(path_parts) >= 2 and path_parts[0].upper() == "PHOTOGRAPHER":
        clean_path = "/".join(path_parts[2:])
    else:
        clean_path = asset.ingest_path or ""

    path_ctx   = extract_path_context(asset.ingest_path or "")
    event_name = path_ctx["event"]

    # 6. Build prompt
    series_name = path_ctx["series"]
    camera_id   = path_ctx["camera"]

    _VIDEO_EXTS = (".mp4", ".mov", ".mxf", ".avi", ".m4v", ".mkv", ".webm", ".ts", ".mts")
    is_video = (
        (asset.item_type or "").lower() == "video" or
        any((asset.title or "").lower().endswith(ext) for ext in _VIDEO_EXTS)
    )

    # Build video-specific technical info line
    video_tech = ""
    if is_video:
        parts = []
        if exif_data.get("duration"): parts.append(f"ความยาว: {exif_data['duration']}")
        if exif_data.get("fps"):      parts.append(f"fps: {exif_data['fps']}")
        if exif_data.get("codec"):    parts.append(f"codec: {exif_data['codec']}")
        if exif_data.get("resolution"): parts.append(f"resolution: {exif_data['resolution']}")
        video_tech = " | ".join(parts)

    prompt = PROMPT.format(
        title=asset.title or "",
        event_name=event_name,
        clean_path=clean_path,
        item_type="วิดีโอ/Footage" if is_video else (asset.item_type or "image"),
        exif_photographer=exif_data.get("photographer", ""),
        exif_camera_model=exif_data.get("camera_model", ""),
        gps_location=gps_location,
    )

    if is_video and video_tech:
        prompt += f"\n\n[Video Technical]: {video_tech}"

    if series_name:
        prompt += f"\n\n[Program/Series จาก path]: \"{series_name}\" — ใส่ใน project_series"

    if camera_id:
        prompt += f"\n\n[Camera ID จาก path]: \"{camera_id}\""

    if is_video:
        prompt += ("\n\n[หมายเหตุวิดีโอ]: ภาพนี้คือ thumbnail frame เดียวจากไฟล์วิดีโอ "
                   "ให้ใส่ category=\"Footage\" และ subcat ที่เหมาะสม "
                   "description ต้องบรรยายเฉพาะ action/สถานที่ที่เห็นชัดในเฟรมนี้เท่านั้น "
                   "ห้ามสรุปกิจกรรมหรือบริบทที่กว้างกว่าสิ่งที่ปรากฏจริงในภาพ")

    # Cross-asset context from same event (highest trust)
    if shared_context:
        prompt += f"\n\n[Cross-asset] ข้อมูลจากภาพอื่นในงานเดียวกัน — ใช้ยืนยันชื่อบุคคล/สถานที่:\n{shared_context}"

    # Auto news context — headlines + article full text
    if news_context:
        prompt += f"\n\n[News] ข่าวที่เกี่ยวข้อง (ค้นอัตโนมัติ) — ใช้ cross-reference ชื่อบุคคล ตำแหน่ง สถานที่ วันที่:\n{news_context}"

    # Manually attached article URLs
    article_ctx = await fetch_article_context(client, context_urls or [])
    if article_ctx:
        prompt += f"\n\n[บทความที่แนบมา]:\n{article_ctx}"

    # Free-text hint from user
    if context_text:
        prompt += f"\n\n[หมายเหตุจากผู้ใช้]: {context_text}"

    # 5. Claude API call
    import anthropic as _anthropic
    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY") or settings.ANTHROPIC_API_KEY
    claude = _anthropic.Anthropic(api_key=api_key)

    def _call_claude():
        return claude.messages.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=800,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": image_b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )

    resp = await asyncio.get_event_loop().run_in_executor(None, _call_claude)

    raw     = resp.content[0].text
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    result  = json.loads(cleaned)

    result["_tokens_input"]  = resp.usage.input_tokens
    result["_tokens_output"] = resp.usage.output_tokens

    result["_exif"] = exif_data
    return result


async def run_claude_batch(album_keys: list = None, cancel_flag: dict = None) -> AsyncGenerator[dict, None]:
    """
    album_keys: list of folder_ids to process. None / [] = process all pending.
    """
    db = SessionLocal()
    # Auto-recover assets stuck in "processing" from a previous crashed run
    stuck = db.query(Asset).filter(Asset.status == "processing").all()
    for a in stuck:
        a.status = "pending"
    if stuck:
        db.commit()
        logger.info(f"Auto-reset {len(stuck)} stuck 'processing' assets to pending")

    q = db.query(Asset).filter(Asset.status == "pending")
    if album_keys:
        q = q.filter(Asset.folder_id.in_(set(album_keys)))
    pending_assets = q.order_by(Asset.ingest_path).all()

    # Group by event so cross-asset context sharing works within each event
    event_order: dict[str, list[str]] = {}
    for a in pending_assets:
        ev = extract_event_from_path(a.ingest_path or "") or "__ungrouped__"
        event_order.setdefault(ev, []).append(a.item_id)

    # Flatten: process all assets of one event before moving to next
    pending_ids = [item_id for group in event_order.values() for item_id in group]
    total = len(pending_ids)
    db.close()

    processed = 0
    errors = 0
    idx = 0
    event_cache: dict[str, str] = {}       # event_name → cross-asset shared context
    news_cache:  dict[str, str] = {}       # event_name → Google News headlines

    async with httpx.AsyncClient() as client:
        while idx < len(pending_ids):
            # Check cancel flag between assets
            if cancel_flag and cancel_flag.get("batch"):
                yield {"type": "cancelled", "message": "ยกเลิกโดยผู้ใช้",
                       "processed": processed, "errors": errors, "total": total}
                return

            item_id = pending_ids[idx]

            db = SessionLocal()
            rate_limited = False
            try:
                asset = db.query(Asset).filter(Asset.item_id == item_id).first()
                if not asset or asset.status != "pending":
                    idx += 1
                    continue

                asset.status = "processing"
                db.commit()

                event = extract_event_from_path(asset.ingest_path or "") or "__ungrouped__"
                shared_ctx = event_cache.get(event, "")

                # Auto-search news once per event (cache result for remaining assets)
                if event != "__ungrouped__" and event not in news_cache:
                    date_hint = extract_date_from_path(asset.ingest_path or "")
                    news_cache[event] = await search_news_context(
                        client, event, date_hint=date_hint,
                        max_headlines=6, fetch_top_articles=2,
                    )
                news_ctx = news_cache.get(event, "")

                yield {"type": "progress", "processed": processed, "errors": errors,
                       "total": total, "current": asset.title or item_id}

                ctx_urls = json.loads(asset.context_urls or "[]") if asset.context_urls else []
                result = await _analyze_one(client, asset,
                                            context_urls=ctx_urls,
                                            context_text=asset.context_text or "",
                                            shared_context=shared_ctx,
                                            news_context=news_ctx)
                exif = result.get("_exif", {})
                kw   = result.get("keywords", [])

                _VIDEO_EXTS = (".mp4", ".mov", ".mxf", ".avi", ".m4v", ".mkv", ".webm", ".ts", ".mts")
                _is_video = (
                    (asset.item_type or "").lower() == "video" or
                    any((asset.title or "").lower().endswith(ext) for ext in _VIDEO_EXTS)
                )

                # Always overwrite AI-generated fields (fresh analysis)
                # Video: keep original filename as title (don't overwrite)
                if not _is_video:
                    asset.ai_title            = result.get("title", "")
                asset.ai_description          = result.get("description", "")
                asset.ai_category             = result.get("category", "")
                asset.ai_subcat               = result.get("subcat", "")
                asset.ai_keyword              = ", ".join(kw) if isinstance(kw, list) else str(kw)
                asset.ai_editorial_categories = result.get("editorial_categories", "")
                asset.ai_location             = result.get("location", "")
                asset.ai_persons              = result.get("persons", "")
                asset.ai_event_occasion       = result.get("event_occasion", "")
                asset.ai_emotion_mood         = result.get("emotion_mood", "")
                asset.ai_language             = result.get("language", "")
                asset.ai_episode_segment      = result.get("episode_segment", "")
                asset.ai_department           = result.get("department", "")
                # project_series: use folder name from path directly (deterministic)
                _series = extract_path_context(asset.ingest_path or "")["series"]
                asset.ai_project_series       = _series or result.get("project_series", "")
                asset.ai_right_license        = result.get("right_license", "")
                asset.ai_deliverable_type     = result.get("deliverable_type", "")
                asset.ai_subject_tags         = result.get("subject_tags", "")
                asset.ai_technical_tags       = result.get("technical_tags", "")
                asset.ai_visual_attributes    = result.get("visual_attributes", "")

                # EXIF: only fill if empty (EXIF doesn't change between re-analyses)
                def _set_if_empty(attr, val):
                    if not getattr(asset, attr, None):
                        setattr(asset, attr, val)

                _set_if_empty("exif_photographer", exif.get("photographer", ""))
                _set_if_empty("exif_camera_model", exif.get("camera_model", ""))
                _set_if_empty("exif_credit_line",  exif.get("credit_line", ""))
                _set_if_empty("exif_iso",          exif.get("iso", ""))
                _set_if_empty("exif_aperture",     exif.get("aperture", ""))
                _set_if_empty("exif_shutter",      exif.get("shutter", ""))
                _set_if_empty("exif_focal_length", exif.get("focal_length", ""))

                asset.tokens_input  = result.get("_tokens_input", 0)
                asset.tokens_output = result.get("_tokens_output", 0)
                asset.processed_at  = datetime.utcnow()
                asset.status        = "done"
                asset.error_log     = ""
                db.commit()

                try:
                    from app.services import vector_service as _vs
                    _vs.index_asset(asset)
                except Exception as _ve:
                    logger.warning(f"Vector index skipped for {asset.item_id[:8]}: {_ve}")

                # Accumulate cross-asset cache for this event (merge, don't replace)
                existing = event_cache.get(event, "")
                cmap: dict = {}
                for part in existing.split(" | "):
                    if ": " in part:
                        k, v = part.split(": ", 1)
                        cmap[k] = v
                if result.get("persons"):
                    key = "บุคคลที่พบในงาน"
                    if key in cmap:
                        old_names = {n.strip() for n in cmap[key].split(",") if n.strip()}
                        new_names = {n.strip() for n in result["persons"].split(",") if n.strip()}
                        cmap[key] = ", ".join(sorted(old_names | new_names))
                    else:
                        cmap[key] = result["persons"]
                if result.get("location"):       cmap["สถานที่"] = result["location"]
                if result.get("event_occasion"): cmap["ชื่องาน"]  = result["event_occasion"]
                if cmap:
                    event_cache[event] = " | ".join(f"{k}: {v}" for k, v in cmap.items())

                processed += 1
                idx += 1
                logger.info(f"[{processed}/{total}] done: {asset.ai_title}")

            except Exception as exc:
                err_str = str(exc)
                if "overloaded" in err_str.lower() or "529" in err_str or "rate" in err_str.lower():
                    logger.warning(f"Claude rate/overload — รอ 60s retry {item_id[:8]}")
                    try:
                        asset.status = "pending"
                        asset.error_log = ""
                        db.commit()
                    except Exception:
                        pass
                    rate_limited = True
                else:
                    errors += 1
                    idx += 1
                    logger.error(f"Error on {item_id}: {exc}")
                    try:
                        asset.status    = "error"
                        asset.error_log = err_str
                        db.commit()
                    except Exception:
                        pass
            finally:
                db.close()

            yield {"type": "progress", "processed": processed, "errors": errors, "total": total}

            if rate_limited:
                for remaining in range(60, 0, -10):
                    yield {"type": "progress", "processed": processed, "errors": errors,
                           "total": total, "current": f"⏳ Overloaded — รอ {remaining}s..."}
                    await asyncio.sleep(10)
            else:
                await asyncio.sleep(1)

    yield {"type": "done", "processed": processed, "errors": errors, "total": total}
