import asyncio
import base64
import json
import logging
from datetime import datetime, date
from typing import AsyncGenerator, Optional

import httpx
from sqlalchemy import func
from app.config import settings
from app.database import SessionLocal
from app.models.asset import Asset

logger = logging.getLogger(__name__)

PROMPT = """\
You are a media metadata specialist for THE STANDARD, a Thai news and media company.

ข้อมูล Context ของไฟล์นี้:
- ชื่อไฟล์เดิม: "{title}"
- Path ในระบบ: "{ingest_path}"
- ประเภทไฟล์: "{item_type}"
- ช่างภาพ (จาก EXIF): "{exif_photographer}"
- กล้อง (จาก EXIF): "{exif_camera_model}"

วิเคราะห์ภาพประกอบกับ Context แล้ว return ONLY a valid JSON object:

{{
  "title": "ชื่อกระชับภาษาไทย รูปแบบ YYYY.MM.DD_หัวข้อ_ชื่อบุคคลหรือแบรนด์",
  "description": "อธิบาย 3-5 ประโยคในภาษาไทย ว่าใคร ทำอะไร ที่ไหน เมื่อไหร่",
  "category": "Photo หรือ Footage หรือ Audio หรือ Graphic หรือ Deliverable",
  "subcat": "Portrait หรือ Event หรือ B-Roll หรือ Drone หรือ BTS หรือ Interview หรือ Press Conference หรือ Protest หรือ Document หรือ Product",
  "editorial_categories": "Politics หรือ Business หรือ Entertainment หรือ Lifestyle หรือ Sport หรือ Tech หรือ World หรือ Environment หรือ Health",
  "location": "สถานที่ถ่าย เช่น สวนลุมพินี กรุงเทพมหานคร หรือ The Standard ออฟฟิศ",
  "persons": "ชื่อบุคคลในภาพคั่นด้วย comma (ถ้าไม่ทราบชื่อให้ใส่ตำแหน่ง เช่น นักการเมือง, นักธุรกิจ)",
  "event_occasion": "ชื่องานหรือโอกาสที่ถ่าย เช่น งานแถลงข่าว, พิธีมอบรางวัล",
  "emotion_mood": "Happy หรือ Serious หรือ Tense หรือ Celebratory หรือ Neutral หรือ Sad",
  "language": "Thai หรือ English หรือ Other",
  "subject_tags": "แท็กหัวข้อคั่นด้วย comma เช่น การเมือง, เศรษฐกิจ, สิ่งแวดล้อม",
  "visual_attributes": "ลักษณะภาพคั่นด้วย comma เช่น Wide shot, Close-up, Candid, Studio, Outdoor",
  "keywords": ["คำ1", "คำ2", "คำ3", "คำ4", "คำ5"]
}}

กฎ:
- ถ้า Path หรือชื่อไฟล์มีชื่อคน ให้ใช้ชื่อนั้น อย่าเดาจากหน้า
- keywords 5-10 คำ ครอบคลุมคน สถานที่ หัวข้อ และ action
- Return JSON only ห้าม return อย่างอื่นเด็ดขาด\
"""


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
    ifd0  = exif.get("EXIF:IFD0", {})
    exif_ = exif.get("EXIF:ExifIFD", {})
    return {
        "photographer":  ifd0.get("Artist", ""),
        "camera_model":  f"{ifd0.get('Make', '')} {ifd0.get('Model', '')}".strip(),
        "credit_line":   ifd0.get("Copyright", ""),
        "iso":           str(exif_.get("ISO", "")),
        "aperture":      f"f/{exif_.get('FNumber', '')}" if exif_.get("FNumber") else "",
        "shutter":       str(exif_.get("ExposureTime", "")),
        "focal_length":  str(exif_.get("FocalLength", "")),
    }


async def _analyze_one(client: httpx.AsyncClient, asset: Asset) -> dict:
    import anthropic as _anthropic
    import os

    # 1. EXIF
    exif_raw  = await _fetch_exif(client, asset)
    exif_data = _parse_exif(exif_raw)

    # 2. Thumbnail (refresh URL from Mimir if expired)
    img_resp = await client.get(asset.thumbnail_url, timeout=30)
    if img_resp.status_code in (400, 403, 404):
        r = await client.get(
            f"{settings.MIMIR_BASE_URL}/api/v1/items/{asset.item_id}",
            headers={"x-mimir-cognito-id-token": f"Bearer {settings.MIMIR_TOKEN}"},
            timeout=15,
        )
        if r.status_code == 200:
            new_thumb = r.json().get("thumbnail", "")
            if new_thumb:
                db = SessionLocal()
                try:
                    a = db.query(Asset).filter(Asset.item_id == asset.item_id).first()
                    if a:
                        a.thumbnail_url = new_thumb
                        db.commit()
                        asset.thumbnail_url = new_thumb
                finally:
                    db.close()
                img_resp = await client.get(new_thumb, timeout=30)
    if img_resp.status_code != 200:
        raise ValueError(f"Cannot fetch thumbnail ({img_resp.status_code})")
    image_b64 = base64.b64encode(img_resp.content).decode()

    # 3. Prompt
    prompt = PROMPT.format(
        title=asset.title or "",
        ingest_path=asset.ingest_path or "",
        item_type=asset.item_type or "image",
        exif_photographer=exif_data.get("photographer", ""),
        exif_camera_model=exif_data.get("camera_model", ""),
    )

    # 4. Claude API call (sync in thread to keep async-friendly)
    api_key = os.environ.get("ANTHROPIC_API_KEY") or settings.ANTHROPIC_API_KEY
    claude = _anthropic.Anthropic(api_key=api_key)

    def _call_claude():
        return claude.messages.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
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

    result["_exif"]          = exif_data
    result["_tokens_input"]  = resp.usage.input_tokens
    result["_tokens_output"] = resp.usage.output_tokens
    return result


async def run_claude_batch() -> AsyncGenerator[dict, None]:
    db = SessionLocal()
    pending_ids = [a.item_id for a in db.query(Asset).filter(Asset.status == "pending").all()]
    db.close()

    total = len(pending_ids)
    processed = 0
    errors = 0
    idx = 0

    async with httpx.AsyncClient() as client:
        while idx < len(pending_ids):
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

                yield {"type": "progress", "processed": processed, "errors": errors,
                       "total": total, "current": asset.title or item_id}

                result = await _analyze_one(client, asset)
                exif   = result.get("_exif", {})
                kw     = result.get("keywords", [])

                asset.ai_title               = result.get("title", "")
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
                asset.ai_subject_tags        = result.get("subject_tags", "")
                asset.ai_visual_attributes   = result.get("visual_attributes", "")
                asset.exif_photographer      = exif.get("photographer", "")
                asset.exif_camera_model      = exif.get("camera_model", "")
                asset.exif_credit_line       = exif.get("credit_line", "")
                asset.exif_iso               = exif.get("iso", "")
                asset.exif_aperture          = exif.get("aperture", "")
                asset.exif_shutter           = exif.get("shutter", "")
                asset.exif_focal_length      = exif.get("focal_length", "")
                asset.tokens_input           = result.get("_tokens_input", 0)
                asset.tokens_output          = result.get("_tokens_output", 0)
                asset.processed_at           = datetime.utcnow()
                asset.status                 = "done"
                asset.error_log              = ""
                db.commit()

                processed += 1
                idx += 1
                logger.info(f"[{processed}/{total}] done: {asset.ai_title}")

            except Exception as exc:
                err_str = str(exc)
                # Overloaded → รอ 60s retry
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
                await asyncio.sleep(1)  # Claude ไม่มี RPM ต่ำ delay น้อย

    yield {"type": "done", "processed": processed, "errors": errors, "total": total}
