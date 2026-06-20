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

    # Resumable upload (T2.1).
    UPLOAD_CHUNK_SIZE: int = 5 * 1024 * 1024  # 5 MiB
    UPLOAD_EXPIRY_HOURS: int = 24
    MAX_UPLOAD_MIB: int = 1024  # prod parity (system_setting MAX_UPLOAD_SIZE_MIB)

    # Slow-lane transcode + thermal guard (T2.3). Saturn is thermally bound: defer the
    # H.264 transcode while the SoC is above THERMAL_MAX_C. Missing zone path → no-op.
    THERMAL_ZONE: str = "/sys/class/thermal/thermal_zone0/temp"
    THERMAL_MAX_C: int = 70
    MEDIA_MAX_ATTEMPTS: int = 3

    # Web push (VAPID) — T2.5. Empty keys = push disabled (every send is a no-op), so the
    # pipeline runs unchanged without VAPID configured. Generate a keypair with `vapid --gen`
    # (py-vapid) and set both halves via the stack .env in prod.
    VAPID_PUBLIC_KEY: str = ""
    VAPID_PRIVATE_KEY: str = ""
    VAPID_SUBJECT: str = "mailto:admin@discomarder.live"

    # Authelia OIDC (confidential BFF). PUBLIC https endpoints only — never the
    # internal http://authelia:9091 (that caused the X-Forwarded-Proto 400 / 401).
    OIDC_CLIENT_ID: str = "memos-ng"
    OIDC_CLIENT_SECRET: str = ""  # set via stack .env in prod
    OIDC_ISSUER: str = "https://auth.discomarder.live"
    OIDC_AUTHORIZE_ENDPOINT: str = "https://auth.discomarder.live/api/oidc/authorization"
    OIDC_TOKEN_ENDPOINT: str = "https://auth.discomarder.live/api/oidc/token"
    OIDC_JWKS_URI: str = "https://auth.discomarder.live/jwks.json"
    # Authelia's id_token is minimal (sub only); preferred_username/name/email/groups come
    # from userinfo. Without this the app keyed users by the opaque sub UUID.
    OIDC_USERINFO_ENDPOINT: str = "https://auth.discomarder.live/api/oidc/userinfo"
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
