"""UsageHistory — append-only record of every batch / push / clear-snapshot.

Survives `Clear DB` by design: this table is separate from `assets`, so wiping
the asset queue does NOT erase the historical usage record. This is the
data-driven foundation for monthly / yearly performance reports.

One row = one event (batch finished, push_all finished, clear_db snapshot).
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String, Text

from app.database import Base


class UsageHistory(Base):
    __tablename__ = "usage_history"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    timestamp     = Column(DateTime, default=datetime.utcnow, index=True, nullable=False)
    user          = Column(String(128), index=True, default="")
    event         = Column(String(32),  index=True, nullable=False)   # batch_done / push_all_done / clear_db_snapshot
    folder_label  = Column(String(256), default="")                   # album / "all" / folder id
    assets_count  = Column(Integer, default=0)                        # how many assets in this event
    tokens_input  = Column(Float, default=0.0)
    tokens_output = Column(Float, default=0.0)
    cost_usd      = Column(Float, default=0.0)                        # computed with the pricing in effect AT the event
    gemini_model  = Column(String(64), default="")                    # which model was used (price reproducible)
    duration_sec  = Column(Integer, default=0)                        # how long the event took
    notes         = Column(Text, default="")                          # free-form, optional

    def to_dict(self) -> dict:
        tin  = self.tokens_input  or 0
        tout = self.tokens_output or 0
        return {
            "id":            self.id,
            "timestamp":     self.timestamp.isoformat() if self.timestamp else None,
            "user":          self.user or "",
            "event":         self.event,
            "folder_label":  self.folder_label or "",
            "assets_count":  self.assets_count or 0,
            "tokens_input":  int(tin),
            "tokens_output": int(tout),
            "tokens_total":  int(tin + tout),
            "cost_usd":      round(self.cost_usd or 0.0, 6),
            "cost_thb":      round((self.cost_usd or 0.0) * 34, 4),
            "gemini_model":  self.gemini_model or "",
            "duration_sec":  self.duration_sec or 0,
            "notes":         self.notes or "",
        }
