from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime
from app.database import Base


class Person(Base):
    __tablename__ = "persons"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    name       = Column(String, nullable=False, index=True)
    title      = Column(String, default="")      # ตำแหน่ง เช่น "นายกรัฐมนตรี"
    keywords   = Column(String, default="")      # comma-sep สำหรับ event matching
    photo_data = Column(Text,   default="")      # base64 JPEG
    photo_mime = Column(String, default="image/jpeg")
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id":        self.id,
            "name":      self.name,
            "title":     self.title or "",
            "keywords":  self.keywords or "",
            "has_photo": bool(self.photo_data),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
