from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    MIMIR_BASE_URL: str = "https://apac.mjoll.no"
    MIMIR_TOKEN: str = ""
    FOLDER_ID: str = ""
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"
    DATABASE_URL: str = "sqlite:///./data/mimir_assets.db"
    ITEMS_PER_PAGE: int = 100
    GEMINI_DELAY_MS: int = 4500
    BATCH_SIZE: int = 20

    # Gemini Free Tier limits (gemini-2.5-flash)
    FREE_TIER_RPD: int = 500        # requests per day
    FREE_TIER_RPM: int = 10         # requests per minute
    FREE_TIER_TPD: int = 1_000_000  # tokens per day
    FREE_TIER_WARN_PCT: float = 0.9 # หยุดที่ 90% ของ limit

    class Config:
        env_file = ".env"


settings = Settings()
