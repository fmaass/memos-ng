from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# DEV-ONLY default for SESSION_SECRET. It is the Fernet key encrypting BFF tokens at rest AND the
# fallback HMAC key for signed media URLs — so running production on this published value would make
# session tokens decryptable and media signatures forgeable. `validate_runtime_secrets()` refuses to
# start in production with this value. Keep the field default referencing this constant so the guard
# compares against the exact same string.
_DEV_SESSION_SECRET = "TzWJWb-LwdXWdsb92xjiY7yqMaW1lWZJPDNZSg-qqd8="


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Runtime
    ENV: str = "development"
    # Which installation this process is (trip pass P2). `home` is the household app, the default.
    # `ingest` is the guest door on the public VPS and `ingest-home` the interface home pulls from
    # there; both serve their own app (app/ingest) and never import a member router.
    APP_ROLE: Literal["home", "ingest", "ingest-home"] = "home"

    # Database (async + sync DSN for Alembic). Provided by the stack .env in prod.
    DATABASE_URL: str = "postgresql+asyncpg://memosng:memosng@localhost:5433/memosng"
    DATABASE_URL_SYNC: str = "postgresql+psycopg2://memosng:memosng@localhost:5433/memosng"

    # Storage — bind-mounted at /app/data in the container (layout identical to old memos).
    DATA_ROOT: str = "./data"

    # Where the self-hosted basemap lives: the PMTiles archives plus the fonts and sprites the
    # style needs. Empty means "<DATA_ROOT>/assets/tiles", i.e. the fast derived-asset volume
    # rather than the originals share — a map client reads an archive by byte range, so this
    # directory is hit far more often than any single media file. Resolved per call
    # (services/asset_paths.tiles_dir) so an override of DATA_ROOT moves the tiles with it.
    TILES_DIR: str = ""

    # DB connection pool (prod only — dev/test use NullPool so per-test event loops are safe).
    # memos-ng SHARES Postgres17 with prod; cap total connections (pool_size + max_overflow) so a
    # request burst can't exhaust the shared slots (TooManyConnectionsError starves prod too).
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 5
    DB_POOL_RECYCLE: int = 1800
    # Explicit, short pool checkout timeout: fail loud at 10s, not the 30s default that lets a burst
    # queue silently and hang the feed. Exhaustion is a hold-time bug to fix, not a wait to tune.
    DB_POOL_TIMEOUT: int = 10

    # Upload bodies streamed at once, per user and in total, before a PATCH to /api/uploads/{uid}
    # is answered 429 with this Retry-After. A body holds its request for as long as the client
    # takes to send it, so a burst of slow uploads is the one load that answering fast cannot
    # shed. The counters live in process memory, per uvicorn worker: one worker today, and a
    # second worker would double the effective caps.
    UPLOAD_BODIES_PER_USER: int = 3
    UPLOAD_BODIES_PER_PROCESS: int = 6
    UPLOAD_RETRY_AFTER_SECONDS: int = 2

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

    # Media worker thermal/concurrency guards (the worker host is thermally bound — see plan §3).
    FFMPEG_THREADS: int = 1

    # The worker drains its queue with two in-process consumers, one per claim CLASS: `transcode`
    # (a slow-lane derive job whose attachment is a video) and `light` (everything else). One slot
    # each, so a long transcode can no longer park every photo behind it. Validated once at startup
    # by worker.validate_worker_slots().
    WORKER_TRANSCODE_SLOTS: int = 1
    WORKER_LIGHT_SLOTS: int = 1
    # The CPU the worker container actually has. The validator checks the configured consumers fit
    # inside it; it is a budget the worker refuses to exceed, not a limit it can impose on ffmpeg.
    WORKER_CPU_BUDGET: int = 8
    # What ONE transcode really costs, MEASURED rather than nominal. Measured 2026-09-27 on jupiter
    # (a 29 s 1080p120 h264 clip, two runs each): with FFMPEG_THREADS=1 bounding only the encoder,
    # the decoder stayed on ffmpeg's auto thread count and the transcode ran at a mean 3.4 and a
    # peak 3.8 cores — over this cost. slow_ffmpeg_cmd therefore passes `-threads` before `-i` as
    # well; with the decoder bounded too the same clip ran at a mean 2.0 and a peak 2.2 cores, so
    # 3 is a ceiling again, bought with about 1.5x the wall-clock (16 s -> 24 s). Bounding the
    # filter threads as well made no measurable difference, so they stay on auto.
    # Deliberately NOT `FFMPEG_THREADS`: even at one decoder and one encoder thread the transcode
    # took 2.2 cores, so `slots x FFMPEG_THREADS` would be a thread count that bounds nothing;
    # this product is a CPU BUDGET. The validator refuses a cost below FFMPEG_THREADS, which is
    # what stops the arithmetic being decorative.
    WORKER_TRANSCODE_CPU_COST: int = 3
    # INACTIVITY bounds, per consumer, read by the supervisor before it stamps the heartbeat: a
    # consumer suspended forever inside an awaited operation is neither finished nor erroring, so
    # bounding death alone would let the supervisor keep reporting a hung worker healthy.
    # Composed from numbers that exist in the source: SLOW_TRANSCODE_TIMEOUT_S (3600) +
    # KILL_GRACE_SECONDS (5) + a stated 300 s margin for the lock-and-publish tail that follows the
    # transcode under no worker-side deadline. It stays under MEDIA_CLAIM_STALE_SECONDS so a
    # container healed at this bound is recovered by the sweep that already exists.
    WORKER_TRANSCODE_STALL_SECONDS: int = 3905
    # A POLICY number, stated honestly as one: the light class has NO upper bound in the source —
    # only its sub-steps do (ffprobe 30 s, poster encode 60 s), while the Pillow/libheif decode and
    # encode are wrapped by no timeout at all. It is anchored on the two numbers that do exist: it
    # is half the 1800 s worker healthcheck bar, so the supervisor stops stamping with a full bar of
    # margin before autoheal fires, and it sits far above the only light-class cost this deployment
    # has measured: 23 HEIC derives in 14 s on 2026-09-18, i.e. about 0.6 s each ON AVERAGE. That
    # is a batch figure; no per-job maximum was recorded. No source-derived light bound is claimed.
    WORKER_LIGHT_STALL_SECONDS: int = 900

    # Which media lanes a worker drains. Default = both (single-worker dev/CI + the original
    # in-stack Saturn worker). The Merkur offload runs a second worker with WORKER_LANES=slow
    # (CPU-heavy H.264 transcode) while Saturn runs WORKER_LANES=fast (light thumbs/poster/dims),
    # so transcode load leaves the thermally-bound NAS entirely. See ADR-0006 (revisit).
    WORKER_LANES: str = "fast,slow"

    # Resumable upload (T2.1).
    UPLOAD_CHUNK_SIZE: int = 5 * 1024 * 1024  # 5 MiB
    UPLOAD_EXPIRY_HOURS: int = 24
    MAX_UPLOAD_MIB: int = 1024  # prod parity (system_setting MAX_UPLOAD_SIZE_MIB)

    # Trip passes (trip pass P1): the guest door's public origin, which the pass URL and its QR
    # code name. Guests never reach this host; the door is a separate installation.
    TRIP_PASS_BASE_URL: str = "https://trip.discomarder.live"

    # The ingest installation (trip pass P2; only read by the two ingest roles). The service token
    # authenticates home on the `ingest-home` interface: at least 64 random bytes, generated with
    # `python -c "import secrets; print(secrets.token_urlsafe(64))"` (86 characters) and kept in the
    # stack's secret env, never in a file of this repo.
    INGEST_SERVICE_TOKEN: str = Field(default="", repr=False)
    INGEST_DATA_ROOT: str = "/srv/trip-ingest"
    # Refuse new guest upload sessions (507) once the volume would keep less than this free.
    INGEST_MIN_FREE_BYTES: int = 20 * 1024**3
    # Every guest PATCH body is bounded, a resumed tail included (plan decision 8: the Cloudflare
    # request-body limit is taken as applying to the tunnel).
    INGEST_PATCH_MAX_BYTES: int = 64 * 1024 * 1024
    INGEST_RETENTION_DAYS: int = 7
    INGEST_PURGE_INTERVAL_S: int = 3600
    INGEST_REDEEM_PER_MINUTE: int = 5
    INGEST_REFUSALS_PER_HOUR: int = 50
    # `CF-Connecting-IP` names the client only when cloudflared is the one peer that can reach the
    # process, which is the `ingest` deploy and no other; startup refuses it in any other role.
    INGEST_TRUST_CF_CONNECTING_IP: bool = False
    INGEST_IP_HASH_SALT: str = Field(default="", repr=False)
    # Home's puller (trip pass P3a): where home reaches the ingest host's home interface (through
    # its port forward) and the FILE holding the service token — a file, so the token never sits in
    # home's environment, and a distinct name, because home refuses `INGEST_SERVICE_TOKEN` itself.
    # The puller is off while either is empty.
    TRIP_INGEST_URL: str = ""
    TRIP_INGEST_TOKEN_FILE: str = Field(default="", repr=False)
    TRIP_INGEST_TIMEOUT_S: float = 30.0
    GUEST_BODIES_PER_DEVICE: int = 2
    GUEST_BODIES_PER_PROCESS: int = 12

    # Slow-lane transcode + thermal guard (T2.3): defer the H.264 transcode while the CPU package
    # is above THERMAL_MAX_C.
    #
    # THERMAL_ZONE empty = resolve the sensor BY NAME (media.resolve_thermal_sensor: the coretemp
    # chip's `Package id 0` input). Set it to a path and that path wins, unresolved — which is how
    # a host whose sensor cannot be found by name is configured, Saturn's k10temp among them
    # (`/sys/class/hwmon/hwmon0/temp1_input` there, and it publishes no temp*_label at all, so
    # name resolution could only pick it by index — the thing this default exists to stop).
    # The override does not escape the guard's plausibility bound: a reading outside 0 < °C <= 150
    # is treated as a failed measurement (UNKNOWN), so a sensor whose legitimate working range
    # leaves that window cannot be used here.
    #
    # THE HOST THESE NUMBERS DESCRIBE IS JUPITER — an i9-13900H, and the machine every transcode
    # has run on since the worker moved off Saturn on 2026-07-15. Measured there 2026-09-18 on the
    # package sensor, one real transcode of a synthetic 1080p clip under the worker's own cpus: 8
    # limit: 81 °C peak with no transcode running at all (ordinary household load), 85 °C peak at
    # one encoder thread, mean 87.5 °C and peak 92 °C at eight, against a critical limit of 100 °C.
    # A package under a transcode is simply a hot package; 90 °C is the owner's threshold for this
    # host (2026-09-18), chosen with those numbers in hand.
    #
    # The 70 °C that stood here belonged to the PREVIOUS host: Saturn, a Synology that shuts down
    # fast when hot. It was never measured against a CPU package and carrying it over would stall
    # every media job on jupiter, where an idle-ish package already crosses it.
    THERMAL_ZONE: str = ""
    THERMAL_MAX_C: int = 90
    MEDIA_MAX_ATTEMPTS: int = 3
    # Wall-clock ceiling for ONE slow-lane transcode (MEMNG-38). ffmpeg can wedge — a truncated
    # stream, a stalled read on the bind-mounted share — and unbounded it holds the worker's only
    # slot forever while the job sits `processing` and never fails visibly. The fast lane already
    # bounds its work (ffprobe 30s, poster 60s); a full H.264 transcode is orders of magnitude more,
    # so this is scaled, not copied: at FFMPEG_THREADS=1 / `-preset veryfast` a 1080p encode runs at
    # roughly real-time on this hardware, so an hour covers a ~30-60 min household clip with
    # headroom while still catching a wedged process within the hour instead of never. Keep it
    # hours-not-days: a genuinely stuck job burns up to MEDIA_MAX_ATTEMPTS x this before it goes
    # terminal. Raise it (env) if a legitimately long import starts timing out.
    SLOW_TRANSCODE_TIMEOUT_S: int = 3600
    # Stale-claim horizon for media_jobs (MEMNG-90). run_once commits the claim (`state=processing`)
    # before the transcode so no DB transaction is held across it; the price is that a worker that
    # dies mid-job leaves a durable `processing` row that claim_one (queued-only) never re-claims.
    # recover_stale_media_claims re-queues a `processing` row whose updated_at is older than this.
    # INVARIANT: it MUST exceed SLOW_TRANSCODE_TIMEOUT_S — a legitimately running transcode keeps
    # updated_at pinned at claim time for its whole duration (progress lives in a separate table),
    # so a shorter horizon would re-queue a live job and double-transcode it. Default = 2 h, a full
    # hour of headroom past the 1 h transcode ceiling.
    MEDIA_CLAIM_STALE_SECONDS: int = 7200
    # Worker healthcheck honesty (MEMNG-90): the loop stamps a liveness file every pass, which
    # stayed green even while every drain failed. After this many CONSECUTIVE run_media_drain()
    # results (an exception escaping run_once — a DB outage, not an ordinary job failure, which is
    # handled and returns processed/deferred), the loop stops stamping so the Docker healthcheck can
    # trip. Reset on the first non-error drain. At POLL_SECONDS backoff this is ~1 min of sustained
    # hard failure before the container is reported unhealthy.
    WORKER_UNHEALTHY_ERROR_STREAK: int = 6
    # Worker schema gate (MEMNG-86): the worker polls alembic_version until the live schema reaches
    # the head its image was built with before it drains anything, so a deploy that recreates app +
    # worker together can't run the worker's recovery sweep against a not-yet-migrated schema. Only
    # the app runs alembic (docker-entrypoint.sh), so there is one migrator and no double-run.
    # Budget generous enough for a real migration; on expiry the worker exits (Docker restarts it)
    # rather than draining a stale schema.
    WORKER_SCHEMA_GATE_TIMEOUT_S: int = 300
    WORKER_SCHEMA_GATE_INTERVAL_S: float = 1.0

    # Rolling BACK across an expand-only migration. The previous image cannot start normally on a
    # database at a newer revision: the app entrypoint would run `alembic upgrade head` against a
    # graph that does not contain that revision ("Can't locate revision"), and the worker's schema
    # gate would wait for a head it will never see. With this set, the entrypoint skips the
    # migration and the gate accepts a head its own graph does not know — which, by construction,
    # can only be a NEWER one. It is never set in normal operation; it is set on the rollback
    # deploy and cleared on the roll-forward (RELEASING.md, "Rollback across a migration").
    ROLLBACK_MODE: bool = False

    # Web push (VAPID) — T2.5. Empty keys = push disabled (every send is a no-op), so the
    # pipeline runs unchanged without VAPID configured. Generate a keypair with `vapid --gen`
    # (py-vapid) and set both halves via the stack .env in prod.
    VAPID_PUBLIC_KEY: str = ""
    VAPID_PRIVATE_KEY: str = ""
    VAPID_SUBJECT: str = "mailto:admin@discomarder.live"

    # Aura frame email share. Empty username/password = feature disabled globally: no jobs are
    # enqueued and every send is a no-op (same empty-default-disables pattern as VAPID above).
    # These are secrets — set via the stack .env in prod and never log them. Default host/port
    # target iCloud SMTP (smtp.mail.me.com:587 STARTTLS), the proven Phase-0 send path.
    SMTP_HOST: str = "smtp.mail.me.com"
    SMTP_PORT: int = 587
    # repr=False keeps these credentials out of any repr(settings)/structlog dump. They stay plain
    # str (not SecretStr) so the empty-default truthiness test that disables the feature still works
    # (a SecretStr is always truthy, which would defeat the on/off switch).
    SMTP_USERNAME: str = Field(default="", repr=False)
    SMTP_PASSWORD: str = Field(default="", repr=False)

    # Aura frame album HTTP upload. The SEND step POSTs the rendered JPEG to the aura-frames album
    # API, which then owns delivery to the frame (replacing the direct SMTP send). Empty URL/token
    # OR a zero album id disables the feature globally: no jobs are enqueued and every send is a
    # no-op (same empty-default-disables pattern as SMTP/VAPID above). The token is a secret — set
    # via the stack .env in prod and never log it (repr=False keeps it out of any settings dump).
    FRAME_API_URL: str = ""
    FRAME_ALBUM_ID: int = 0
    FRAME_API_TOKEN: str = Field(default="", repr=False)

    # img-manager's stem lookup, asked by the `sibling` media job for a photo whose own file said
    # nothing about when or where (MEMNG-796). All three or none: an empty value disables the
    # feature, and `validate_runtime_secrets` refuses a partial set. The token is a PAT and a
    # secret (repr=False, never logged); it stays a plain str for the same truthiness reason as
    # SMTP_PASSWORD above.
    IMGMANAGER_URL: str = ""
    IMGMANAGER_TOKEN: str = Field(default="", repr=False)
    # A connect+read ceiling for one indexed lookup on the same host, not a tuned value.
    IMGMANAGER_TIMEOUT_S: float = 5.0
    # Comma-separated users.id values whose attachments may be enriched. The RAW library is ONE
    # member's data: without this fence another member could learn a donor photo's date and GPS
    # by uploading a file with a chosen name. Checked at enqueue AND again by the job.
    SIBLING_OWNER_IDS: str = ""
    # Stale-claim horizon (MEMNG-35): a job still `claimed` this many minutes after the tombstone
    # commit is assumed orphaned by a crashed/recreated worker and is re-queued (or failed at the
    # attempt ceiling). One render+upload takes seconds, so 15 minutes is far outside a healthy
    # send while still recovering the same deploy window — it must stay well above the slowest
    # real send (30s upload timeout) or a live job would be re-queued under itself.
    FRAME_CLAIM_STALE_MINUTES: int = 15

    # Daily digest media hosting (Dropbox). The digest's recipients are outside the household and
    # have no memos-ng account, so a mail never links back here: oversized media is uploaded to
    # Dropbox and linked from there. These three are the Dropbox APP credentials and are SECRETS —
    # set via the stack .env in prod, never logged (repr=False keeps them out of a settings dump).
    # Empty disables hosting globally, the same empty-default-disables switch SMTP/VAPID/FRAME use;
    # `GET /api/admin/digest/hosting` reports that state, and the pipeline degrades to an
    # inline-only mail rather than failing.
    DROPBOX_APP_KEY: str = Field(default="", repr=False)
    DROPBOX_APP_SECRET: str = Field(default="", repr=False)
    DROPBOX_REFRESH_TOKEN: str = Field(default="", repr=False)

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
    SESSION_SECRET: str = _DEV_SESSION_SECRET
    COOKIE_SECURE: bool = True
    SESSION_ABSOLUTE_DAYS: int = 90
    SESSION_ROLLING: bool = True
    # last_seen is informational only (expiry is absolute), so don't rewrite it on every request:
    # the front page's concurrent fan-out all hits ONE session row and the per-request UPDATE+COMMIT
    # lock-convoys (measured 60-90x slowdown). Refresh it at most once per this window per session.
    SESSION_ROLLING_THROTTLE_S: int = 600

    # Same reasoning for a PAT's last_used: it is informational (shown in the PAT list), never an
    # authorization input, so a bounded staleness window is fine. A satellite client fetching a
    # gallery fires many /file/ requests at once, all against the ONE PAT row — stamping per
    # request would serialise them on that row lock. Stamp at most once per this window per PAT.
    # Shorter than the session window (600s): a PAT is a background client whose "is this token
    # still in use?" answer wants finer granularity than a browser session's.
    PAT_LAST_USED_THROTTLE_S: int = 300

    # Browser-brokered native device login. The app never talks to Authelia itself (Decision 1/2):
    # it opens the ordinary web login in ASWebAuthenticationSession, and the BFF hands the result
    # back over an HTTPS callback the app owns via its associated-domains entitlement.
    NATIVE_CALLBACK_URL: str = "https://memos.discomarder.live/ios/auth/callback"
    # Seconds a handoff code stays exchangeable. Short by design — the code travels through a URL
    # and its only job is to survive the hop from the auth session back into the app.
    NATIVE_CODE_TTL_S: int = 120
    # Lifetime of the PAT a device login mints. Bounds the blast radius of a lost phone that
    # nobody revokes; matches the /api/pats upper bound (PatCreate.expires_in_days le=365).
    NATIVE_TOKEN_DAYS: int = 365
    # Seconds a step-up management grant stays usable (MEMNG-138 option 2). Long enough for the
    # round trip through Authelia plus the management session it authorises (list, create, revoke),
    # short enough that a stolen grant is worth about as much as a stolen live browser session —
    # which is precisely the blast radius this design accepts. A device PAT can never become one.
    MANAGE_GRANT_TTL_S: int = 600
    # Apple associated-domains identity. While APPLE_TEAM_ID is unset (no Developer Program
    # membership yet) the AASA route 404s, which is the correct answer for a domain that claims
    # no app. Settings are read ONCE at process start, so enabling this is an env change plus a
    # same-image `up -d --force-recreate` — never a bare env edit.
    APPLE_TEAM_ID: str | None = None
    IOS_BUNDLE_ID: str = "live.discomarder.memosng"
    # The App Clip the ingest door's association file names for `/p/*` (trip pass P2).
    IOS_CLIP_BUNDLE_ID: str = "live.discomarder.memosng.Clip"
    # Which AASA sections to emit. `webcredentials` is what the callback interception needs;
    # `applinks` is kept switchable because which one ASWebAuthenticationSession's HTTPS callback
    # actually requires is an OPEN question answered in Phase 4 against Apple's docs.
    AASA_KEYS: list[str] = ["webcredentials"]

    @property
    def is_production(self) -> bool:
        return self.ENV != "development"

    @property
    def sibling_owner_ids(self) -> frozenset[int]:
        """`SIBLING_OWNER_IDS` parsed; a non-integer entry raises ValueError."""
        return frozenset(int(part) for part in self.SIBLING_OWNER_IDS.split(",") if part.strip())

    @property
    def sibling_enabled(self) -> bool:
        # The PARSED set: ", " is a non-empty string naming nobody, and would enable a feature with
        # no member allowed to use it. A malformed value reads as off here; startup refuses it.
        try:
            owners = self.sibling_owner_ids
        except ValueError:
            return False
        return bool(self.IMGMANAGER_URL and self.IMGMANAGER_TOKEN and owners)

    def validate_runtime_secrets(self) -> None:
        """Refuse to start production on the published dev-default secret (forgeable media
        signatures + decryptable BFF tokens). Called from the app lifespan at startup — NOT at
        import — so pure imports and tests stay side-effect-free. A dedicated ASSET_URL_SECRET does
        not exempt this: SESSION_SECRET still encrypts session tokens."""
        if self.APP_ROLE != "home":
            self._validate_ingest_role()
            return
        if self.INGEST_SERVICE_TOKEN or self.INGEST_TRUST_CF_CONNECTING_IP:
            raise RuntimeError(
                "INGEST_SERVICE_TOKEN and INGEST_TRUST_CF_CONNECTING_IP belong to the ingest "
                "roles; the home role refuses them"
            )
        if self.is_production and self.SESSION_SECRET == _DEV_SESSION_SECRET:
            raise RuntimeError(
                "SESSION_SECRET is the built-in dev default in production — set a unique value "
                "(and optionally ASSET_URL_SECRET) via the stack .env. The default key makes BFF "
                "session tokens decryptable and media URL signatures forgeable."
            )
        try:
            owners = self.sibling_owner_ids
        except ValueError:
            raise RuntimeError(
                "SIBLING_OWNER_IDS must be comma-separated integer user ids"
            ) from None
        sibling = {
            "IMGMANAGER_URL": bool(self.IMGMANAGER_URL),
            "IMGMANAGER_TOKEN": bool(self.IMGMANAGER_TOKEN),
            # Parsed, not the raw string: ", " names no member and counts as absent.
            "SIBLING_OWNER_IDS": bool(owners),
        }
        # Half a configuration would enqueue jobs that can only ever fail, or silently never
        # enqueue at all — either way the owner believes the enrichment is running.
        if any(sibling.values()) and not all(sibling.values()):
            missing = ", ".join(name for name, present in sibling.items() if not present)
            raise RuntimeError(
                "IMGMANAGER_URL, IMGMANAGER_TOKEN and SIBLING_OWNER_IDS are set together or not "
                f"at all — missing: {missing}"
            )

    def _validate_ingest_role(self) -> None:
        """The public door holds nothing of the household: a member-side setting given to either
        ingest role is refused, whatever its value, so a copied home env cannot start there."""
        given = self.model_fields_set
        present = sorted(name for name in MEMBER_SETTINGS if name in given and getattr(self, name))
        if present:
            raise RuntimeError(
                f"APP_ROLE={self.APP_ROLE} refuses member-side settings: {', '.join(present)}"
            )
        if self.APP_ROLE == "ingest":
            if self.INGEST_SERVICE_TOKEN:
                raise RuntimeError("APP_ROLE=ingest refuses INGEST_SERVICE_TOKEN (ingest-home's)")
            return
        if len(self.INGEST_SERVICE_TOKEN) < INGEST_TOKEN_MIN_CHARS:
            raise RuntimeError(
                "APP_ROLE=ingest-home needs INGEST_SERVICE_TOKEN of at least 64 random bytes "
                f"({INGEST_TOKEN_MIN_CHARS} characters of secrets.token_urlsafe(64))"
            )
        if self.INGEST_TRUST_CF_CONNECTING_IP:
            raise RuntimeError("APP_ROLE=ingest-home refuses INGEST_TRUST_CF_CONNECTING_IP")


# Settings only the household installation may hold. An ingest role refuses each of them when it is
# set at all (`model_fields_set`), so a default that merely exists is not mistaken for one given.
MEMBER_SETTINGS = (
    "SESSION_SECRET",
    "OIDC_CLIENT_SECRET",
    "ASSET_URL_SECRET",
    "ASSET_URL_SECRET_PREV",
    "VAPID_PUBLIC_KEY",
    "VAPID_PRIVATE_KEY",
    "IMGMANAGER_URL",
    "IMGMANAGER_TOKEN",
    "SIBLING_OWNER_IDS",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "FRAME_API_URL",
    "FRAME_API_TOKEN",
    "DROPBOX_APP_KEY",
    "DROPBOX_APP_SECRET",
    "DROPBOX_REFRESH_TOKEN",
)
# len(secrets.token_urlsafe(64)): 64 bytes in unpadded base64url.
INGEST_TOKEN_MIN_CHARS = 86


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
