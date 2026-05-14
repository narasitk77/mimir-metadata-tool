"""AuditLog model — append-only record of every meaningful action.

Captures what was done, when, by which entry point, with what result.
Designed to be lightweight (one INSERT per action, no joins) so wiring
it into hot paths like the batch loop is safe.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text

from app.database import Base


class AuditLog(Base):
    __tablename__ = "audit_log"

    id        = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True, nullable=False)
    action    = Column(String(64),  index=True, nullable=False)  # fetch / batch_start / push / push_all / reset / clear_db ...
    target    = Column(String(256), index=True, default="")      # item_id or folder_id or "all"
    status    = Column(String(16),  default="ok")                # ok / error / cancelled
    message   = Column(Text, default="")                         # human-readable summary
    details   = Column(Text, default="")                         # JSON string (optional structured data)

    def to_dict(self) -> dict:
        return {
            "id":        self.id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "action":    self.action,
            "target":    self.target or "",
            "status":    self.status or "ok",
            "message":   self.message or "",
            "details":   self.details or "",
        }
