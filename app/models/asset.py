from sqlalchemy import Column, DateTime, Float, String, Text
from app.database import Base


def _extract_event(path: str) -> str:
    """Lightweight duplicate of extract_event_from_path (avoids circular import)."""
    import re
    _SKIP = {"hires", "hi-res", "hi_res", "raw", "proxies", "proxy", "highres"}
    parts = path.replace("\\", "/").split("/") if path else []
    start = 2 if len(parts) >= 2 and parts[0].upper() == "PHOTOGRAPHER" else 0
    best = ""
    for seg in parts[start:]:
        s = seg.strip()
        if not s or s.upper() in {x.upper() for x in _SKIP} or len(s) <= 3:
            continue
        if " " in s or any("\u0E00" <= c <= "\u0E7F" for c in s):
            return s
        best = best or s
    return best


class Asset(Base):
    __tablename__ = "assets"

    item_id = Column(String, primary_key=True, index=True)
    thumbnail_url = Column(String, default="")
    status = Column(String, default="pending")  # pending | processing | done | error
    error_log = Column(Text, default="")

    # --- Source folder ---
    folder_id = Column(String, default="", index=True)

    # --- From Mimir API ---
    title = Column(String, default="")
    item_type = Column(String, default="")
    media_created_on = Column(String, default="")
    file_type = Column(String, default="")
    width = Column(String, default="")
    height = Column(String, default="")
    aspect_ratio = Column(String, default="")
    filesize_mb = Column(Float, nullable=True)
    ingest_path = Column(String, default="")
    exif_url = Column(String, default="")   # exifTagsUrl จาก Mimir
    proxy_url = Column(String, default="")  # proxy image URL (1-2MB, better quality than thumbnail)

    # --- AI-generated (core) ---
    ai_title = Column(String, default="")
    ai_description = Column(Text, default="")
    ai_category = Column(String, default="")
    ai_subcat = Column(String, default="")
    ai_keyword = Column(String, default="")

    # --- AI-generated (extended) ---
    ai_editorial_categories = Column(String, default="")
    ai_location = Column(String, default="")
    ai_persons = Column(String, default="")
    ai_episode_segment = Column(String, default="")
    ai_event_occasion = Column(String, default="")
    ai_emotion_mood = Column(String, default="")
    ai_language = Column(String, default="")
    ai_department = Column(String, default="")
    ai_project_series = Column(String, default="")
    ai_right_license = Column(String, default="")
    ai_deliverable_type = Column(String, default="")
    ai_subject_tags = Column(String, default="")
    ai_technical_tags = Column(String, default="")
    ai_visual_attributes = Column(String, default="")

    # --- From EXIF (auto-filled) ---
    exif_photographer = Column(String, default="")   # EXIF Artist
    exif_camera_model = Column(String, default="")   # EXIF Make + Model
    exif_credit_line = Column(String, default="")    # EXIF Copyright
    exif_iso = Column(String, default="")
    exif_aperture = Column(String, default="")
    exif_shutter = Column(String, default="")
    exif_focal_length = Column(String, default="")

    # --- Token usage ---
    tokens_input = Column(Float, nullable=True)
    tokens_output = Column(Float, nullable=True)
    processed_at = Column(DateTime, nullable=True)

    # --- Default ---
    rights = Column(String, default="THE STANDARD/All Rights Reserved")

    # --- User-supplied context for re-analysis ---
    context_urls = Column(Text, default="")   # JSON array of up to 5 URLs
    context_text = Column(Text, default="")   # free-text hint

    def to_dict(self):
        return {
            "item_id": self.item_id,
            "thumbnail_url": self.thumbnail_url,
            "status": self.status,
            "error_log": self.error_log,
            "folder_id": self.folder_id or "",
            # Mimir
            "title": self.title,
            "item_type": self.item_type,
            "media_created_on": self.media_created_on,
            "file_type": self.file_type,
            "width": self.width,
            "height": self.height,
            "aspect_ratio": self.aspect_ratio,
            "filesize_mb": self.filesize_mb,
            "ingest_path": self.ingest_path,
            "exif_url": self.exif_url,
            "proxy_url": self.proxy_url or "",
            # AI core
            "ai_title": self.ai_title,
            "ai_description": self.ai_description,
            "ai_category": self.ai_category,
            "ai_subcat": self.ai_subcat,
            "ai_keyword": self.ai_keyword,
            # AI extended
            "ai_editorial_categories": self.ai_editorial_categories,
            "ai_location": self.ai_location,
            "ai_persons": self.ai_persons,
            "ai_episode_segment": self.ai_episode_segment,
            "ai_event_occasion": self.ai_event_occasion,
            "ai_emotion_mood": self.ai_emotion_mood,
            "ai_language": self.ai_language,
            "ai_department": self.ai_department,
            "ai_project_series": self.ai_project_series,
            "ai_right_license": self.ai_right_license,
            "ai_deliverable_type": self.ai_deliverable_type,
            "ai_subject_tags": self.ai_subject_tags,
            "ai_technical_tags": self.ai_technical_tags,
            "ai_visual_attributes": self.ai_visual_attributes,
            # EXIF
            "exif_photographer": self.exif_photographer,
            "exif_camera_model": self.exif_camera_model,
            "exif_credit_line": self.exif_credit_line,
            "exif_iso": self.exif_iso,
            "exif_aperture": self.exif_aperture,
            "exif_shutter": self.exif_shutter,
            "exif_focal_length": self.exif_focal_length,
            # Token
            "tokens_input": self.tokens_input,
            "tokens_output": self.tokens_output,
            # Default
            "rights": self.rights,
            # Context
            "context_urls": self.context_urls or "",
            "context_text": self.context_text or "",
            # Derived
            "album_key": _extract_event(self.ingest_path or ""),
        }
