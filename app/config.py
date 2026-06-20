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

    # Authelia OIDC (confidential BFF). PUBLIC https endpoints only — never the
    # internal http://authelia:9091 (that caused the X-Forwarded-Proto 400 / 401).
    OIDC_CLIENT_ID: str = "memos-ng"
    OIDC_CLIENT_SECRET: str = ""  # set via stack .env in prod
    OIDC_ISSUER: str = "https://auth.discomarder.live"
    OIDC_AUTHORIZE_ENDPOINT: str = "https://auth.discomarder.live/api/oidc/authorization"
    OIDC_TOKEN_ENDPOINT: str = "https://auth.discomarder.live/api/oidc/token"
    OIDC_JWKS_URI: str = "https://auth.discomarder.live/jwks.json"
    OIDC_END_SESSION_ENDPOINT: str = "https://auth.discomarder.live/api/oidc/logout"
    OIDC_REDIRECT_URI: str = "https://memos-ng.discomarder.live/api/auth/callback"
    OIDC_POST_LOGOUT_REDIRECT_URI: str = "https://memos-ng.discomarder.live/"

    # BFF session. SESSION_SECRET is a Fernet key encrypting tokens at rest; the
    # default is DEV-ONLY and must be overridden in prod via the stack .env.
    SESSION_SECRET: str = "TzWJWb-LwdXWdsb92xjiY7yqMaW1lWZJPDNZSg-qqd8="
    COOKIE_SECURE: bool = True
    SESSION_ABSOLUTE_DAYS: int = 90
    SESSION_ROLLING: bool = True

    @property
    def is_production(self) -> bool:
        return self.ENV != "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
