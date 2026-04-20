from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    MIMIR_BASE_URL: str = "https://apac.mjoll.no"
    MIMIR_TOKEN: str = ""
    FOLDER_ID: str = ""
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-haiku-4-5-20251001"
    AI_PROVIDER: str = "claude"  # "gemini" หรือ "claude"
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

    class Config:
        env_file = ".env"


settings = Settings()
