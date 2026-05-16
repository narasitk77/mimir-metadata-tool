from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Mimir DAM API
    MIMIR_BASE_URL: str = "https://apac.mjoll.no"
    MIMIR_TOKEN: str = ""  # optional static token; if empty, uses Cognito SRP auth
    # Cognito SRP auth (preferred — token auto-refreshes every 55 min)
    MIMIR_COGNITO_USER_POOL_ID: str = ""
    MIMIR_COGNITO_CLIENT_ID: str = ""
    MIMIR_USERNAME: str = ""
    MIMIR_PASSWORD: str = ""
    FOLDER_ID: str = ""

    # Gemini API
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"

    # Storage
    DATABASE_URL: str = "sqlite:///./data/mimir_assets.db"

    # Processing
    ITEMS_PER_PAGE: int = 100
    GEMINI_DELAY_MS: int = 6000  # 6s = 10 req/min, right at free-tier 10 RPM cap
    BATCH_SIZE: int = 20

    # Gemini Free Tier limits (gemini-2.5-flash)
    FREE_TIER_RPD: int = 500        # requests per day
    FREE_TIER_RPM: int = 10         # requests per minute
    FREE_TIER_TPD: int = 1_000_000  # tokens per day
    FREE_TIER_WARN_PCT: float = 0.9 # stop at 90% of any limit

    # ── Google SSO gate (optional) ────────────────────────────────────────────
    # Leave any of these empty → gate is OFF, app is open (trusted-LAN mode).
    # Set all four → every page requires a Google login with an @thestandard.co
    # account. Create the OAuth client at console.cloud.google.com → Credentials
    # → Web application, and register GOOGLE_AUTH_REDIRECT_URI exactly.
    GOOGLE_AUTH_CLIENT_ID: str = ""
    GOOGLE_AUTH_CLIENT_SECRET: str = ""
    GOOGLE_AUTH_REDIRECT_URI: str = ""   # e.g. https://mimir-tool.thestandard.co/auth/callback
    SESSION_SECRET_KEY: str = ""         # random 32+ chars; required when SSO is on

    class Config:
        env_file = ".env"
        extra = "ignore"  # ignore unrelated env vars (APP_PORT, POSTGRES_*, …)


settings = Settings()
