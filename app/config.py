from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    MIMIR_BASE_URL: str = "https://apac.mjoll.no"
    MIMIR_TOKEN: str = ""
    FOLDER_ID: str = ""
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.0-flash"
    DATABASE_URL: str = "sqlite:///./data/mimir_assets.db"
    ITEMS_PER_PAGE: int = 100
    GEMINI_DELAY_MS: int = 4500
    BATCH_SIZE: int = 20

    class Config:
        env_file = ".env"


settings = Settings()
