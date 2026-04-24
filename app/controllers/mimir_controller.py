import asyncio
import logging
import re as _re
from typing import AsyncGenerator, List, Optional

import httpx
from app.config import settings
from app.database import SessionLocal
from app.models.asset import Asset
from app.services.cognito_auth import get_token as _get_cognito_token


async def _auth_header() -> dict:
    """Return Mimir auth header, using static token or Cognito SRP."""
    if settings.MIMIR_TOKEN:
        token = settings.MIMIR_TOKEN
    else:
        token = await _get_cognito_token()
    return {"x-mimir-cognito-id-token": f"Bearer {token}"}

logger = logging.getLogger(__name__)

_HIRES_NAMES = {"hires", "hi-res", "hi_res", "highres", "high res"}

# ── Mimir UUID field map ────────────────────────────────────────────────────────
# Mimir's dropdown fields use UUID keys (not default_* keys).
# Map: db_field → (uuid_key, value_transformer)
# Discover UUIDs by: manually editing an item in Mimir UI, then calling
#   GET /api/debug/mimir/{item_id}  and reading the UUID-keyed fields.
#
# Value transformer: convert our AI-generated string to Mimir's option ID.
# Most Mimir option IDs are lowercase slugs (spaces → underscore or hyphen).

def _slug(v: str) -> str:
    """Convert display value to Mimir option slug: lowercase, spaces → underscore."""
    return _re.sub(r'\s+', '_', v.strip().lower())


def _split_list(v: str) -> list:
    """Split comma-separated string into a trimmed list (for Mimir multi-value text fields)."""
    return [x.strip() for x in str(v).split(",") if x.strip()]


def _split_lower_list(v: str) -> list:
    """Split comma-separated string into a lowercase list (for controlled-vocab multi-select fields)."""
    return [x.strip().lower() for x in str(v).split(",") if x.strip()]


def _photographer_slugs(v: str) -> list:
    """Convert 'First Last' → ['first_last'] (Mimir photographer field stores slug in an array)."""
    s = _slug(v)
    return [s] if s else []


def _dept_id(v: str) -> str:
    """Map AI-generated department name to Mimir's valid dept option ID.
    Known valid: 'news', 'tsd'. Unknown values raise ValueError → field is skipped.
    """
    _MAP = {
        "news": "news",
        "editorial": "news",   # The Standard editorial = News dept
        "tsd": "tsd",
        "the standard": "tsd",
        "standard": "tsd",
    }
    key = v.strip().lower()
    result = _MAP.get(key)
    if result is None:
        raise ValueError(f"Unknown department value: {v!r}")
    return result


# ── Mimir UUID → field mapping ────────────────────────────────────────────────
# Discovered by: manually editing an item in Mimir UI then calling
#   GET /api/debug/mimir/{item_id}  →  uuid_in_mimir section
#
# Format: db_field → (uuid_key, value_transformer_fn)
# NOTE: Editorial Categories option IDs may use abbreviated slugs (e.g. "hum_inter")
#       that don't match plain lowercase. The AI prompt should be updated to use
#       the exact option IDs once all valid values are known.

_MIMIR_UUID_FIELDS: dict[str, tuple] = {
    # db_field                  (uuid_key,                               value_transformer)
    "ai_category":              ("a2c6f3f0-5ecb-44c1-a255-25f3e50bdeda", str.lower),           # "photo"
    # ai_subcat — Sub-category UUID not yet identified; still pushed via default_subCategory
    "ai_editorial_categories":  ("2f5f0fb9-b4a7-44a1-92b7-a12daaaf625e", _split_lower_list),   # ["politics","hum_inter"]
    "ai_language":              ("2c09393f-1c1b-43e4-9778-8d14bc6132b9", str.lower),            # "thai","english"
    "ai_emotion_mood":          ("a6711363-9183-4e41-a7e9-cae0ef7889c8", _split_lower_list),    # ["neutral"],["happy"]
    "ai_location":              ("0d0222be-8d47-45c0-add8-f3de9ca9f682", str),                  # free text
    "ai_event_occasion":        ("847fce2b-454c-4599-b947-6dd2a7fbae7d", str),                  # free text
    "ai_persons":               ("4597e1f4-e586-4e92-b058-5b01a4dc462e", str),                  # free text
    "ai_keyword":               ("d59bc0ce-0195-4648-a6f9-223bfc15e5fb", _split_list),          # ["kw1","kw2"]
    "exif_photographer":        ("6a1c55aa-1367-42a8-8753-9482f86163ed", _photographer_slugs),  # ["thanis_sudto"]
    "ai_department":            ("766b92be-47e7-49f4-bbe4-2917c4702a8b", _dept_id),             # "news","tsd" (Mimir field id: "dept")
    # 37cf2de2: "TSD" — unknown field, excluded until confirmed
    "ai_subject_tags":          ("5dccd413-eac9-4032-8bae-f76c8a24d2d3", _split_list),          # ["pol","การเมือง",…]
    "ai_technical_tags":        ("f35ee943-2fc3-4a20-b949-a49eb5f55059", _split_list),          # ["Outdoor","Group"]
    "ai_visual_attributes":     ("65b8ebd0-7f97-493f-865e-c50224c14748", _split_list),          # ["Group","Wideshot"]
}


def _folder_name(item: dict) -> str:
    """Extract folder display name from a Mimir search result item."""
    return (
        item.get("originalFileName")
        or item.get("title")
        or item.get("metadata", {}).get("formData", {}).get("title", "")
        or ""
    )


async def _list_subfolders(client: httpx.AsyncClient, folder_id: str) -> List[dict]:
    """
    Return [{id, name}] for direct subfolder children of folder_id.
    Uses Mimir search with includeFolders=true, includeSubfolders=false.
    """
    params = {
        "searchString": "*",
        "folderId": folder_id,
        "itemsPerPage": 200,
        "from": 0,
        "includeSubfolders": "false",
        "includeFolders": "true",
        "readableMetadataFields": "false",
    }
    try:
        resp = await client.get(
            f"{settings.MIMIR_BASE_URL}/api/v1/search",
            params=params,
            headers=await _auth_header(),
        )
        if resp.status_code != 200:
            logger.warning(f"_list_subfolders HTTP {resp.status_code} for {folder_id}")
            return []
        data = resp.json()
        items = data.get("_embedded", {}).get("collection", [])
        return [
            {"id": it["id"], "name": _folder_name(it)}
            for it in items
            if it.get("itemType", "").lower() in ("folder", "folders", "archive")
        ]
    except Exception as exc:
        logger.warning(f"_list_subfolders error: {exc}")
        return []


async def discover_hires_folders(folder_id: str) -> List[dict]:
    """
    Given a folder ID (e.g. a whole month), discover all 'Hires' subfolders.
    Searches 2 levels deep:
      Level 1 — direct children (e.g. event/day folders, or Hires itself)
      Level 2 — children of level-1 folders (Hires, RAW, Proxies…)
    Returns list of [{id, name}] for Hires folders.
    Returns [] if none found → caller should use the original folder_id.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        level1 = await _list_subfolders(client, folder_id)
        if not level1:
            return []

        # Check if any level-1 subfolder is already a Hires folder
        hires = [f for f in level1 if f["name"].lower() in _HIRES_NAMES]
        if hires:
            return hires

        # Recurse into level-1 folders to find Hires at level 2
        tasks = [_list_subfolders(client, f["id"]) for f in level1]
        results = await asyncio.gather(*tasks)
        for children in results:
            hires.extend(f for f in children if f["name"].lower() in _HIRES_NAMES)

        return hires


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


async def fetch_all_items(folder_id: Optional[str] = None, context_text: str = "") -> AsyncGenerator[dict, None]:
    """
    Fetch all items from Mimir folder and upsert into DB.
    Yields progress dicts for SSE streaming.
    folder_id: UUID โดยตรง หรือ None เพื่อใช้ค่าจาก config
    context_text: optional folder-level context for AI analysis
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
                headers=await _auth_header(),
            )

            if resp.status_code != 200:
                yield {"type": "error", "message": f"Mimir API error {resp.status_code}: {resp.text[:300]}"}
                return

            data = resp.json()
            total_in_api = data.get("total", 0)
            items = data.get("_embedded", {}).get("collection", [])

            if not items:
                break

            # ── Patterns to skip (camera card metadata / thumbnail folders) ──
            # Sony: THMBNL=thumbnails, Sub=sub-clips, XMETA=XML metadata
            # Generic: Proxies, .LUT, .XML sidecar files, etc.
            _SKIP_PATH_SEGS = (
                "/thmbnl/", "/sub/", "/xmeta/", "/proxies/",
                "/general/", "/.mxf.xmp",
            )
            _SKIP_EXTS = (".xml", ".bup", ".inf", ".smi", ".xmp",
                          ".idx", ".cif", ".sif", ".lut")

            db = SessionLocal()
            try:
                skipped_meta = 0
                for item in items:
                    ingest_path_lower = (item.get("ingestSourceFullPath", "") or "").lower()
                    fname_lower = (item.get("originalFileName", "") or "").lower()

                    # Skip camera metadata / thumbnail folders and sidecar files
                    if any(seg in ingest_path_lower for seg in _SKIP_PATH_SEGS):
                        skipped_meta += 1
                        continue
                    if any(fname_lower.endswith(ext) for ext in _SKIP_EXTS):
                        skipped_meta += 1
                        continue

                    fd = item.get("metadata", {}).get("formData", {})
                    tfd = item.get("technicalMetadata", {}).get("formData", {})

                    proxy_url = item.get("proxy", "")
                    existing = db.query(Asset).filter(Asset.item_id == item["id"]).first()
                    if not existing:
                        db.add(Asset(
                            item_id=item.get("id", ""),
                            folder_id=resolved_folder_id,
                            thumbnail_url=item.get("thumbnail", ""),
                            proxy_url=proxy_url,
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
                            context_text=context_text,
                        ))
                    elif proxy_url and not existing.proxy_url:
                        # Backfill proxy_url for assets fetched before this feature existed
                        existing.proxy_url = proxy_url
                db.commit()
                if skipped_meta:
                    logger.info(f"Skipped {skipped_meta} metadata/thumbnail files")
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
                headers=await _auth_header(),
            )
            if r.status_code != 200:
                return {"ok": False, "error": f"Cannot fetch item: HTTP {r.status_code}"}
            existing = r.json()
            fd = existing.get("metadata", {}).get("formData", {})
            # Mimir stores keys with "default_" prefix after first push,
            # but plain keys before first push — handle both
            created_on = (fd.get("default_createdOn") or fd.get("createdOn")
                          or asset.media_created_on or "1970-01-01T00:00:00.000Z")
            media_created_on = (fd.get("default_mediaCreatedOn") or fd.get("mediaCreatedOn")
                                or asset.media_created_on or "1970-01-01T00:00:00.000Z")

            # Required fields — must always be present (Mimir returns 400 if missing)
            required = {
                "default_createdOn":      created_on,
                "default_mediaCreatedOn": media_created_on,
                "default_title":          asset.ai_title or asset.title or "(no title)",
                "default_description":    asset.ai_description or asset.ai_title or asset.title or "(no description)",
            }

            # Optional fields — only include if non-empty
            optional = {
                "default_category":             asset.ai_category,
                "default_subCategory":          asset.ai_subcat,
                "default_keywords":             asset.ai_keyword,
                "default_rights":               asset.rights,
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
                "default_photographer":         asset.exif_photographer,
                "default_cameraModel":          asset.exif_camera_model,
                "default_creditLine":           asset.exif_credit_line,
            }

            # Build UUID-keyed fields for Mimir's native dropdown rendering
            uuid_fields: dict = {}
            for db_field, (uuid_key, transform) in _MIMIR_UUID_FIELDS.items():
                raw_val = getattr(asset, db_field, None)
                if raw_val:
                    try:
                        uuid_fields[uuid_key] = transform(str(raw_val))
                    except Exception:
                        pass

            # ── Smart retry loop ───────────────────────────────────────────────
            # Mimir returns 400 when a UUID field value isn't a valid option.
            # Parse the error to find which value was rejected, drop that UUID
            # field, and retry — until all remaining UUID fields are accepted.
            # Pattern: {"error":{"message":"Trying to set invalid value X for field: \"Y\""}}
            _invalid_pat = _re.compile(r'invalid value (.+?) for field', _re.IGNORECASE)
            headers = {
                **(await _auth_header()),
                "Content-Type": "application/json",
            }
            current_uuid = dict(uuid_fields)
            skipped_uuid: list[str] = []
            resp = None

            for _attempt in range(len(uuid_fields) + 1):   # max n+1 attempts
                formdata = {
                    **current_uuid,
                    **required,
                    **{k: v for k, v in optional.items() if v},
                }
                resp = await client.post(
                    f"{settings.MIMIR_BASE_URL}/api/v1/items/{item_id}",
                    json={"metadata": {"formId": "default", "formData": formdata}},
                    headers=headers,
                )
                if resp.status_code == 200:
                    break
                if resp.status_code == 400 and current_uuid:
                    m = _invalid_pat.search(resp.text)
                    removed = False
                    if m:
                        bad_val = m.group(1)
                        for k in list(current_uuid.keys()):
                            v = current_uuid[k]
                            v_str = v if isinstance(v, str) else (v[0] if isinstance(v, list) and v else str(v))
                            if v_str == bad_val:
                                skipped_uuid.append(k)
                                del current_uuid[k]
                                logger.warning(f"UUID field {k[:8]} skipped — invalid value '{bad_val}'")
                                removed = True
                                break
                    if not removed:
                        # Can't identify which field, drop all UUID fields
                        skipped_uuid.extend(current_uuid.keys())
                        current_uuid = {}
                        logger.warning(f"Cleared all UUID fields after unparseable 400: {resp.text[:200]}")
                else:
                    break  # non-400 or no UUID fields left

        if resp and resp.status_code == 200:
            sent = list(current_uuid.keys())
            if skipped_uuid:
                logger.info(f"Push OK: {item_id[:8]} | sent UUID: {[k[:8] for k in sent]} | skipped: {[k[:8] for k in skipped_uuid]}")
                return {"ok": True, "uuid_fields_sent": sent, "uuid_fields_skipped": skipped_uuid}
            logger.info(f"Push OK: {item_id[:8]} | UUID fields: {[k[:8] for k in sent]}")
            return {"ok": True, "uuid_fields_sent": sent}

        err_text = resp.text[:300] if resp else "no response"
        logger.error(f"Push FAILED {item_id[:8]}: HTTP {resp.status_code if resp else '?'}\n{err_text}")
        return {"ok": False, "error": f"HTTP {resp.status_code if resp else '?'}: {err_text}"}

    finally:
        db.close()
