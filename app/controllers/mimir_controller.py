import asyncio
import logging
import re as _re
from datetime import datetime
from typing import AsyncGenerator, List, Optional

import httpx
from app.config import settings
from app.database import SessionLocal
from app.models.asset import Asset
from app.models.mimir_option import MimirOption
from app.services.cognito_auth import get_token as _get_cognito_token


def _filter_through_cache(uuid_fields: dict) -> dict:
    """Filter list-typed UUID payloads through the MimirOption cache.

    Three-way decision per value:
      - status "bad"  → drop (Mimir rejected it before — don't waste a retry)
      - status "ok"   → keep (confirmed good)
      - not in cache  → keep as a DISCOVERY CANDIDATE — give it a chance; the
                        value-level retry will sort it out and the result is
                        recorded so we never test the same value twice.

    A field is dropped only if every value resolves to "bad"/empty."""
    filtered: dict = {}
    db = SessionLocal()
    try:
        for k, v in uuid_fields.items():
            if not isinstance(v, list) or not v:
                filtered[k] = v
                continue
            rows = db.query(MimirOption).filter(MimirOption.field_uuid == k).all()
            bad = {r.option_value for r in rows if (r.status or "ok") == "bad"}
            kept = [item for item in v if str(item) not in bad]
            if kept:
                filtered[k] = kept
    except Exception as exc:
        logger.warning(f"Cache filter failed, sending raw: {exc}")
        return uuid_fields
    finally:
        db.close()
    return filtered


def _record_accepted(accepted_payload: dict) -> None:
    """Mark every value in an accepted payload as status 'ok'."""
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        for k, v in accepted_payload.items():
            if not k or not v:
                continue
            for item in (v if isinstance(v, list) else [v]):
                s = str(item).strip()
                if not s:
                    continue
                row = (db.query(MimirOption)
                         .filter(MimirOption.field_uuid == k,
                                 MimirOption.option_value == s)
                         .first())
                if row:
                    row.status = "ok"
                    row.accept_count = (row.accept_count or 0) + 1
                    row.last_seen = now
                else:
                    db.add(MimirOption(field_uuid=k, option_value=s, status="ok",
                                       accept_count=1, last_seen=now))
        db.commit()
    except Exception as exc:
        logger.warning(f"Option cache record (accept) failed: {exc}")
        db.rollback()
    finally:
        db.close()


def _record_rejected(field_uuid: str, value: str) -> None:
    """Mark one value as status 'bad' so future pushes skip it up-front."""
    if not field_uuid or not value:
        return
    db = SessionLocal()
    try:
        s = str(value).strip()
        row = (db.query(MimirOption)
                 .filter(MimirOption.field_uuid == field_uuid,
                         MimirOption.option_value == s)
                 .first())
        if row:
            row.status = "bad"
            row.last_seen = datetime.utcnow()
        else:
            db.add(MimirOption(field_uuid=field_uuid, option_value=s,
                               status="bad", accept_count=0,
                               last_seen=datetime.utcnow()))
        db.commit()
    except Exception as exc:
        logger.warning(f"Option cache record (reject) failed: {exc}")
        db.rollback()
    finally:
        db.close()


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


def _first_lower(v: str) -> str:
    """First comma-element only, lowercased — for SINGLE-value dropdowns.
    Mimir's editorial_categories rejects a multi-value array (it joins the
    array and looks for one option matching the whole 'a,b' string). The
    option cache only ever recorded single values as accepted, confirming
    the field takes exactly one value."""
    parts = [x.strip().lower() for x in str(v).split(",") if x.strip()]
    return parts[0] if parts else ""


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
    "ai_editorial_categories":  ("2f5f0fb9-b4a7-44a1-92b7-a12daaaf625e", _first_lower),         # single value e.g. "lifestyle"
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
    """Extract folder display name from a Mimir search result item.

    Mimir folder objects use `name` field; media items use `originalFileName`
    or `title`. Check `name` first so subfolder discovery works correctly.
    """
    return (
        item.get("name")
        or item.get("originalFileName")
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


_HIRES_PATH_SEGS  = {"hires", "hi-res", "hi_res", "highres", "high res", "high_res"}
_FB_PATH_SEGS     = {"fb", "facebook", "fb-size", "fbsize", "fb_size"}


async def fetch_all_items(
    folder_id: Optional[str] = None,
    context_text: str = "",
    subfolder_filter: Optional[str] = None,
) -> AsyncGenerator[dict, None]:
    """
    Fetch all items from Mimir folder and upsert into DB.
    Yields progress dicts for SSE streaming.

    folder_id: UUID โดยตรง หรือ None เพื่อใช้ค่าจาก config
    context_text: optional folder-level context for AI analysis
    subfolder_filter:
        None / "all"      → no filtering (default behaviour)
        "hires_only"      → include only items whose parent folder name is in
                            HIRES_PATH_SEGS or items directly in the event folder
                            (i.e. not in any recognised sub-type folder)
        "no_fb"           → exclude items whose parent folder name is in FB_PATH_SEGS
        "hires_no_fb"     → hires_only + also explicitly exclude FB paths
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

            # Pre-compute subfolder_filter flags once per page (not per item)
            _do_hires_only = subfolder_filter in ("hires_only", "hires_no_fb")
            _do_no_fb      = subfolder_filter in ("no_fb", "hires_no_fb")

            db = SessionLocal()
            try:
                skipped_meta = 0
                skipped_filter = 0
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

                    # ── subfolder_filter ──────────────────────────────────────
                    if _do_hires_only or _do_no_fb:
                        # Determine parent folder name from ingest path
                        path_parts = ingest_path_lower.replace("\\", "/").split("/")
                        parent_seg = path_parts[-2].strip() if len(path_parts) >= 2 else ""
                        is_hires = parent_seg in _HIRES_PATH_SEGS
                        is_fb    = parent_seg in _FB_PATH_SEGS
                        if _do_no_fb and is_fb:
                            skipped_filter += 1
                            continue
                        if _do_hires_only and not is_hires:
                            # Skip non-hires sub-type folders; only include explicit hires
                            # and items sitting directly in an event folder (no recognised sub-type)
                            _all_known_subtypes = _HIRES_PATH_SEGS | _FB_PATH_SEGS | {
                                "logo", "raw", "proxy", "proxies",
                                "social", "web", "thumb", "thumbnail",
                                "small", "medium", "large",
                            }
                            if parent_seg in _all_known_subtypes:
                                skipped_filter += 1
                                continue
                    # ─────────────────────────────────────────────────────────

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
                if skipped_filter:
                    logger.info(f"Skipped {skipped_filter} items by subfolder_filter={subfolder_filter!r}")
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

            # Pre-filter through self-learning cache so we never retry the same
            # value Mimir already rejected in a previous push. First-time fields
            # bypass the filter (cache is empty → discovery mode).
            uuid_fields = _filter_through_cache(uuid_fields)

            # ── Smart retry loop — value-level ─────────────────────────────────
            # Mimir returns 400 with: invalid value X for field: "Y".
            # We drop X (one value, not the whole field), record it as rejected
            # in the option cache, and retry. List fields keep their other
            # values; only when a field is emptied is it skipped entirely.
            # This is the auto-discovery mechanism: un-cached values get tried
            # once, and the ok/bad verdict is remembered forever.
            _invalid_pat = _re.compile(r'invalid value (.+?) for field', _re.IGNORECASE)
            headers = {
                **(await _auth_header()),
                "Content-Type": "application/json",
            }
            current_uuid = dict(uuid_fields)
            skipped_uuid: list[str] = []
            resp = None

            # Worst case = one retry per individual value, plus slack.
            max_attempts = sum(len(v) if isinstance(v, list) else 1
                               for v in uuid_fields.values()) + 2

            for _attempt in range(max(max_attempts, 1)):
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
                if resp.status_code != 400 or not current_uuid:
                    break

                m = _invalid_pat.search(resp.text)
                bad_val = m.group(1).strip() if m else ""
                removed = False

                for k in list(current_uuid.keys()):
                    v = current_uuid[k]
                    if isinstance(v, list):
                        joined = ",".join(str(x) for x in v)
                        elems  = [str(x) for x in v]
                        if bad_val and bad_val in elems:
                            # Mimir named one bad element — drop just it.
                            v_new = [x for x in v if str(x) != bad_val]
                            _record_rejected(k, bad_val)
                        elif bad_val and bad_val == joined:
                            # Whole-list rejected, Mimir won't say which element.
                            # Drop the LAST (most likely the discovery candidate),
                            # record it bad, retry with the rest.
                            _record_rejected(k, elems[-1])
                            v_new = v[:-1]
                        else:
                            continue   # this field isn't the culprit
                        if v_new:
                            current_uuid[k] = v_new
                        else:
                            del current_uuid[k]
                            skipped_uuid.append(k)
                        logger.warning(f"UUID field {k[:8]} — dropped value '{bad_val or elems[-1]}'")
                        removed = True
                        break
                    else:  # single string value
                        if bad_val and str(v) == bad_val:
                            _record_rejected(k, str(v))
                            del current_uuid[k]
                            skipped_uuid.append(k)
                            logger.warning(f"UUID field {k[:8]} skipped — invalid value '{bad_val}'")
                            removed = True
                            break

                if not removed:
                    # Fallback: substring-match the response body.
                    for k in list(current_uuid.keys()):
                        v = current_uuid[k]
                        v_str = ",".join(str(x) for x in v) if isinstance(v, list) else str(v)
                        if v_str and v_str in resp.text:
                            del current_uuid[k]
                            skipped_uuid.append(k)
                            logger.warning(f"UUID field {k[:8]} skipped via fallback")
                            removed = True
                            break
                if not removed:
                    # Truly unparseable — drop all UUID fields, last resort.
                    skipped_uuid.extend(current_uuid.keys())
                    current_uuid = {}
                    logger.warning(f"Cleared all UUID fields after unparseable 400: {resp.text[:200]}")

        if resp and resp.status_code == 200:
            sent = list(current_uuid.keys())
            # Record accepted values so the next push pre-filters them through
            # the cache and avoids re-learning the same lesson.
            _record_accepted(current_uuid)
            # Capture Mimir's confirmation body so the audit trail can prove
            # the write was actually accepted (not just a 200 echo with no change).
            resp_excerpt = resp.text[:400] if resp.text else ""
            if skipped_uuid:
                logger.info(f"Push OK: {item_id[:8]} | sent UUID: {[k[:8] for k in sent]} | skipped: {[k[:8] for k in skipped_uuid]}")
                return {"ok": True, "uuid_fields_sent": sent, "uuid_fields_skipped": skipped_uuid,
                        "status_code": 200, "response_excerpt": resp_excerpt,
                        "fields_pushed": list(formdata.keys())}
            logger.info(f"Push OK: {item_id[:8]} | UUID fields: {[k[:8] for k in sent]}")
            return {"ok": True, "uuid_fields_sent": sent,
                    "status_code": 200, "response_excerpt": resp_excerpt,
                    "fields_pushed": list(formdata.keys())}

        err_text = resp.text[:500] if resp else "no response"
        status_code = resp.status_code if resp else 0
        logger.error(f"Push FAILED {item_id[:8]}: HTTP {status_code}\n{err_text}")
        return {"ok": False, "error": f"HTTP {status_code}: {err_text}",
                "status_code": status_code, "response_excerpt": err_text}

    finally:
        db.close()
