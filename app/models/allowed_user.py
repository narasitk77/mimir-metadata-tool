from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Boolean
from app.database import Base


class AllowedUser(Base):
    __tablename__ = "allowed_users"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    email      = Column(String, unique=True, nullable=False, index=True)
    is_admin   = Column(Boolean, default=False, nullable=False)
    added_by   = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id":         self.id,
            "email":      self.email,
            "is_admin":   bool(self.is_admin),
            "added_by":   self.added_by or "",
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
