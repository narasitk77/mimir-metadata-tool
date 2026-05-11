"""
Shared utilities for AI controllers.
- extract_event_from_path: parse event/album name from ingest_path
- parse_gps: extract decimal lat/lon from EXIF dict
- reverse_geocode: GPS → Thai location name via OSM Nominatim (free, no key)
- search_news_context: Google News RSS → headline+snippet string for AI prompt
- fetch_best_image: fetch proxy (hi-res) image, fallback to thumbnail
"""
import hashlib
import io
import logging
import os
import re
import urllib.parse
import xml.etree.ElementTree as _ET
from pathlib import Path
from typing import Optional

import httpx
from app.services.cognito_auth import get_token as _get_cognito_token
from app.config import settings as _settings

logger = logging.getLogger(__name__)

# ── Image cache ────────────────────────────────────────────────────────────────
# Stores resized JPEG files: data/img_cache/<item_id>.jpg (~50–80 KB each)
# First analysis: download proxy (~1 MB PNG) → resize → save cache → send to AI
# Subsequent analyses: read cache directly, no download needed
_CACHE_DIR = Path(__file__).parent.parent.parent / "data" / "img_cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Max long-edge before sending to AI (px). Gemini optimal = 1568px max.
# Proxy is already 1080×720 so no resize needed — just PNG→JPEG conversion saves ~92%
_AI_MAX_PX   = 1568
_JPEG_QUALITY = 85

_SKIP_SEGS = {
    # Photo
    "hires", "hi-res", "hi_res", "raw", "proxies", "proxy", "highres",
    # Video — Sony camera card folders
    "xdroot", "m4root", "mxroot", "mxr00t", "xdcam", "dcim",
    "clip", "clips", "sub", "thmbnl", "xmeta", "general",
    # Generic
    "footage", "media", "video", "audio", "import", "export",
}

# CAM folder pattern: CAM1, CAM 1, CAM_1, C1, etc.
_CAM_RE = re.compile(r"^(cam|camera|c)\s*[\-_]?\s*\d+$", re.IGNORECASE)


def _is_technical_seg(s: str) -> bool:
    """Return True if the path segment is a technical/camera folder to skip."""
    sl = s.strip().lower()
    if sl in _SKIP_SEGS:
        return True
    if _CAM_RE.match(sl):
        return True
    # Skip segments that look like file extensions or are all uppercase short codes
    if len(sl) <= 3 and sl.isupper():
        return True
    return False


def extract_path_context(path: str) -> dict:
    """
    Parse ingest_path into structured video/photo context.

    Returns:
      {
        "event":   most specific shoot/date segment (e.g. "2026.02.23_Interview_EP5"),
        "series":  top-level program/series name (e.g. "THE SECRET SAUCE"),
        "camera":  camera identifier if found (e.g. "CAM 1"),
      }

    Handles both photo paths:
      PHOTOGRAPHER/<name>/2026/2026-04-05_สงกรานต์/Hires/IMG.jpg

    And video paths:
      THE SECRET SAUCE/2026.02.23_Interview_EP5/CAM 1/XDROOT/Clip/C0001.MXF
      2026.02.23 คุณธงชัย Noble Day 1/CAM 1/M4ROOT/CLIP/A001C001.MP4
    """
    parts = [p.strip() for p in path.replace("\\", "/").split("/") if p.strip()]
    if not parts:
        return {"event": "", "series": "", "camera": ""}

    # Strip PHOTOGRAPHER/<name> prefix (photo workflow)
    if parts and parts[0].upper() == "PHOTOGRAPHER":
        parts = parts[2:]   # drop PHOTOGRAPHER + photographer name

    # Strip year-only segment (e.g. "2026")
    if parts and re.match(r"^\d{4}$", parts[0]):
        parts = parts[1:]

    if not parts:
        return {"event": "", "series": "", "camera": ""}

    # Collect meaningful segments (skip technical folders and the filename)
    meaningful = []
    camera = ""
    for seg in parts[:-1]:   # skip filename (last part)
        if _CAM_RE.match(seg.lower()):
            camera = seg
        elif not _is_technical_seg(seg):
            meaningful.append(seg)

    if not meaningful:
        return {"event": "", "series": "", "camera": camera}

    # Heuristic: segment WITH a date pattern is the "episode/shoot" (event)
    # Segment WITHOUT a date is likely the "series/program" name
    _DATE_RE = re.compile(r"\d{4}[\.\-]\d{2}")

    dated   = [s for s in meaningful if _DATE_RE.search(s)]
    undated = [s for s in meaningful if not _DATE_RE.search(s)]

    event  = dated[0]   if dated   else meaningful[-1]
    series = undated[0] if undated else ""

    # If only one segment, it's both
    if len(meaningful) == 1:
        series = ""

    return {"event": event, "series": series, "camera": camera}


def extract_event_from_path(path: str) -> str:
    """Backward-compat wrapper — returns just the event name."""
    return extract_path_context(path)["event"]


def _dms_to_decimal(dms, ref: str) -> Optional[float]:
    """Convert DMS GPS value (list, float, or string) to decimal degrees."""
    try:
        if isinstance(dms, (int, float)):
            d = float(dms)
        elif isinstance(dms, list) and len(dms) >= 3:
            d = float(dms[0]) + float(dms[1]) / 60 + float(dms[2]) / 3600
        elif isinstance(dms, str):
            nums = re.findall(r"[\d.]+", dms)
            if len(nums) >= 3:
                d = float(nums[0]) + float(nums[1]) / 60 + float(nums[2]) / 3600
            elif len(nums) == 1:
                d = float(nums[0])
            else:
                return None
        else:
            return None
        if str(ref).upper() in ("S", "W"):
            d = -d
        return d
    except Exception:
        return None


def parse_gps(exif: dict) -> Optional[tuple]:
    """
    Extract (lat, lon) decimal degrees from EXIF dict.
    Returns None if GPS data is absent or cannot be parsed.
    """
    gps = exif.get("EXIF:GPS") or exif.get("GPS") or {}
    if not gps:
        return None
    lat = gps.get("GPSLatitude")
    lat_ref = gps.get("GPSLatitudeRef", "N")
    lon = gps.get("GPSLongitude")
    lon_ref = gps.get("GPSLongitudeRef", "E")
    if lat is None or lon is None:
        return None
    lat_dec = _dms_to_decimal(lat, lat_ref)
    lon_dec = _dms_to_decimal(lon, lon_ref)
    if lat_dec is None or lon_dec is None:
        return None
    if not (-90 <= lat_dec <= 90) or not (-180 <= lon_dec <= 180):
        return None
    return (lat_dec, lon_dec)


async def reverse_geocode(client: httpx.AsyncClient, lat: float, lon: float) -> str:
    """
    Convert GPS coordinates to Thai location string via OpenStreetMap Nominatim (free).
    Returns empty string on failure.
    """
    try:
        r = await client.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "json", "accept-language": "th"},
            headers={"User-Agent": "mimir-metadata-tool/1.0"},
            timeout=8,
        )
        if r.status_code == 200:
            data = r.json()
            addr = data.get("address", {})
            parts = []
            for key in ("amenity", "building", "road", "suburb", "quarter",
                        "city_district", "city", "town", "state"):
                v = addr.get(key)
                if v and v not in parts:
                    parts.append(v)
                if len(parts) >= 3:
                    break
            return ", ".join(parts) if parts else ""
    except Exception as e:
        logger.debug(f"Reverse geocode failed ({lat:.4f},{lon:.4f}): {e}")
    return ""


def extract_date_from_path(path: str) -> str:
    """
    Extract YYYY-MM-DD or YYYYMMDD date string from ingest_path.
    Returns empty string if no date found.
    """
    if not path:
        return ""
    # Match YYYY-MM-DD or YYYY.MM.DD or YYYYMMDD (2020-2030)
    m = re.search(r"(202\d)[.\-_](\d{2})[.\-_](\d{2})", path)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(202\d)(\d{2})(\d{2})", path)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


async def _fetch_article_text(client: httpx.AsyncClient, url: str, max_chars: int = 800) -> str:
    """Fetch a URL, strip HTML, return plain text up to max_chars."""
    if not url:
        return ""
    try:
        r = await client.get(url, timeout=12, follow_redirects=True)
        if r.status_code == 200:
            text = re.sub(r"<[^>]+>", " ", r.text)
            text = re.sub(r"\s+", " ", text).strip()
            return text[:max_chars]
    except Exception as e:
        logger.debug(f"Article fetch failed {url[:60]}: {e}")
    return ""


async def fetch_article_context(client: httpx.AsyncClient, urls: list, max_chars: int = 700) -> str:
    """Fetch plain text from up to 5 URLs and return as numbered context string."""
    if not urls:
        return ""
    parts = []
    for url in urls[:5]:
        text = await _fetch_article_text(client, url, max_chars)
        if text:
            parts.append(f"[บทความ {len(parts)+1}] {text}")
    return "\n".join(parts)


async def search_news_context(
    client: httpx.AsyncClient,
    query: str,
    date_hint: str = "",
    max_headlines: int = 6,
    fetch_top_articles: int = 2,
) -> str:
    """
    Search Google News RSS and return rich context for AI:
    - Up to max_headlines headlines + snippets
    - Full text fetched from top fetch_top_articles article URLs

    date_hint (YYYY-MM-DD): added to query for more targeted results.
    Returns empty string on failure.
    """
    if not query or not query.strip():
        return ""
    try:
        # Build query: combine event name + date hint for precision
        q = query.strip()
        if date_hint:
            q = f"{q} {date_hint}"
        encoded = urllib.parse.quote(q)
        rss_url = f"https://news.google.com/rss/search?q={encoded}&hl=th&gl=TH&ceid=TH:th"
        r = await client.get(rss_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if r.status_code != 200:
            return ""

        root  = _ET.fromstring(r.text)
        items = root.findall(".//item")[:max_headlines]
        if not items:
            return ""

        headlines = []
        article_urls = []
        for it in items:
            title  = (it.findtext("title") or "").strip()
            link   = (it.findtext("link") or "").strip()
            desc   = re.sub(r"<[^>]+>", " ", it.findtext("description") or "")
            desc   = re.sub(r"\s+", " ", desc).strip()[:250]
            source = (it.findtext("source") or "").strip()
            pub    = (it.findtext("pubDate") or "")[:16].strip()
            if not title:
                continue
            line = f"• {title}"
            if source: line += f" ({source})"
            if pub:    line += f" [{pub}]"
            if desc:   line += f"\n  {desc}"
            headlines.append(line)
            if link and len(article_urls) < fetch_top_articles:
                article_urls.append(link)

        # Fetch full article text from top URLs for deeper context
        article_parts = []
        for i, url in enumerate(article_urls, 1):
            text = await _fetch_article_text(client, url, max_chars=900)
            if text:
                article_parts.append(f"[เนื้อหาบทความ {i}] {text}")

        sections = []
        if headlines:
            sections.append("รายการข่าว:\n" + "\n".join(headlines))
        if article_parts:
            sections.append("\n".join(article_parts))

        result = "\n\n".join(sections)
        logger.info(f"News context: {len(headlines)} headlines, {len(article_parts)} articles for '{query[:40]}'")
        return result

    except Exception as e:
        logger.debug(f"News search failed for '{query[:40]}': {e}")
        return ""


def has_clear_faces(image_bytes: bytes, min_size: int = 60) -> bool:
    """
    Return True if the image contains at least one face large enough to identify.
    Uses OpenCV Haar cascade — runs locally, no API call needed.
    Falls back to True (safe default) if OpenCV is unavailable.
    """
    try:
        import cv2
        import numpy as np
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return True   # can't decode → assume might have faces
        cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        faces = cascade.detectMultiScale(
            img,
            scaleFactor=1.1,
            minNeighbors=5,
            minSize=(min_size, min_size),
        )
        found = len(faces) > 0
        logger.debug(f"Face detect: {len(faces)} face(s) found (min_size={min_size}px)")
        return found
    except ImportError:
        logger.debug("OpenCV not available — defaulting has_clear_faces=True")
        return True
    except Exception as e:
        logger.debug(f"Face detect error: {e} — defaulting True")
        return True


def _to_jpeg(raw: bytes) -> bytes:
    """Convert any image bytes → resized JPEG. Returns raw unchanged if Pillow unavailable."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw))
        # Resize only if larger than AI_MAX_PX (proxy is 1080×720 so usually no-op)
        if max(img.size) > _AI_MAX_PX:
            img.thumbnail((_AI_MAX_PX, _AI_MAX_PX), Image.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
        return buf.getvalue()
    except Exception as e:
        logger.debug(f"Pillow convert failed: {e}")
        return raw


def _cache_path(item_id: str) -> Path:
    return _CACHE_DIR / f"{item_id}.jpg"


def _read_cache(item_id: str) -> Optional[bytes]:
    p = _cache_path(item_id)
    if p.exists() and p.stat().st_size > 1000:
        return p.read_bytes()
    return None


def _write_cache(item_id: str, data: bytes) -> None:
    try:
        _cache_path(item_id).write_bytes(data)
    except Exception as e:
        logger.debug(f"Cache write failed for {item_id}: {e}")


async def fetch_best_image(
    client: httpx.AsyncClient,
    asset,
    mimir_base_url: str,
    mimir_token: str = "",   # kept for compat — ignored; uses _auth_header() instead
) -> tuple[bytes, str]:
    """
    Return (jpeg_bytes, 'image/jpeg') for AI analysis.

    Cache strategy (data/img_cache/<item_id>.jpg):
      HIT  → read from disk, no network call (~50-80 KB)
      MISS → download proxy (~1 MB) → convert PNG→JPEG → save to cache → return

    Fallback chain:
      proxy_url → thumbnail_url → refresh from Mimir API → retry
    """
    from app.database import SessionLocal

    # ── 1. Disk cache HIT ─────────────────────────────────────────────────
    cached = _read_cache(asset.item_id)
    if cached:
        logger.debug(f"[cache HIT] {asset.item_id[:8]} ({len(cached)//1024}KB)")
        return cached, "image/jpeg"

    # ── 2. Download helpers ───────────────────────────────────────────────
    _VIDEO_EXTS = (".mp4", ".mov", ".mxf", ".avi", ".m4v", ".mkv", ".webm", ".ts", ".mts")
    is_video_asset = (
        (getattr(asset, "item_type", "") or "").lower() in ("video",) or
        any((getattr(asset, "title", "") or "").lower().endswith(ext) for ext in _VIDEO_EXTS)
    )

    async def _download(url: str, skip_video: bool = False) -> Optional[bytes]:
        if not url:
            return None
        try:
            r = await client.get(url, timeout=40, follow_redirects=True)
            if r.status_code == 200 and len(r.content) > 1000:
                ct = r.headers.get("content-type", "")
                # Never try to parse video bytes as image — PIL will crash
                if "video" in ct or "octet-stream" in ct:
                    return None
                if skip_video:
                    return None
                if "image" in ct or len(r.content) > 50_000:
                    return r.content
        except Exception as e:
            logger.debug(f"Download failed {url[:60]}: {e}")
        return None

    async def _refresh_urls() -> tuple[str, str]:
        try:
            _token = _settings.MIMIR_TOKEN or await _get_cognito_token()
            r = await client.get(
                f"{mimir_base_url}/api/v1/items/{asset.item_id}",
                headers={"x-mimir-cognito-id-token": f"Bearer {_token}"},
                timeout=15,
            )
            if r.status_code == 200:
                data = r.json()
                new_proxy = data.get("proxy", "")
                new_thumb = data.get("thumbnail", "")
                db = SessionLocal()
                try:
                    from app.models.asset import Asset as _Asset
                    a = db.query(_Asset).filter(_Asset.item_id == asset.item_id).first()
                    if a:
                        if new_proxy: a.proxy_url = new_proxy
                        if new_thumb: a.thumbnail_url = new_thumb
                        db.commit()
                    asset.proxy_url = new_proxy
                    asset.thumbnail_url = new_thumb
                finally:
                    db.close()
                return new_proxy, new_thumb
        except Exception as e:
            logger.warning(f"URL refresh failed {asset.item_id[:8]}: {e}")
        return "", ""

    # ── 3. Download (proxy first, then thumbnail, then refresh+retry) ─────
    raw = None
    source = ""

    proxy_url = getattr(asset, "proxy_url", "") or ""
    thumb_url = getattr(asset, "thumbnail_url", "") or ""

    # For video assets skip proxy (it's a video file, not an image)
    if proxy_url and not is_video_asset:
        raw = await _download(proxy_url)
        if raw:
            source = "proxy"

    if raw is None and thumb_url:
        raw = await _download(thumb_url)
        if raw:
            source = "thumbnail"

    if raw is None:
        logger.info(f"Refreshing URLs for {asset.item_id[:8]}")
        new_proxy, new_thumb = await _refresh_urls()
        # For video always skip proxy on refresh too
        if not is_video_asset:
            raw = await _download(new_proxy)
        if raw is None:
            raw = await _download(new_thumb)
        if raw:
            source = "refreshed"

    if raw is None:
        raise ValueError(f"Cannot fetch any image for {asset.item_id}")

    # ── 4. Convert to JPEG + cache ────────────────────────────────────────
    jpeg = _to_jpeg(raw)
    _write_cache(asset.item_id, jpeg)
    logger.info(f"[cache MISS/{source}] {asset.item_id[:8]} raw={len(raw)//1024}KB → cached={len(jpeg)//1024}KB")
    return jpeg, "image/jpeg"


def clear_image_cache(item_id: Optional[str] = None) -> int:
    """
    Delete cached images. If item_id given, delete one file.
    If None, delete all. Returns number of files deleted.
    """
    if item_id:
        p = _cache_path(item_id)
        if p.exists():
            p.unlink()
            return 1
        return 0
    deleted = 0
    for f in _CACHE_DIR.glob("*.jpg"):
        f.unlink()
        deleted += 1
    return deleted


def cache_stats() -> dict:
    """Return stats about the image cache directory."""
    files = list(_CACHE_DIR.glob("*.jpg"))
    total_bytes = sum(f.stat().st_size for f in files)
    return {
        "count": len(files),
        "size_mb": round(total_bytes / 1_048_576, 2),
        "avg_kb": round(total_bytes / len(files) / 1024, 1) if files else 0,
    }
