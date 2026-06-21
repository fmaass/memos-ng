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

    # DB connection pool (prod only — dev/test use NullPool so per-test event loops are safe).
    # memos-ng SHARES Postgres17 with prod; cap total connections (pool_size + max_overflow) so a
    # request burst can't exhaust the shared slots (TooManyConnectionsError starves prod too).
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 5
    DB_POOL_RECYCLE: int = 1800
    # Explicit, short pool checkout timeout: fail loud at 10s, not the 30s default that lets a burst
    # queue silently and hang the feed. Exhaustion is a hold-time bug to fix, not a wait to tune.
    DB_POOL_TIMEOUT: int = 10

    # Derived media tiers are immutable (uid-keyed) — let browsers cache them hard so re-renders
    # of the feed/gallery don't re-request every image (1 year, immutable).
    ASSET_CACHE_SECONDS: int = 31536000

    # HMAC-signed media URLs (the DB-free byte path). The feed mints short-lived signed derived-tier
    # URLs; the serve route verifies the signature with no DB/auth dependency, then streams. The
    # signing key defaults off SESSION_SECRET so prod needs no new env; PREV is the rotation slot.
    ASSET_URL_SECRET: str = ""  # empty → derived from SESSION_SECRET (see services/asset_urls)
    ASSET_URL_SECRET_PREV: str = ""
    ASSET_URL_TTL: int = 7200  # seconds a signed URL stays valid
    ASSET_URL_BUCKET: int = 3600  # mint bucket → URLs stable within the window (browser cache hits)

    # Media worker thermal/concurrency guards (Saturn is thermally bound — see plan §3).
    FFMPEG_THREADS: int = 1
    WORKER_CONCURRENCY: int = 1

    # Which media lanes a worker drains. Default = both (single-worker dev/CI + the original
    # in-stack Saturn worker). The Merkur offload runs a second worker with WORKER_LANES=slow
    # (CPU-heavy H.264 transcode) while Saturn runs WORKER_LANES=fast (light thumbs/poster/dims),
    # so transcode load leaves the thermally-bound NAS entirely. See ADR-0006 (revisit).
    WORKER_LANES: str = "fast,slow"

    # Resumable upload (T2.1).
    UPLOAD_CHUNK_SIZE: int = 5 * 1024 * 1024  # 5 MiB
    UPLOAD_EXPIRY_HOURS: int = 24
    MAX_UPLOAD_MIB: int = 1024  # prod parity (system_setting MAX_UPLOAD_SIZE_MIB)

    # Slow-lane transcode + thermal guard (T2.3). Saturn is thermally bound: defer the
    # H.264 transcode while the SoC is above THERMAL_MAX_C. The default is Saturn's real CPU
    # sensor (k10temp) — `/sys/class/thermal/thermal_zone*` does NOT exist on this Synology, so
    # the old default silently read nothing and the guard failed OPEN (the 80 °C near-miss).
    # On dev/CI this path is absent → read returns None → guard is a no-op (correct for dev).
    THERMAL_ZONE: str = "/sys/class/hwmon/hwmon0/temp1_input"
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
