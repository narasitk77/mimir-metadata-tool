"""MimirOption — cache of values Mimir actually accepts for each UUID field.

Mimir's dropdown fields each have a closed set of option IDs. We don't have
that list exposed via API, so we learn it from observation: every value Mimir
accepts on a successful push gets recorded here. Subsequent pushes filter
AI-generated values through this cache so we never resend something Mimir
already rejected.

Schema is intentionally append-mostly (unique on field+value) and small —
one row per (field, accepted_value) pair, plus a count and last_seen for
audit.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint

from app.database import Base


class MimirOption(Base):
    __tablename__ = "mimir_options"
    __table_args__ = (
        UniqueConstraint("field_uuid", "option_value", name="uq_mimir_options_field_value"),
    )

    id           = Column(Integer, primary_key=True, autoincrement=True)
    field_uuid   = Column(String(64),  index=True, nullable=False)
    option_value = Column(String(256), index=True, nullable=False)
    accept_count = Column(Integer, default=1, nullable=False)
    last_seen    = Column(DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id":           self.id,
            "field_uuid":   self.field_uuid,
            "option_value": self.option_value,
            "accept_count": self.accept_count,
            "last_seen":    self.last_seen.isoformat() if self.last_seen else None,
        }
