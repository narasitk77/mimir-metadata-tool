import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from app.config import settings

# Ensure data directory exists for SQLite
db_path = settings.DATABASE_URL.replace("sqlite:///", "")
os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def run_migrations():
    """Add new columns to existing DB without losing data."""
    new_columns = [
        ("tokens_input",  "REAL"),
        ("tokens_output", "REAL"),
        ("processed_at",  "DATETIME"),
    ]
    with engine.connect() as conn:
        for col, col_type in new_columns:
            try:
                conn.execute(__import__("sqlalchemy").text(f"ALTER TABLE assets ADD COLUMN {col} {col_type}"))
                conn.commit()
            except Exception:
                pass  # column already exists
