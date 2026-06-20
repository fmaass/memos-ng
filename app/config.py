from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Runtime
    ENV: str = "development"

    # Database (async + sync DSN for Alembic). Provided by the stack .env in prod.
    DATABASE_URL: str = "postgresql+asyncpg://memosng:memosng@localhost:5433/memosng"
    DATABASE_URL_SYNC: str = "postgresql+psycopg2://memosng:memosng@localhost:5433/memosng"

    # Storage — bind-mounted at /app/data in the container (layout identical to old memos).
    DATA_ROOT: str = "./data"

    # Media worker thermal/concurrency guards (Saturn is thermally bound — see plan §3).
    FFMPEG_THREADS: int = 1
    WORKER_CONCURRENCY: int = 1

    @property
    def is_production(self) -> bool:
        return self.ENV != "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
