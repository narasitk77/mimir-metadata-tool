from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    MIMIR_BASE_URL: str = "https://apac.mjoll.no"
    MIMIR_TOKEN: str = ""  # optional static token; if empty, uses Cognito SRP auth
    # Cognito SRP auth (same as Node.js app — preferred over static token)
    MIMIR_COGNITO_USER_POOL_ID: str = ""
    MIMIR_COGNITO_CLIENT_ID: str = ""
    MIMIR_USERNAME: str = ""
    MIMIR_PASSWORD: str = ""
    FOLDER_ID: str = ""
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"
    DATABASE_URL: str = "sqlite:///./data/mimir_assets.db"
    ITEMS_PER_PAGE: int = 100
    GEMINI_DELAY_MS: int = 7000  # 7s = ~8 req/min ต่ำกว่า free tier 10 RPM
    BATCH_SIZE: int = 20

    # Gemini Free Tier limits (gemini-2.5-flash)
    FREE_TIER_RPD: int = 500        # requests per day
    FREE_TIER_RPM: int = 10         # requests per minute
    FREE_TIER_TPD: int = 1_000_000  # tokens per day
    FREE_TIER_WARN_PCT: float = 0.9 # หยุดที่ 90% ของ limit

    # Sub-path when running behind a reverse proxy (e.g. "/ai-tool" — no trailing slash)
    APP_ROOT_PATH: str = ""

    # ── Google SSO gate (internal users only) ─────────────────────────────────
    # If GOOGLE_AUTH_CLIENT_ID is empty the gate is disabled (open access).
    GOOGLE_AUTH_CLIENT_ID: str = ""
    GOOGLE_AUTH_CLIENT_SECRET: str = ""
    GOOGLE_AUTH_REDIRECT_URI: str = ""  # e.g. "http://192.168.21.220:8765/auth/callback"
    ALLOWED_EMAIL_DOMAIN: str = "thestandard.co"
    SESSION_SECRET_KEY: str = ""  # required when SSO is enabled

    # Whitelist mode — เมื่อตั้งค่าจะอนุญาตเฉพาะอีเมลในรายการ (แทน wildcard ทั้งโดเมน)
    # ALLOWED_EMAILS comma-separated — bootstrap whitelist (อ่านจาก env ตอน startup)
    # ADMIN_EMAILS subset ที่เห็นหน้า /admin/users ใช้เพิ่ม/ลบสิทธิ์คนอื่น
    ALLOWED_EMAILS: str = ""
    ADMIN_EMAILS:   str = ""

    class Config:
        env_file = ".env"
        extra = "ignore"  # ignore unrelated env vars (APP_PORT, POSTGRES_*, …)


settings = Settings()
