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
        # token tracking
        ("tokens_input",           "REAL"),
        ("tokens_output",          "REAL"),
        ("processed_at",           "DATETIME"),
        # exif url
        ("exif_url",               "TEXT"),
        # AI extended
        ("ai_editorial_categories","TEXT"),
        ("ai_location",            "TEXT"),
        ("ai_persons",             "TEXT"),
        ("ai_episode_segment",     "TEXT"),
        ("ai_event_occasion",      "TEXT"),
        ("ai_emotion_mood",        "TEXT"),
        ("ai_language",            "TEXT"),
        ("ai_department",          "TEXT"),
        ("ai_project_series",      "TEXT"),
        ("ai_right_license",       "TEXT"),
        ("ai_deliverable_type",    "TEXT"),
        ("ai_subject_tags",        "TEXT"),
        ("ai_technical_tags",      "TEXT"),
        ("ai_visual_attributes",   "TEXT"),
        # EXIF
        ("exif_photographer",      "TEXT"),
        ("exif_camera_model",      "TEXT"),
        ("exif_credit_line",       "TEXT"),
        ("exif_iso",               "TEXT"),
        ("exif_aperture",          "TEXT"),
        ("exif_shutter",           "TEXT"),
        ("exif_focal_length",      "TEXT"),
    ]
    with engine.connect() as conn:
        for col, col_type in new_columns:
            try:
                conn.execute(__import__("sqlalchemy").text(f"ALTER TABLE assets ADD COLUMN {col} {col_type}"))
                conn.commit()
            except Exception:
                pass  # column already exists
