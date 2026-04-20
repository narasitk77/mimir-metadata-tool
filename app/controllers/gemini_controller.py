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
from app.controllers._shared import (
    extract_event_from_path, extract_date_from_path, extract_path_context,
    fetch_best_image, parse_gps, reverse_geocode,
    search_news_context, fetch_article_context,
)

logger = logging.getLogger(__name__)


PROMPT = """\
You are a media metadata specialist for THE STANDARD, a Thai news and media company.

ข้อมูล Context ของไฟล์นี้:
- ชื่อไฟล์เดิม: "{title}"
- ชื่องาน/Event (จาก path): "{event_name}"
- Path ในระบบ (ไม่รวมโฟลเดอร์ช่างภาพ): "{clean_path}"
- ประเภทไฟล์: "{item_type}"
- ช่างภาพ (จาก EXIF): "{exif_photographer}"
- กล้อง (จาก EXIF): "{exif_camera_model}"
- GPS Location: "{gps_location}"

วิเคราะห์ภาพประกอบกับ Context แล้ว return ONLY a valid JSON object:

{{
  "title": "ชื่อกระชับภาษาไทย รูปแบบ YYYY.MM.DD_หัวข้อ_ชื่อบุคคลในภาพหรือแบรนด์ (ไม่ใช่ชื่อช่างภาพ)",
  "description": "คำบรรยาย 1-2 ประโยค ภาษาไทย: ใคร ทำอะไร ที่ไหน — เฉพาะข้อเท็จจริงที่เห็นในภาพ ไม่ใส่วันที่",
  "category": "Photo หรือ Footage หรือ Audio หรือ Graphic หรือ Deliverable",
  "subcat": "Portrait หรือ Event หรือ B-Roll หรือ Drone หรือ BTS หรือ Interview หรือ Press Conference หรือ Protest หรือ Document หรือ Product",
  "editorial_categories": "Politics หรือ Business หรือ Entertainment หรือ Lifestyle หรือ Sport หรือ Tech หรือ World หรือ Environment หรือ Health",
  "location": "สถานที่ถ่าย เช่น สวนลุมพินี กรุงเทพมหานคร หรือ The Standard ออฟฟิศ",
  "persons": "ชื่อบุคคลที่เห็นใบหน้าชัดในภาพนี้เท่านั้น คั่นด้วย comma (ต้องเห็นใบหน้าจริงในภาพนี้ — ห้ามใส่ชื่อจาก context/ข่าวถ้าไม่เห็นหน้าในภาพ — ถ้าไม่มีคนในภาพให้ว่าง)",
  "event_occasion": "ชื่องานหรือโอกาสที่ถ่าย เช่น งานแถลงข่าว, พิธีมอบรางวัล",
  "emotion_mood": "Happy หรือ Serious หรือ Tense หรือ Celebratory หรือ Neutral หรือ Sad",
  "language": "Thai หรือ English หรือ Other",
  "subject_tags": "แท็กหัวข้อคั่นด้วย comma เช่น การเมือง, เศรษฐกิจ, สิ่งแวดล้อม",
  "visual_attributes": "ลักษณะภาพคั่นด้วย comma เช่น Wide shot, Close-up, Candid, Studio, Outdoor",
  "episode_segment": "ชื่อ Episode หรือ Segment ถ้ามี (ดูจาก path/ชื่อไฟล์ ถ้าไม่มีให้ว่าง)",
  "department": "แผนกที่เกี่ยวข้อง เช่น Editorial, Marketing, Social Media, Video (ดูจาก path)",
  "project_series": "ชื่อโปรเจคหรือซีรีส์ (ดูจาก path/ชื่อไฟล์ ถ้าไม่มีให้ว่าง)",
  "right_license": "THE STANDARD/All Rights Reserved (default ถ้าไม่มีข้อมูลอื่น)",
  "deliverable_type": "ประเภทงาน เช่น Hero Image, Thumbnail, Social Post, Story, Archive",
  "technical_tags": "แท็กเทคนิคคั่นด้วย comma เช่น RAW, HDR, Long Exposure, Flash (ถ้าไม่มีให้ว่าง)",
  "keywords": ["คำ1", "คำ2", "คำ3", "คำ4", "คำ5"]
}}

กฎการใช้ข้อมูล (เรียงตามความน่าเชื่อถือ):
1. ข้อมูลภาพ (visual evidence) — สิ่งที่เห็นในภาพเป็นจริงเสมอ
2. ข้อมูลจากภาพอื่นในงานเดียวกัน (shared_context) — ใช้ยืนยันชื่อบุคคล/สถานที่
3. ข่าวที่ค้นอัตโนมัติ — ใช้ cross-reference ชื่อบุคคล ตำแหน่ง สถานที่ และรายละเอียดงาน
4. บทความที่แนบมา — ใช้ประกอบบริบทเพิ่มเติม
5. หมายเหตุจากผู้ใช้ — ใช้เป็น hint เพิ่มเติม

การระบุบุคคล:
- ใส่ชื่อใน persons เฉพาะบุคคลที่เห็นใบหน้าชัดในภาพนี้เท่านั้น
- ห้ามใส่ชื่อเพราะชื่อนั้นอยู่ใน shared_context หรือข่าว — shared_context ใช้ยืนยันชื่อเท่านั้น ไม่ใช่เหตุผลให้ใส่
- ถ้าเห็นใบหน้าแต่จำชื่อไม่ได้และไม่มีหลักฐานจาก context → ใส่ตำแหน่ง/บทบาท เช่น "นักการเมือง"
- ถ้าภาพไม่มีคน หรือไม่เห็นใบหน้าชัดเจน → ปล่อยว่าง

การ Cross-reference และ Fact-check:
- ถ้าข่าวระบุชื่อบุคคลที่ตรงกับภาพ → ใส่ชื่อเต็มและตำแหน่งจากข่าวใน persons
- ถ้าข่าวระบุชื่องาน/สถานที่ → ใช้ชื่อที่ถูกต้องตามข่าวใน event_occasion และ location
- ถ้าข่าวมีวันที่ → ใช้ยืนยัน/ปรับ title format YYYY.MM.DD
- ถ้าข่าวหลายสำนักพูดถึงคนเดียวกัน → มั่นใจระบุชื่อนั้น
- ถ้าไม่มีข่าวหรือข่าวไม่ตรง → ระบุจากภาพและ context ที่มี ห้ามเดา

กฎ description:

Photo: "[ชื่อเต็ม ตำแหน่ง] [กริยา] [ชื่องาน/สถานที่]" — 1-2 ประโยค
- ตัวอย่าง: "เศรษฐา ทวีสิน นายกรัฐมนตรี ให้สัมภาษณ์สื่อมวลชน ณ ทำเนียบรัฐบาล กรุงเทพฯ"

Footage (thumbnail frame จากวิดีโอ): "[ใคร] [ทำอะไร] [ที่ไหน/รายการ]" — 1 ประโยค
- มีคน: "คุณธงชัย Noble นั่งสัมภาษณ์ รายการ The Secret Sauce"
- B-Roll: "ชายหาดและร่มบังแดด ริมทะเล"
- ชื่อรายการ (Program/Series จาก path) ใส่ใน description ไม่ใช่ใน keywords
- subcat Footage: Interview = คนพูดต่อกล้อง, B-Roll = ไม่มีคนเป็น subject, Event = หลายคน, Drone = บินโดรน

กฎห้าม:
- ห้ามใส่วันที่ใน description
- ห้ามขึ้นต้นด้วย "ภาพ", "ฉาก", "ในภาพ", "ภาพนี้"
- ห้ามใช้คำคุณศัพท์: สวยงาม, ยิ่งใหญ่, ประณีต, งดงาม, หรูหรา, ทันสมัย ฯลฯ
- ห้ามเดาหรือสรุปสิ่งที่ไม่เห็นชัดในภาพ
- ห้ามนำชื่อช่างภาพมาใส่ใน title

กฎอื่น ๆ:
- ถ้า "ชื่องาน/Event" ไม่ว่าง ให้ใช้เป็นข้อมูลหลักสำหรับ event_occasion และ title
- ถ้า "GPS Location" ไม่ว่าง ให้ใช้เป็น location
- title ท้ายด้วยชื่อบุคคลที่ปรากฏในภาพหรือแบรนด์ที่เป็น subject เท่านั้น
- keywords 5-10 คำ ครอบคลุมคน สถานที่ หัวข้อ และ action
- Return JSON only ห้าม return อย่างอื่นเด็ดขาด\
"""


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

    import os
    _api_key = os.environ.get("GEMINI_API_KEY") or settings.GEMINI_API_KEY
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
            params={"key": _api_key},
            json=payload,
            timeout=45,
        )
        if resp.status_code != 200:
            logger.warning(f"Person verify Gemini {resp.status_code} — using initial")
            return initial_persons
        body    = resp.json()
        raw     = body["candidates"][0]["content"]["parts"][0]["text"]
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
            headers={"x-mimir-cognito-id-token": f"Bearer {settings.MIMIR_TOKEN}"},
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
    qt     = exif.get("EXIF:QuickTime", {})
    xmp    = exif.get("EXIF:XMP", {})
    comp   = exif.get("EXIF:Composite", {})

    make  = ifd0.get("Make", "") or qt.get("Make", "") or xmp.get("xmpDM:cameraModel", "")
    model = ifd0.get("Model", "") or qt.get("Model", "") or ""
    camera = f"{make} {model}".strip()

    duration = (comp.get("Duration") or qt.get("Duration") or xmp.get("xmpDM:duration") or "")
    fps      = (comp.get("VideoFrameRate") or qt.get("VideoFrameRate") or xmp.get("xmpDM:videoFrameRate") or "")
    codec    = (qt.get("CompressorName") or qt.get("VideoCodecID") or xmp.get("xmpDM:videoCompressor") or "")
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
        client, asset, settings.MIMIR_BASE_URL, settings.MIMIR_TOKEN
    )
    image_b64 = base64.b64encode(image_bytes).decode()

    # 4. Build prompt
    path_parts = (asset.ingest_path or "").split("/")
    if len(path_parts) >= 2 and path_parts[0].upper() == "PHOTOGRAPHER":
        clean_path = "/".join(path_parts[2:])
    else:
        clean_path = asset.ingest_path or ""

    path_ctx    = extract_path_context(asset.ingest_path or "")
    event_name  = path_ctx["event"]
    series_name = path_ctx["series"]
    camera_id   = path_ctx["camera"]

    _VIDEO_EXTS = (".mp4", ".mov", ".mxf", ".avi", ".m4v", ".mkv", ".webm", ".ts", ".mts")
    is_video = (
        (asset.item_type or "").lower() == "video" or
        any((asset.title or "").lower().endswith(ext) for ext in _VIDEO_EXTS)
    )

    video_tech = ""
    if is_video:
        parts = []
        if exif_data.get("duration"):    parts.append(f"ความยาว: {exif_data['duration']}")
        if exif_data.get("fps"):         parts.append(f"fps: {exif_data['fps']}")
        if exif_data.get("codec"):       parts.append(f"codec: {exif_data['codec']}")
        if exif_data.get("resolution"):  parts.append(f"resolution: {exif_data['resolution']}")
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

    # People Directory face-ID pre-pass (highest confidence)
    try:
        from app.controllers.people_controller import get_relevant_people, identify_persons_with_directory
        from app.database import SessionLocal as _SL
        import os as _os
        _api_key_for_people = _os.environ.get("GEMINI_API_KEY") or settings.GEMINI_API_KEY
        _db = _SL()
        try:
            _candidates = get_relevant_people(_db, event_name, context_text, limit=8)
        finally:
            _db.close()
        if _candidates and any(p.photo_data for p in _candidates):
            _confirmed = await identify_persons_with_directory(
                client, image_b64, mime_type, _candidates,
                _api_key_for_people, settings.GEMINI_MODEL,
            )
            if _confirmed:
                prompt += (
                    f"\n\n[People Directory — ยืนยันจากการเปรียบเทียบใบหน้า — เชื่อถือสูงสุด]: "
                    f"บุคคลต่อไปนี้ถูกยืนยันแล้วว่าอยู่ในภาพนี้: {_confirmed}\n"
                    f"ใส่ชื่อเหล่านี้ใน persons ได้เลย และใช้ใน title/description ด้วย"
                )
    except Exception as _e:
        logger.warning(f"People directory pre-pass error: {_e}")

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

    # 5. Gemini API call
    payload = {
        "contents": [{"parts": [
            {"inlineData": {"mimeType": mime_type, "data": image_b64}},
            {"text": prompt},
        ]}],
        "generationConfig": {"temperature": 0.2},
    }

    import os
    _api_key = os.environ.get("GEMINI_API_KEY") or settings.GEMINI_API_KEY
    for attempt in range(3):
        resp = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{settings.GEMINI_MODEL}:generateContent",
            params={"key": _api_key},
            json=payload,
            timeout=60,
        )
        if resp.status_code == 503 and attempt < 2:
            wait = 15 * (2 ** attempt)
            logger.warning(f"Gemini 503 overload — retry {attempt+1}/2 in {wait}s")
            await asyncio.sleep(wait)
            continue
        break
    if resp.status_code != 200:
        raise ValueError(f"Gemini error {resp.status_code}: {resp.text[:200]}")

    body    = resp.json()
    raw     = body["candidates"][0]["content"]["parts"][0]["text"]
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    result  = json.loads(cleaned)

    # Pass 2 (verify_persons) disabled for Gemini —
    # Gemini 2.5 Flash has its own face recognition; verify pass was stripping valid names.

    result["_exif"]          = exif_data
    result["_tokens_input"]  = body.get("usageMetadata", {}).get("promptTokenCount", 0)
    result["_tokens_output"] = body.get("usageMetadata", {}).get("candidatesTokenCount", 0)
    return result


def get_daily_usage() -> dict:
    db = SessionLocal()
    try:
        today_start = datetime.combine(date.today(), datetime.min.time())
        row = db.query(
            func.count(Asset.item_id).label("requests"),
            func.coalesce(func.sum(Asset.tokens_input + Asset.tokens_output), 0).label("tokens"),
        ).filter(
            Asset.processed_at >= today_start,
            Asset.status == "done",
        ).first()
        return {"requests": row.requests or 0, "tokens": int(row.tokens or 0)}
    finally:
        db.close()


def check_rate_limit() -> Optional[str]:
    usage    = get_daily_usage()
    warn_rpd = int(settings.FREE_TIER_RPD * settings.FREE_TIER_WARN_PCT)
    warn_tpd = int(settings.FREE_TIER_TPD * settings.FREE_TIER_WARN_PCT)
    if usage["requests"] >= warn_rpd:
        return (f"ใกล้ถึง limit รายวัน: {usage['requests']}/{settings.FREE_TIER_RPD} requests "
                f"({settings.FREE_TIER_WARN_PCT*100:.0f}%) — หยุดรอ reset เที่ยงคืน UTC")
    if usage["tokens"] >= warn_tpd:
        return (f"ใกล้ถึง token limit รายวัน: {usage['tokens']:,}/{settings.FREE_TIER_TPD:,} tokens "
                f"({settings.FREE_TIER_WARN_PCT*100:.0f}%) — หยุดรอ reset เที่ยงคืน UTC")
    return None


async def run_gemini_batch(album_keys: list = None, cancel_flag: dict = None) -> AsyncGenerator[dict, None]:
    """
    album_keys: list of event names to process. None / [] = process all pending.
    """
    db = SessionLocal()
    # Auto-recover assets stuck in "processing" from a previous crashed run
    stuck = db.query(Asset).filter(Asset.status == "processing").all()
    for a in stuck:
        a.status = "pending"
    if stuck:
        db.commit()
        logger.info(f"Auto-reset {len(stuck)} stuck 'processing' assets to pending")

    pending_assets = (db.query(Asset)
                      .filter(Asset.status == "pending")
                      .order_by(Asset.ingest_path)
                      .all())

    # Filter by album_keys if provided
    album_set = set(album_keys) if album_keys else None
    if album_set:
        pending_assets = [a for a in pending_assets
                          if (extract_event_from_path(a.ingest_path or "") or "__ungrouped__") in album_set]

    # Group by event so cross-asset context sharing works within each event
    event_order: dict[str, list[str]] = {}
    for a in pending_assets:
        ev = extract_event_from_path(a.ingest_path or "") or "__ungrouped__"
        event_order.setdefault(ev, []).append(a.item_id)

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

            limit_msg = check_rate_limit()
            if limit_msg:
                yield {"type": "rate_limit", "message": limit_msg,
                       "processed": processed, "errors": errors, "total": total}
                return

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
                if "429" in err_str:
                    logger.warning(f"429 RPM — รอ 90s แล้ว retry {item_id[:8]}")
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
                for remaining in range(90, 0, -10):
                    yield {"type": "progress", "processed": processed, "errors": errors,
                           "total": total, "current": f"⏳ Rate limit — รอ {remaining}s..."}
                    await asyncio.sleep(10)
            else:
                await asyncio.sleep(settings.GEMINI_DELAY_MS / 1000)

    yield {"type": "done", "processed": processed, "errors": errors, "total": total}
