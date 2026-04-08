import asyncio
import logging
from typing import AsyncGenerator

import httpx
from app.config import settings
from app.database import SessionLocal
from app.models.asset import Asset

logger = logging.getLogger(__name__)


async def fetch_all_items() -> AsyncGenerator[dict, None]:
    """
    Fetch all items from Mimir folder and upsert into DB.
    Yields progress dicts for SSE streaming.
    """
    from_offset = 0
    total_fetched = 0
    total_in_api = 0

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            params = {
                "searchString": "*",
                "folderId": settings.FOLDER_ID,
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
    Push AI-generated metadata back to Mimir for a single asset.
    Returns {"ok": True} or {"ok": False, "error": "..."}
    """
    db = SessionLocal()
    try:
        asset = db.query(Asset).filter(Asset.item_id == item_id).first()
        if not asset:
            return {"ok": False, "error": "Asset not found"}
        if asset.status != "done":
            return {"ok": False, "error": "Asset not yet processed by AI"}

        payload = {
            "metadata": {
                "formData": {
                    "title": asset.ai_title,
                    "description": asset.ai_description,
                    "category": asset.ai_category,
                    "subCategory": asset.ai_subcat,
                    "keywords": asset.ai_keyword,
                    "rights": asset.rights,
                }
            }
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.patch(
                f"{settings.MIMIR_BASE_URL}/api/v1/assets/{item_id}",
                json=payload,
                headers={
                    "x-mimir-cognito-id-token": f"Bearer {settings.MIMIR_TOKEN}",
                    "Content-Type": "application/json",
                },
            )

        if resp.status_code in (200, 204):
            return {"ok": True}
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

    finally:
        db.close()
