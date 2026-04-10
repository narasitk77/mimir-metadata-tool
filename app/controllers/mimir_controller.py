import asyncio
import logging
from typing import AsyncGenerator, Optional

import httpx
from app.config import settings
from app.database import SessionLocal
from app.models.asset import Asset

logger = logging.getLogger(__name__)


def extract_folder_id(folder_url: str) -> str:
    """
    รับ URL หรือ folder ID ดิบ แล้วคืน folder ID (UUID)
    รองรับ:
      - UUID ตรงๆ  : 1bff1e1d-4542-47a4-b083-a98adbf1b230
      - URL แบบ    : https://apac.mjoll.no/folder/1bff1e1d-...
                     https://apac.mjoll.no/folders/1bff1e1d-...
    """
    import re
    uuid_pattern = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
    match = re.search(uuid_pattern, folder_url, re.IGNORECASE)
    if match:
        return match.group(0)
    raise ValueError(f"ไม่พบ Folder ID ใน: {folder_url}")


async def fetch_all_items(folder_id: Optional[str] = None) -> AsyncGenerator[dict, None]:
    """
    Fetch all items from Mimir folder and upsert into DB.
    Yields progress dicts for SSE streaming.
    folder_id: UUID โดยตรง หรือ None เพื่อใช้ค่าจาก config
    """
    resolved_folder_id = folder_id or settings.FOLDER_ID
    if not resolved_folder_id:
        yield {"type": "error", "message": "ยังไม่ได้ระบุ Folder ID"}
        return

    from_offset = 0
    total_fetched = 0
    total_in_api = 0

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            params = {
                "searchString": "*",
                "folderId": resolved_folder_id,
                "itemsPerPage": settings.ITEMS_PER_PAGE,
                "from": from_offset,
                "includeSubfolders": "true",
                "includeFolders": "false",
                "readableMetadataFields": "true",
            }
            resp = await client.get(
                f"{settings.MIMIR_BASE_URL}/api/v1/search",
                params=params,
                headers={"x-mimir-cognito-id-token": f"Bearer {settings.MIMIR_TOKEN}"},
            )

            if resp.status_code != 200:
                yield {"type": "error", "message": f"Mimir API error {resp.status_code}: {resp.text[:300]}"}
                return

            data = resp.json()
            total_in_api = data.get("total", 0)
            items = data.get("_embedded", {}).get("collection", [])

            if not items:
                break

            db = SessionLocal()
            try:
                for item in items:
                    fd = item.get("metadata", {}).get("formData", {})
                    tfd = item.get("technicalMetadata", {}).get("formData", {})

                    existing = db.query(Asset).filter(Asset.item_id == item["id"]).first()
                    if not existing:
                        db.add(Asset(
                            item_id=item.get("id", ""),
                            thumbnail_url=item.get("thumbnail", ""),
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
                        ))
                db.commit()
            finally:
                db.close()

            total_fetched += len(items)
            from_offset += settings.ITEMS_PER_PAGE

            yield {"type": "progress", "fetched": total_fetched, "total": total_in_api}
            logger.info(f"Fetched {total_fetched} / {total_in_api}")

            if total_fetched >= total_in_api:
                break

            await asyncio.sleep(0.5)

    yield {"type": "done", "fetched": total_fetched, "total": total_in_api}


async def push_metadata_to_mimir(item_id: str) -> dict:
    """
    Push AI+EXIF metadata back to Mimir using POST /api/v1/items/{id}.
    Returns {"ok": True} or {"ok": False, "error": "..."}
    """
    db = SessionLocal()
    try:
        asset = db.query(Asset).filter(Asset.item_id == item_id).first()
        if not asset:
            return {"ok": False, "error": "Asset not found"}
        if asset.status != "done":
            return {"ok": False, "error": "Asset not yet processed by AI"}

        # ดึง createdOn จาก Mimir ก่อน (required field)
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{settings.MIMIR_BASE_URL}/api/v1/items/{item_id}",
                headers={"x-mimir-cognito-id-token": f"Bearer {settings.MIMIR_TOKEN}"},
            )
            if r.status_code != 200:
                return {"ok": False, "error": f"Cannot fetch item: HTTP {r.status_code}"}
            existing = r.json()
            fd = existing.get("metadata", {}).get("formData", {})
            created_on = fd.get("default_createdOn") or asset.media_created_on or ""
            media_created_on = fd.get("default_mediaCreatedOn") or asset.media_created_on or ""

            payload = {
                "metadata": {
                    "formId": "default",
                    "formData": {
                        # Required
                        "default_createdOn":            created_on,
                        "default_mediaCreatedOn":       media_created_on,
                        # Core AI
                        "default_title":                asset.ai_title or asset.title,
                        "default_description":          asset.ai_description,
                        "default_category":             asset.ai_category,
                        "default_subCategory":          asset.ai_subcat,
                        "default_keywords":             asset.ai_keyword,
                        "default_rights":               asset.rights,
                        # Extended AI
                        "default_editorialCategories":  asset.ai_editorial_categories,
                        "default_location":             asset.ai_location,
                        "default_persons":              asset.ai_persons,
                        "default_episodeSegment":       asset.ai_episode_segment,
                        "default_eventOccasion":        asset.ai_event_occasion,
                        "default_emotionMood":          asset.ai_emotion_mood,
                        "default_language":             asset.ai_language,
                        "default_department":           asset.ai_department,
                        "default_projectSeries":        asset.ai_project_series,
                        "default_rightLicense":         asset.ai_right_license,
                        "default_deliverableType":      asset.ai_deliverable_type,
                        "default_subjectTags":          asset.ai_subject_tags,
                        "default_technicalTags":        asset.ai_technical_tags,
                        "default_visualAttributes":     asset.ai_visual_attributes,
                        # EXIF
                        "default_photographer":         asset.exif_photographer,
                        "default_cameraModel":          asset.exif_camera_model,
                        "default_creditLine":           asset.exif_credit_line,
                    }
                }
            }
            # ลบ field ที่ว่างออกเพื่อไม่ overwrite ด้วยค่าว่าง
            payload["metadata"]["formData"] = {
                k: v for k, v in payload["metadata"]["formData"].items() if v
            }

            resp = await client.post(
                f"{settings.MIMIR_BASE_URL}/api/v1/items/{item_id}",
                json=payload,
                headers={
                    "x-mimir-cognito-id-token": f"Bearer {settings.MIMIR_TOKEN}",
                    "Content-Type": "application/json",
                },
            )

        if resp.status_code == 200:
            return {"ok": True}
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

    finally:
        db.close()
