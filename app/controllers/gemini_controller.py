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

ใช้ข้อมูล Context ข้างบนประกอบการวิเคราะห์ภาพ แล้ว return ONLY a valid JSON:

{{
  "title": "ชื่อกระชับในภาษาไทย รูปแบบ YYYY.MM.DD_หัวข้อ_ชื่อบุคคลหรือแบรนด์",
  "description": "อธิบาย 3-5 ประโยคในภาษาไทย ว่าใคร ทำอะไร ที่ไหน",
  "category": "Photo หรือ Footage หรือ Audio หรือ Graphic หรือ Deliverable",
  "subcat": "Portrait หรือ Event หรือ B-Roll หรือ Drone หรือ BTS หรือ Interview หรือ Press Conference หรือ Protest หรือ Document หรือ Product",
  "keyword": ["คำ1", "คำ2", "คำ3", "คำ4", "คำ5"]
}}

กฎ:
- ถ้า Path หรือชื่อไฟล์มีชื่อคน ให้ใช้ชื่อนั้น อย่าเดาจากหน้า
- keyword 5-10 คำ ครอบคลุมคน สถานที่ หัวข้อ และ action
- Return JSON only ห้าม return อย่างอื่น\
"""


def get_daily_usage() -> dict:
    """คืน requests และ tokens ที่ใช้ไปวันนี้จาก DB"""
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
        return {
            "requests": row.requests or 0,
            "tokens":   int(row.tokens or 0),
        }
    finally:
        db.close()


def check_rate_limit() -> Optional[str]:
    """
    ตรวจ daily usage เทียบกับ free tier limit
    คืน None = ยังใช้ได้, คืน string = เหตุผลที่ต้องหยุด
    """
    usage = get_daily_usage()
    warn_rpd = int(settings.FREE_TIER_RPD * settings.FREE_TIER_WARN_PCT)
    warn_tpd = int(settings.FREE_TIER_TPD * settings.FREE_TIER_WARN_PCT)

    if usage["requests"] >= warn_rpd:
        return (f"ใกล้ถึง limit รายวัน: {usage['requests']}/{settings.FREE_TIER_RPD} requests "
                f"({settings.FREE_TIER_WARN_PCT*100:.0f}%) — หยุดรอ reset เที่ยงคืน UTC")
    if usage["tokens"] >= warn_tpd:
        return (f"ใกล้ถึง token limit รายวัน: {usage['tokens']:,}/{settings.FREE_TIER_TPD:,} tokens "
                f"({settings.FREE_TIER_WARN_PCT*100:.0f}%) — หยุดรอ reset เที่ยงคืน UTC")
    return None


async def _analyze_one(client: httpx.AsyncClient, asset: Asset) -> dict:
    """Call Gemini Vision for a single asset. Returns parsed JSON dict + token usage."""
    img_resp = await client.get(asset.thumbnail_url, timeout=30)
    if img_resp.status_code != 200:
        raise ValueError(f"Cannot fetch thumbnail ({img_resp.status_code})")

    image_b64 = base64.b64encode(img_resp.content).decode()
    prompt = PROMPT.format(
        title=asset.title or "",
        ingest_path=asset.ingest_path or "",
        item_type=asset.item_type or "image",
    )

    payload = {
        "contents": [{"parts": [
            {"inlineData": {"mimeType": "image/jpeg", "data": image_b64}},
            {"text": prompt},
        ]}],
        "generationConfig": {"temperature": 0.2},
    }

    resp = await client.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{settings.GEMINI_MODEL}:generateContent",
        params={"key": settings.GEMINI_API_KEY},
        json=payload,
        timeout=60,
    )
    if resp.status_code != 200:
        raise ValueError(f"Gemini error {resp.status_code}: {resp.text[:200]}")

    body = resp.json()
    raw = body["candidates"][0]["content"]["parts"][0]["text"]
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    result = json.loads(cleaned)

    # แนบ token usage กลับมาด้วย
    usage = body.get("usageMetadata", {})
    result["_tokens_input"]  = usage.get("promptTokenCount", 0)
    result["_tokens_output"] = usage.get("candidatesTokenCount", 0)
    return result


async def run_gemini_batch() -> AsyncGenerator[dict, None]:
    """
    Process all 'pending' assets with Gemini Vision.
    Yields progress dicts for SSE streaming.
    """
    db = SessionLocal()
    pending_ids = [a.item_id for a in db.query(Asset).filter(Asset.status == "pending").all()]
    db.close()

    total = len(pending_ids)
    processed = 0
    errors = 0

    async with httpx.AsyncClient() as client:
        for item_id in pending_ids:

            # ── ตรวจ rate limit ก่อนทุก request ──────────────────────────
            limit_msg = check_rate_limit()
            if limit_msg:
                yield {"type": "rate_limit", "message": limit_msg,
                       "processed": processed, "errors": errors, "total": total}
                return

            db = SessionLocal()
            try:
                asset = db.query(Asset).filter(Asset.item_id == item_id).first()
                if not asset or asset.status != "pending":
                    continue

                asset.status = "processing"
                db.commit()

                yield {"type": "progress", "processed": processed, "errors": errors, "total": total,
                       "current": asset.title or item_id}

                result = await _analyze_one(client, asset)

                keywords = result.get("keyword", [])
                asset.ai_title       = result.get("title", "")
                asset.ai_description = result.get("description", "")
                asset.ai_category    = result.get("category", "")
                asset.ai_subcat      = result.get("subcat", "")
                asset.ai_keyword     = ", ".join(keywords) if isinstance(keywords, list) else str(keywords)
                asset.tokens_input   = result.get("_tokens_input", 0)
                asset.tokens_output  = result.get("_tokens_output", 0)
                asset.processed_at   = datetime.utcnow()
                asset.status         = "done"
                asset.error_log      = ""
                db.commit()

                processed += 1
                logger.info(f"[{processed}/{total}] done: {asset.ai_title}")

            except Exception as exc:
                errors += 1
                logger.error(f"Error on {item_id}: {exc}")
                try:
                    asset.status = "error"
                    asset.error_log = str(exc)
                    db.commit()
                except Exception:
                    pass
            finally:
                db.close()

            yield {"type": "progress", "processed": processed, "errors": errors, "total": total}
            await asyncio.sleep(settings.GEMINI_DELAY_MS / 1000)

    yield {"type": "done", "processed": processed, "errors": errors, "total": total}
