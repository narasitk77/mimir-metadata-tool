from sqlalchemy import Column, DateTime, Float, String, Text
from app.database import Base


class Asset(Base):
    __tablename__ = "assets"

    item_id = Column(String, primary_key=True, index=True)
    thumbnail_url = Column(String, default="")
    status = Column(String, default="pending")  # pending | processing | done | error
    error_log = Column(Text, default="")

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
    exif_url = Column(String, default="")  # exifTagsUrl จาก Mimir

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

    def to_dict(self):
        return {
            "item_id": self.item_id,
            "thumbnail_url": self.thumbnail_url,
            "status": self.status,
            "error_log": self.error_log,
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
        }
