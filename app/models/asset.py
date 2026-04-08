from sqlalchemy import Column, Float, String, Text
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

    # --- AI-generated ---
    ai_title = Column(String, default="")
    ai_description = Column(Text, default="")
    ai_category = Column(String, default="")
    ai_subcat = Column(String, default="")
    ai_keyword = Column(String, default="")

    # --- Default ---
    rights = Column(String, default="THE STANDARD/All Rights Reserved")

    def to_dict(self):
        return {
            "item_id": self.item_id,
            "thumbnail_url": self.thumbnail_url,
            "status": self.status,
            "error_log": self.error_log,
            "title": self.title,
            "item_type": self.item_type,
            "media_created_on": self.media_created_on,
            "file_type": self.file_type,
            "width": self.width,
            "height": self.height,
            "aspect_ratio": self.aspect_ratio,
            "filesize_mb": self.filesize_mb,
            "ingest_path": self.ingest_path,
            "ai_title": self.ai_title,
            "ai_description": self.ai_description,
            "ai_category": self.ai_category,
            "ai_subcat": self.ai_subcat,
            "ai_keyword": self.ai_keyword,
            "rights": self.rights,
        }
