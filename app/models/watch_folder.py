"""WatchFolder — Mimir folders that the scheduler should poll for new items.

Each enabled row triggers an auto-fetch every N minutes (default 15). New
item_ids that don't yet exist in the assets table get inserted as `pending`
and the scheduler kicks off an auto-batch right after the poll if anything
new was found.

Auto-push is intentionally NOT supported — a human still clicks Push.
"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from app.database import Base


class WatchFolder(Base):
    __tablename__ = "watch_folders"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    folder_id       = Column(String(64),  unique=True, index=True, nullable=False)  # Mimir folder UUID
    label           = Column(String(256), default="")                                # human-readable name
    enabled         = Column(Boolean, default=True, nullable=False)                  # turn polling on/off per folder
    created_at      = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_polled_at  = Column(DateTime, nullable=True)
    last_new_count  = Column(Integer, default=0)
    last_error      = Column(String(500), default="")

    def to_dict(self) -> dict:
        return {
            "id":              self.id,
            "folder_id":       self.folder_id,
            "label":           self.label or "",
            "enabled":         bool(self.enabled),
            "created_at":      self.created_at.isoformat() if self.created_at else None,
            "last_polled_at":  self.last_polled_at.isoformat() if self.last_polled_at else None,
            "last_new_count":  self.last_new_count or 0,
            "last_error":      self.last_error or "",
        }
