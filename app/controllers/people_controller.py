import json
import logging
from typing import List

import httpx

logger = logging.getLogger(__name__)

_FACE_ID_PROMPT = """\
ต่อไปนี้คือรูป reference ของบุคคลที่อาจปรากฏในงานนี้ (แต่ละคนมีชื่อกำกับไว้)

{person_labels}

--- ภาพสุดท้ายคือภาพที่ต้องวิเคราะห์ ---

เปรียบเทียบใบหน้าในภาพสุดท้ายกับ reference แต่ละคนอย่างละเอียด
ใส่ใน "confirmed" เฉพาะคนที่ใบหน้าตรงกันชัดเจน (HIGH confidence เท่านั้น)
ถ้าไม่แน่ใจหรือใบหน้าไม่ชัด → ไม่ต้องใส่ ดีกว่าเดาผิด
ถ้าภาพไม่มีคน หรือไม่เห็นใบหน้าเลย → ส่งกลับ confirmed: []

Return ONLY valid JSON: {{"confirmed": ["ชื่อ1", "ชื่อ2"]}}
"""


def get_relevant_people(db, event_name: str, context_text: str = "", limit: int = 8):
    """Return up to `limit` Person objects most relevant to event_name + context_text."""
    from app.models.person import Person

    all_people = db.query(Person).all()
    if not all_people:
        return []

    search = f"{event_name} {context_text}".lower()

    scored = []
    for person in all_people:
        score = 0
        if person.name.lower() in search:
            score += 10
        for kw in (person.keywords or "").split(","):
            kw = kw.strip().lower()
            if kw and kw in search:
                score += 3
        scored.append((score, person))

    scored.sort(key=lambda x: x[0], reverse=True)

    # Always return something — if no keyword match, return most recently added
    return [p for _, p in scored[:limit]]


async def identify_persons_with_directory(
    client: httpx.AsyncClient,
    image_b64: str,
    mime_type: str,
    candidates: list,
    api_key: str,
    model: str,
) -> str:
    """
    Run Gemini face comparison pass against reference photos.
    Returns comma-separated confirmed names, or "" if none found.
    candidates: list of Person ORM objects
    """
    people_with_photos = [p for p in candidates if p.photo_data]
    if not people_with_photos:
        return ""

    parts = []
    labels = []
    for i, person in enumerate(people_with_photos[:8]):
        label = f"[คน {i+1}] {person.name}"
        if person.title:
            label += f" — {person.title}"
        labels.append(label)
        parts.append({"text": label})
        parts.append({"inlineData": {"mimeType": person.photo_mime or "image/jpeg", "data": person.photo_data}})

    parts.append({"text": "--- ภาพที่ต้องวิเคราะห์ ---"})
    parts.append({"inlineData": {"mimeType": mime_type, "data": image_b64}})
    parts.append({"text": _FACE_ID_PROMPT.format(person_labels="\n".join(labels))})

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0.05},
    }

    try:
        resp = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": api_key},
            json=payload,
            timeout=45,
        )
        if resp.status_code != 200:
            logger.warning(f"People directory face-ID {resp.status_code} — skipping")
            return ""

        raw     = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        cleaned = raw.replace("```json", "").replace("```", "").strip()
        result  = json.loads(cleaned)
        confirmed = result.get("confirmed", [])
        if confirmed:
            names = ", ".join(str(n) for n in confirmed if n)
            logger.info(f"People directory confirmed: {names}")
            return names
        return ""
    except Exception as e:
        logger.warning(f"People directory face-ID failed: {e}")
        return ""
