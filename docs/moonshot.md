# The Public Release Moonshot — "A server anyone can run, an app anyone can install"

**Horizon:** one month, Monday 2 November → Friday 27 November 2026, four weekly builds, one
repository flip, one App Store submission.
**Assumption:** unlimited engineering hands, one owner reviewing, one household still using the
product every day while it changes under them.
**Scope:** everything between "memos-ng is production for one family" and "memos-ng is a public
open-source project with a listed iOS app": the license, the name, the provenance statement, the
household coupling in code and config, the install story, the import story, the security story,
the docs, the demo, the App Store record and the launch.
**Status:** proposal. Nothing here is ticketed or decided. Where it reverses something the code or
the owner decided on purpose, the reversal is named in § 9 and the owner decides; everywhere else
the standing rule applies (take the recommended option, record it, keep delivering).

Written on 2026-10-31 against main `1f62970` (server, v2.11.0) and `290d72e5` (app, past
v2.4.0). It sits beside two earlier moonshots in the iOS repo: `docs/moonshot.md` (the upload
slice, "Post and walk away", decided 2026-10-27 and scheduled for the same four weeks) and
`docs/moonshot-household-memory.md` (the twelve-month product). This one does not add product
features; it turns the product into something strangers can install, and it makes the App Store
build the household already gets into one anyone can download.

---

## 0. How to read this

Three questions started this document, and § 1 answers them from the repositories as they are:

1. *Can we publish the server on GitHub — what is the license situation?* (§ 1.1–1.2)
2. *Would we get away with calling it a clean-room implementation of Memos? We actually did.*
   (§ 1.3: yes, and the exact wording that stays true.)
3. *What else is missing that we desperately need to go public?* (§ 1.4: the blocker list, by
   severity, with file and line for each.)

§ 2 states the moonshot in a sentence and § 3 the principles that do not bend. § 4 is the heart:
eight pillars, each with the scene a stranger lives, the mechanism on both sides, the hard parts,
and the acceptance probe on the real surface. § 5 lays the work on four weeks with named squads and
a dependency spine. § 6 is the scoreboard, § 7 what we refuse to do, § 8 risks with kill criteria,
§ 9 the owner's decision points. Appendices hold the licensing file set, the configuration
reference that must exist, the App Store Connect checklist and the tickets this absorbs.

"Unlimited dev resources" removes the throughput constraint and replaces it with two others: one
owner, and one month in which the household keeps using the product while its identity, its
authentication and its name change. So every pillar is cut so that the household's running
instance is never the test subject (a second instance is, § 4 Pillar 2), and every irreversible act
(the license, the name, the history, the flip, the submission) sits on a single day with a named
rehearsal before it.

---

## 1. Where we stand — the three questions, answered from the code

### 1.1 The license situation: there is none, and the README says so

- There is no `LICENSE`, `CONTRIBUTING`, `SECURITY.md`, `CODE_OF_CONDUCT` or `.github/` directory
  in either repository (`ls` in both roots; the iOS repo likewise has none).
- The server README closes with `## License — Private household project. Not licensed for
  redistribution.` (`README.md:1221-1223`). That is the honest state and it is also what has to
  change first, because a public repository without a license is "all rights reserved" by default:
  people may read it and nobody may run it.
- Upstream `usememos/memos` is MIT-licensed ("Copyright (c) 2025 Memos"). MIT permits reuse,
  including commercial reuse, with one obligation: keep the copyright and permission notice on any
  copied code. memos-ng copied none (§ 1.3), so no obligation is owed; a courtesy credit costs
  nothing and buys goodwill.
- The frontend dependency set is permissive throughout: maplibre-gl, pmtiles and
  `@protomaps/basemaps` are BSD-3-Clause; photoswipe 5, markdown-it, blurhash, `@noble/hashes` and
  `@tanstack/vue-query` are MIT; dompurify is dual MPL-2.0 / Apache-2.0 (take Apache-2.0); the
  PrimeTek packages (primevue, primeicons, `@primeuix/themes`) say `SEE LICENSE IN LICENSE.md` in
  their metadata and are MIT in their repositories, which means license scanners need a manual
  mapping for those three.
- The Python set is permissive too, with **one real finding**: the `pillow-heif` wheel is
  BSD-3-Clause itself but bundles libheif (LGPLv3), libde265 (LGPLv3) and **x265 (GPLv2)**, per
  the project's own `LICENSES_bundled.txt`. memos-ng only *decodes* HEIC (`register_heif_opener()`
  in `app/services/frame_share.py:29` and `app/services/media.py:48`; every `.save` in `app/`
  writes JPEG, `media.py:274-292`, `frame_share.py:162`; the Dockerfile notes only that the wheel
  bundles libheif, `Dockerfile:21-22`). The encoder is dead weight that drags a GPLv2 library into
  an image we would otherwise ship under a license of our choosing. The clean move is the
  maintainer's decode-only sibling `pi-heif` (libheif + libde265 only, LGPL) in the runtime
  dependencies. It is not a one-liner: `pillow_heif` is imported in 14 files (6 in `app/`, 6 in
  `tests/`, 2 in `scripts/`), and the *tests* encode HEIC fixtures (`pillow_heif.from_pillow` in
  `tests/camera_fixtures.py:78,142,301`, `.save(format="HEIF")` in three worker tests), so
  `pillow-heif` stays as a **dev** dependency for fixture generation and the runtime image loses it.
- `ffmpeg` is installed from Debian (`Dockerfile:23`). Debian's standard flavour is built with
  `--enable-gpl --enable-libx265`, so the binary is GPLv2+. memos-ng calls it as a subprocess,
  which the GPL FAQ treats as two separate programs; the application's own license is unaffected.
  What we owe as the *distributor of an image containing ffmpeg* is the notice and an offer of the
  corresponding source, which Debian's source packages satisfy: a `THIRD_PARTY_NOTICES.md` that
  names the Debian package version and how to obtain its source closes it. Immich ships
  jellyfin-ffmpeg (GPL-3.0) inside an AGPL project the same way.
- The Places basemap is Protomaps + OpenStreetMap, which is an ODbL Produced Work. The OSMF
  attribution guideline wants the credit to make the ODbL availability reachable, in practice a
  link from "OpenStreetMap" to `openstreetmap.org/copyright`. Today the attribution is a plain
  string, `'© OpenStreetMap contributors © Protomaps'` (`frontend/src/lib/places/style.ts:15`),
  rendered through MapLibre's `AttributionControl` (`PlacesMap.vue:215`). MapLibre accepts HTML
  there, so the fix is two anchors and one updated test (`style.spec.ts:148`).

### 1.2 Which license to choose, and why it differs per repository

The peer field is split: Memos and LibrePhotos are MIT; Immich, PhotoPrism and Ente are AGPL-3.0.
For a *server* that strangers will run and some will want to host for others, AGPL-3.0 is the
defensible and common choice: it keeps improvements in the open and it stops a hosted fork from
quietly diverging. Apache-2.0 or MIT would maximise adoption and minimise questions.

For the *iOS app* the choice has a hard constraint the server does not: Apple's App Store terms
have a long history of conflict with the GPL family (VLC was pulled in January 2011 over it), and
the projects that ship GPL iOS clients carry an explicit store exception (Nextcloud's
`COPYING.iOS`). An MIT or Apache-2.0 iOS repository has no such problem.

**Recommended (owner decision (a), § 9):** server `AGPL-3.0-only`; iOS app `MIT`; the OpenAPI
document and generated clients `MIT` so that anyone may build a client without AGPL questions;
documentation `CC BY 4.0`. Different licenses per repository are normal and the split above is
exactly what Immich-style projects end up with, minus the store exception we do not need.

### 1.3 "We did a clean-room implementation": yes, and here is the wording that stays true

Legally the question barely matters, because upstream is MIT: even a derivative work would owe
only a notice. It matters for *credibility*, and credibility is lost by overclaiming, so the claim
must be precise.

What the repositories show:

- **No upstream code was copied, and one upstream artifact *was* read.** A grep over `app/`,
  `frontend/src/`, `tests/`, `CLAUDE.md`, `README.md` and `docs/` for upstream markers (`usememos`,
  `\bproto\b`, `\.go:`, `\bgrpc\b`, `\bapiv1\b`, `Copyright`, `MIT License`, `clean.?room`) finds
  four hits, three of them the word "Memos"/"usememos" naming the thing being replaced
  (`README.md:3`, `docs/plans/PLAN_2026-07-03_feature-parity-roadmap.md:3,113`). The fourth is the
  one that matters and the grep nearly missed it (`\bproto\b` does not match "protos"): the
  feature-parity roadmap of 2026-07-03 records "a source-cited feature inventory of upstream memos
  v0.29.1 (usememos.com docs, the v0.29.1 git tag protos/package.json, local fixtures)"
  (`PLAN_2026-07-03_feature-parity-roadmap.md:37-38`). So upstream's **interface definitions**
  (the `.proto` files) and its `package.json` were read, once, to list what upstream *does*, not
  how it does it. No Go, no TypeScript, no copied identifiers or comments. The server is FastAPI +
  SQLAlchemy + Vue, built from a plan written on 2026-06-20 (`docs/plans/PLAN_2026-06-20_memos-ng.md`),
  364 commits later. Strictly, that is a clean room with one documented window: nobody who wrote
  code read upstream *implementation*; the API inventory came from protos, docs and a live instance.
- **What *was* observed from upstream, and must be disclosed:** (1) the *behaviour* of a live
  memos 0.29.1 instance: its API responses were captured as fixtures before the old stack was
  stopped (plan G1, `PLAN_2026-06-20_memos-ng.md:195`); (2) the *database schema* of the old
  instance, which the one-way importer reads directly (`app/scripts/migrate_from_memos.py:1-13`,
  "Reads the old `memos` DB READ-ONLY"). Observing a running program's outputs and its database
  layout is the textbook definition of a clean-room *specification*; it is not reading its source.
- **One piece of evidence is gone.** `fixtures/live-0.29.1/` is referenced by `CLAUDE.md:14` and
  `CLAUDE.md:136-139` as the compat ground truth, but the directory is absent from the working
  tree and has no commit in history (`git log --all -- fixtures/live-0.29.1` is empty). The
  compat layer it anchored is also tiny today: `openapi.json` lists exactly two `/api/v1` paths
  (`/api/v1/memos`, `/api/v1/memos/{memo_id}`) and one `/file/` path, out of 76 paths and 102
  operations. So the honest statement cannot say "API-compatible with Memos"; it says what is
  below.
- **The naming is the actual exposure, not the code.** "memos-ng" is derived from "Memos"; the App
  Store already carries "Moe Memos" (a client for upstream), "Memos Mobile" and "Memos". Apple
  guideline 4.1(c) forbids using another developer's product name in your app's name without
  approval, and 2.3.7 asks for a unique name. No registered trademark on "Memos" tied to upstream
  was found (USPTO; EUIPO/WIPO not searched), and usememos.com publishes only logo guidance, no
  trademark policy. The risk is not a lawsuit, it is an App Review rejection and a permanent
  "that's the memos fork" identity. **Recommended (owner decision (b)):** a new public name, chosen
  in week 1 by the procedure in Pillar 5; the GitHub repository can keep `memos-ng` as an alias
  (GitHub redirects renamed repositories).

The provenance statement that stays true, proposed as `PROVENANCE.md` at the repository root and
quoted in the README:

> This project is an independent, from-scratch implementation. No source code from
> [usememos/memos](https://github.com/usememos/memos) (MIT) or any other project was copied into
> it, and none of its implementation was consulted. It was designed as a replacement for a
> household's Memos 0.29.1 instance, so its feature set, data model and the two legacy routes
> under `/api/v1` were specified from the outside: upstream's public documentation and its
> Protocol Buffer interface definitions at the v0.29.1 tag (for a feature inventory), the running
> instance's HTTP responses, captured as fixtures in June 2026, and its database schema, which the
> one-way importer in `app/scripts/migrate_from_memos.py` reads. "Memos" is the name of that
> upstream project; this project is not affiliated with or endorsed by it.

### 1.4 What is missing to go public, by severity

Severity: **P0** blocks the repository flip or the App Store submission; **P1** makes the first
stranger fail; **P2** is embarrassing but survivable for a week.

| # | Sev | Gap | Where | Pillar |
|---|---|---|---|---|
| 1 | P0 | No license file; README says "not licensed for redistribution" | `README.md:1221-1223`, both repo roots | 4 |
| 2 | P0 | **Nobody but the household can log in.** OIDC endpoints are configured one by one with Authelia defaults on `auth.discomarder.live` (`OIDC_ISSUER`, `OIDC_AUTHORIZE_ENDPOINT`, `OIDC_TOKEN_ENDPOINT`, `OIDC_JWKS_URI`, `OIDC_USERINFO_ENDPOINT`, `OIDC_END_SESSION_ENDPOINT`); no `.well-known` discovery; no local username/password path; users exist only after a first OIDC login (or the importer, `migrate_from_memos.py:105`); the `admin` role is "member of the Authelia group `admins`" | `app/config.py:299-310`, `app/auth.py:106-127`, `app/auth.py:111`, `app/routers/auth.py:51,170` | 1 |
| 3 | P0 | Household defaults baked into `Settings`: `OIDC_REDIRECT_URI`/post-logout/`NATIVE_CALLBACK_URL` on `discomarder.live`, `TRIP_PASS_BASE_URL` on `trip.discomarder.live`, `VAPID_SUBJECT` = `mailto:admin@discomarder.live`, `SMTP_HOST` = `smtp.mail.me.com`, `IOS_BUNDLE_ID` = `live.discomarder.memosng`, `THERMAL_MAX_C` = 90 tuned to one host (`THERMAL_ZONE` already resolves the sensor by name, `:171`) | `app/config.py:309-310,334,128,243,249,355-357,180-192` | 1, 2 |
| 4 | P0 | Tracked file with family members' names and the production inventory | `memory/parity-scope.md` (tracked; it named the owner and the second household member and listed the household's few users and their memos) | 4 |
| 5 | P0 | The tracked `CLAUDE.md` is the household's operations manual: hostnames, LAN addresses, ports, the NFS paths, where the sudo password file lives on the NAS | `CLAUDE.md` (19.6 KB, tracked); same class in the iOS `CLAUDE.md` | 4 |
| 6 | P0 | No secret-scan of 364 + 409 commits of history; a 44-character dev `SESSION_SECRET` is in the tree by design (refused in production by `validate_runtime_secrets`) | `app/config.py:12,395-400` | 4 |
| 7 | P0 | GPLv2 x265 inside the `pillow-heif` wheel; no third-party notices; no ffmpeg source offer | `pyproject.toml` (`pillow-heif>=0.18,<2`), `Dockerfile:21-23` | 4 |
| 8 | P0 | iOS: the only server a new user can reach is ours. `serverAddress = "memos.discomarder.live"` is the default and the first screen has no server field (only the Advanced token sheet has one); the guest door is `https://trip.discomarder.live` | `SignInView.swift:20,127-128,189`, `GuestWire.swift:269-270,274` | 6 |
| 9 | P0 | iOS: App Review cannot reach a server. No demo server, no demo mode | `docs/session-reviews/AUDIT_2026-09-18_apple-guidelines.md` gap 3 (the audit itself is gitignored, so no outside reader can see it) | 5, 6 |
| 10 | P0 | iOS: no privacy-policy link inside the app (guideline 5.1.1 wants it in the metadata *and* in the app); the policy URL on the record is `https://memos.discomarder.live/privacy`, a household server | audit gap 4; `docs/TESTFLIGHT.md:114-120` | 6 |
| 11 | P0 | iOS: App Store name is `memosng`, display name `memosng`; collides with the "Memos" family of apps (4.1(c), 2.3.7) | `project.yml:199`; `docs/TESTFLIGHT.md:100` | 5, 6 |
| 12 | P1 | No self-host compose, no `.env.example`, no published image. `compose.dev.yaml` is a Postgres-only dev file; the image is built locally as `memos-ng:local`; the deploy stack lives in another repo | `compose.dev.yaml`, `RELEASING.md`, `scripts/build-image.sh` | 2 |
| 13 | P1 | README is 1,222 lines, of which 1,030 are the household changelog; the deploy section is written for jupiter and Saturn | `README.md:123-170,191-1220` | 5 |
| 14 | P1 | Hard-coded `Europe/Zurich` for library grouping; UI English-only with no i18n layer; `<html lang="en">` | `frontend/src/lib/librarySections.ts:12`, `frontend/index.html:2`, `frontend/package.json` | 5 |
| 15 | P1 | OSM attribution is text without the required link | `frontend/src/lib/places/style.ts:15` | 4 |
| 16 | P1 | API description says "Authelia login"; `household` appears 72 times in `app/` and 25 in `frontend/src` (the generated schema calls it a "single-household instance") | `app/main.py:191`, `frontend/src/api/schema.d.ts:1153` | 1, 5 |
| 17 | P1 | No CI: the local gate is the only gate (MEMNG-1 cancelled). A public repository with contributors cannot run a gate on the maintainer's machine | `CLAUDE.md` "The LOCAL gate is the gate — there is NO CI" | 4, § 9 (d) |
| 18 | P1 | Household-only integrations are first-class settings with no "off" story in docs: Aura frame share (`FRAME_*`), Dropbox (`DROPBOX_*`), img-manager sibling lookup (`IMGMANAGER_*`, `SIBLING_OWNER_IDS`), daily digest SMTP | `app/config.py` (101 fields, 0 required) | 2 |
| 19 | P1 | iOS: no App Store lane, no `metadata/`, no screenshots pipeline, no listing text; six fastlane lanes end at TestFlight | `fastlane/Fastfile:204-361` | 6 |
| 20 | P1 | iOS: associated domains for `memos.discomarder.live` and `trip.discomarder.live` baked into entitlements (the memos one is inert: the AASA is not publicly fetchable) | `project.yml:113-138` | 6 |
| 21 | P1 | iOS: no license, 13 SPM packages with no recorded licenses, no NOTICE | `Package.resolved` | 4 |
| 22 | P2 | Storage is filesystem-only under `DATA_ROOT`; fine for self-hosters, but documented nowhere for them | `app/services/storage.py:129-154`, `asset_paths.py:30-35` | 2 |
| 23 | P2 | Legacy compat lanes (`/api/v1/*`, `/file/*`) exist for "unmigrated satellites" nobody outside has | `app/routers/compat.py` | 7 |
| 24 | P2 | iOS: English-only UI, no String Catalog (no `.xcstrings`/`.lproj` in the tree); Reduce Motion honoured once; the camera close button is 36 pt | audit gaps 6–7; `CameraView.swift:57` | 6, § 9 (e) |
| 25 | P2 | iOS: stale README (describes PAT sign-in as primary; the app's primary path is the browser login) | `README.md` "Signing in" vs `SignInView.swift:124-141` | 5 |

Twenty-five rows, eleven of them P0. Eight of the eleven are a week of work for one squad each;
the other three (the name, the license, the history) are decisions that take an afternoon once the
owner has made them.

---

## 2. The moonshot in one sentence

**A stranger with a Linux box and an iPhone reads one page, runs one command, opens one app from
the App Store, points it at their own address, and has their family's photos and notes on their
own hardware within ten minutes, with the licenses, the provenance and the security posture of a
project that intends to be around in ten years.**

The test of the sentence is a scene: a person in another country finds the repository, reads the
README, runs `docker compose up -d` on a NAS, opens `https://photos.theirhouse.example`, creates
the first account in the setup wizard, invites their partner by link, imports their Memos
instance with one command, installs the app from the App Store, types the address, signs in with
a passkey, and shares the first picture from the lock screen, all before the kettle boils. Not one
line of our household's configuration is in their path.

---

## 3. Principles that do not bend

Inherited from the two earlier moonshots and `CLAUDE.md`; the public release adds five.

1. **Originals are sacred.** Nothing in the public release changes the archive model
   (ADR-0008).
2. **The contract is the product.** Every new server capability is an OpenAPI path first,
   generated into the client second. The public API is the one in `openapi.json`; there is no
   second contract.
3. **Private by architecture.** No telemetry, no analytics SDK, no crash reporter, no
   phone-home, in either artifact, ever. The App Privacy answer stays "Data Not Collected" because
   it stays true.
4. **The gate is the only build authority.** Every claim below ends in a bare gate exit 0,
   cited by code.
5. **The main bar is frozen** (ADR-0008). The App Store build has the same bar the household has.

The public-release five:

6. **One household per server.** The household moonshot says "no multi-tenancy, forever" and the
   public release agrees: a self-hosted instance serves one family. Multiple *users*, one
   *household*. We do not become a SaaS, and we do not build the plumbing for one.
7. **The household's instance is never the test subject.** Every change to identity, config
   defaults or storage layout is proven on a second instance (the demo instance, Pillar 5) under
   a stranger's configuration before it is deployed to `memos.discomarder.live`. The family keeps
   logging in with Authelia all month, through the same generic code path strangers use.
8. **Irreversible acts have a day and a rehearsal.** The license commit, the rename, the history
   publication, the visibility flip and the App Store submission each sit on one named day with a
   dress rehearsal before it (the external TestFlight review is the rehearsal for App Review; the
   mirror repository is the rehearsal for the flip).
9. **Nothing household-specific ships as a default.** A default is for strangers. Our values
   live in our stack's `.env`, nowhere in the tree. The test is mechanical: a grep for our
   hostnames over the public tree returns zero, with a positive control.
10. **Everything a stranger needs is in the repository.** Install, upgrade, back up, restore,
    import, export, configure, report a vulnerability, contribute, all of it reachable from the
    README within two clicks. No BookStack page, no ticket key, no session review is required
    reading for anyone outside the house.

---

## 4. The eight pillars

Each pillar: **Scene** (what a stranger lives), **Mechanism** (server · iOS), **Hard parts**,
**Acceptance probe** (the real surface).

### Pillar 1 — Anyone's identity: generic OIDC, local accounts, passkeys, a setup wizard

**Scene.** A new instance greets its first visitor with a setup page: name the household, create
the first account with a passkey or a password, done. The admin invites family by link; each
invitee creates their own passkey on their own phone. A household that already runs an identity
provider (Authentik, Keycloak, Authelia, Pocket ID, Zitadel) pastes one issuer URL and one client
secret, and the login button becomes theirs.

**Mechanism.**

*Server.* Three login paths behind one session model (the BFF cookie session, PATs, grants and native device
login all stay exactly as they are; `app/auth.py:128,145,442,507`, `app/routers/auth.py:331-617`):

- **OIDC by discovery.** `OIDC_ISSUER` alone is enough: the six endpoint fields
  (`app/config.py:299-310`) become overrides, and startup fetches `/.well-known/openid-configuration`
  once, caches it, and refuses a provider whose discovery disagrees with any override. The Authelia
  defaults are deleted, not moved. Role mapping becomes a setting (`OIDC_ADMIN_CLAIM`,
  `OIDC_ADMIN_VALUE`) instead of the hard-coded group name at `app/auth.py:111`, and the first
  OIDC user on an empty instance becomes admin.
- **Local accounts.** A `credentials` table (argon2id hashes, per-user salt, a `password_changed_at`
  that invalidates older sessions), a login form on the same `/login` route that today only
  redirects, rate-limited per account and per source address with the same limiter the guest
  door already uses (`INGEST_REDEEM_PER_MINUTE` is the shape). Password reset is an admin action
  on a one-household server, not an email flow.
- **Passkeys.** WebAuthn registration and assertion (`py_webauthn`), resident keys so the login
  page can offer "Sign in with a passkey" without a username, one or more passkeys per user,
  recovery through the admin. This is the default the setup wizard offers first.
- **Setup wizard.** On an instance with zero users, every route redirects to `/setup`, which
  creates the household and the first admin and writes the instance's `SESSION_SECRET` and
  `ASSET_URL_SECRET` into the database-backed settings table (not the env file), so a stranger
  does not have to generate Fernet keys to get started. The dev secret at `app/config.py:12`
  becomes a test-only constant.
- **Invitations.** Admin mints an invite link (the trip-pass code already has a QR, an expiry and
  a redeem path; `app/services/passes.py`); the invitee lands on an account-creation page.

*iOS.* The native login flow is already server-URL-driven and PKCE-based
(`Packages/MemosAPI/Sources/MemosAPI/NativeLogin.swift:11,82-85,139`); it opens the server's
`/login` in `SFSafariViewController`, so every server-side login path (OIDC button, password form,
passkey prompt) works in the app the day the server ships it. The app adds nothing but the server
field (Pillar 6).

**Hard parts.** Making sure the Authelia household keeps working while the defaults disappear
(Principle 7: the demo instance proves generic OIDC against two providers before the household
instance moves to discovery). WebAuthn relying-party ID must equal the instance's public host, so
the wizard must know the public origin; the existing `OIDC_REDIRECT_URI` logic already assumes one.
Passkeys inside `SFSafariViewController` work (it is Safari); inside the share extension there is
no login, by design.

**Acceptance probe.** On a fresh VM: `docker compose up`, `/setup` creates an admin with a
passkey on an iPhone, an invite link creates a second user with a password, both sign in on the
web and in the app; separately, the same image against a Keycloak and a Pocket ID issuer, with
only `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` set, logs in and maps admin by claim.
The household instance, moved to discovery, logs in with Authelia unchanged (real browser, real
phone).

### Pillar 2 — Up in five minutes: one compose file, signed images, a configuration reference, backup and restore

**Scene.** The README's install section is twelve lines: copy `compose.yaml` and `.env.example`,
set two values, `docker compose up -d`, open the address. Upgrades are `docker compose pull && up
-d` and the migrations run themselves. `memosng backup` writes one archive; `memosng restore`
reads it. The configuration reference lists every setting with its default, generated from the
code, so it is never stale.

**Mechanism.**

*Server.*

- **`compose.yaml`** at the repository root with three services (`app`, `worker`, `db` on
  `postgres:17`), one named volume for Postgres, two bind mounts (`DATA_ROOT` for originals and
  derived tiers, optional `TILES_DIR` for the basemap), healthchecks on `/health`, the worker on
  the same image with `command: worker` as today. `.env.example` carries exactly the values a
  stranger must set (`PUBLIC_URL`, `POSTGRES_PASSWORD`) and the ones they may (`MAX_UPLOAD_MIB`,
  worker slots). The in-repo compose is the *reference*; the household stack keeps its own in
  `/opt/jupiter-stacks` and tracks the reference.
- **Images on GHCR**, multi-arch (`linux/amd64`, `linux/arm64` for NAS boxes and Raspberry Pi 5),
  built by GitHub Actions from a tag, signed keyless with cosign, with an SPDX SBOM and a build
  attestation (`actions/attest`), tagged `vX.Y.Z`, `X.Y`, `latest` and by digest. `scripts/
  build-image.sh` stays the local path; CI mirrors it.
- **Neutral defaults.** Every household default in § 1.4 rows 3 and 18 is deleted or neutralised:
  `VAPID_SUBJECT` derives from `PUBLIC_URL`; `SMTP_HOST` is empty (digest off until set);
  `TRIP_PASS_BASE_URL` derives from `PUBLIC_URL`; `IOS_BUNDLE_ID` stays a setting with the real
  bundle id as default because that id *is* the public app's id (§ 9 (g)); the thermal guard
  becomes opt-in behind a new `THERMAL_GUARD=0|1` (today an empty `THERMAL_ZONE` means "find the
  sensor by name", `app/config.py:171`, so the empty value cannot be reused to mean off) and is
  documented as "for hosts that throttle".
- **Integrations become a documented tier.** Aura frame share, Dropbox, img-manager siblings and
  the SMTP digest are grouped under "Integrations (optional)" in the reference, each with the
  sentence "unset = feature hidden", and each is verified hidden in the UI when unset.
- **Configuration reference generated from `Settings`.** A script walks the pydantic model
  (101 fields today, `app/config.py:15-441`) and emits `docs/configuration.md` with name, type,
  default, description and the group; the gate fails on drift, the same way `openapi.json` is
  gated.
- **`memosng` CLI** (a `python -m app.cli` entry in the image): `backup` (pg_dump + the
  `DATA_ROOT` manifest with sha256 per original, streamed to a tar), `restore`, `import memos`
  (Pillar 3), `check` (config + DB + storage health), `user create|promote|reset-password`. The
  CLI is also the operator's door when the web is unreachable.
- **Upgrade contract.** Migrations are expand/contract and run on app start (today's behaviour,
  `alembic upgrade head` in `docker-entrypoint.sh:74`; the worker only waits for the schema,
  `app/migrations.py`); `ROLLBACK_MODE` is documented; every release note states "migration: yes/no"
  and "rollback: image N-1 safe until release N+1".

**Hard parts.** The worker's slot validation and CPU budget (`validate_worker_slots`) are tuned for
an eight-core host; the compose must ship a conservative default (`WORKER_CPU_BUDGET=2`, one slot
each) that a two-core NAS survives, and the docs must say how to raise it. First-run on arm64 has
never been built; `pillow-heif`/`pi-heif` and the Node stage need arm64 wheels (both publish them).
Backups must be consistent across Postgres and the filesystem: the CLI takes the dump first, then
walks files, and records the dump's transaction snapshot in the manifest so a restore knows which
uploads were in flight.

**Acceptance probe.** A clean Ubuntu 24.04 VM (x86) and a Raspberry Pi 5 (arm64), both strangers'
configuration, both from the README alone: first photo visible within ten minutes of `git clone`,
timed. `cosign verify` on the pulled image exits 0. `memosng backup` on the demo instance, wipe,
`memosng restore`, every original's sha256 matches the manifest. `docs/configuration.md` is
regenerated by the gate and `git diff --exit-code` is 0.

### Pillar 3 — Bring your library: importers from Memos, Google Takeout and Apple Photos; the way out

**Scene.** A Memos user runs `memosng import memos --dsn … --files …` and gets their memos,
attachments, tags and reactions, with originals copied and derivatives queued, re-runnable without
duplicates. Someone leaving Google runs `memosng import takeout ./Takeout` and keeps their
captions, albums and the dates Google stored in the sidecar JSON. Someone exporting Apple Photos
drops the export folder in and keeps Live Photo pairs and albums. And anyone can leave: `memosng
export` writes a folder tree a human can read, with a JSON per memo, and that export re-imports
losslessly into a fresh instance.

**Mechanism.**

*Server.*

- **Memos importer, generalised.** `app/scripts/migrate_from_memos.py` is the household's
  one-way ETL from 0.28.1 (`:1-13`). It becomes `memosng import memos`, grows a schema probe so it
  can read the 0.2x schemas *and* the current CalVer releases (upstream is at 26.10.0), reads the
  files directly instead of expecting a Saturn `cp -p`, and hashes on the way in so re-runs are
  no-ops by content, not only by uid. Tested against seeded dicts (today) *and* against a real
  upstream container populated by its own API in CI.
- **Google Takeout importer.** Walk `Takeout/Google Photos/`, pair media with its `.json` sidecar
  (title, description, `photoTakenTime`, geo, favourite), treat album folders as collections,
  recover the capture date from the sidecar when EXIF has none (the household's own 624 undated
  items had the same problem; the `capture` service already has the date-precedence rule
  `library-grouping-date-is-capture-then-memo-date`).
- **Apple Photos export importer.** Read an "Export Unmodified Originals" folder plus its
  IPTC/XMP sidecars; pair `IMG_1234.HEIC` with `IMG_1234.MOV` as a Live Photo (new code: `app/services/sibling.py`
  looks a RAW's stem up in img-manager and explicitly says "a stem is not an identity", `:19`, so
  local stem pairing is its own, stricter rule keyed on the Apple content identifier); albums from the
  export's folder structure.
- **Folder import / watch.** `memosng import folder ./pictures` and an optional watched directory
  for people who sync from a camera.
- **Export, round-trippable.** The streaming export that exists (`app/routers/export.py`,
  `app/services/export.py`) gains a documented on-disk schema (`EXPORT_FORMAT.md`: one directory
  per memo, `memo.json`, originals by their stored name, `collections.json`, `users.json`) and
  an importer for its own format, so "export, wipe, import" is a gate test.
- **Dedupe on import by sha256** across the whole library, which is the upload moonshot's
  decision (e) (hard link + reference-counted reclaim). Import reuses that, so one squad builds
  it.

**Hard parts.** Takeout sidecars are inconsistent (truncated names, `(1)` suffixes, edited
copies); the importer must be lenient and report what it could not pair rather than fail. Upstream
Memos has changed its schema across versions; the probe must refuse an unknown schema with a clear
message, not guess. Import must stream: a 200 GB Takeout cannot be staged twice.

**Acceptance probe.** A real Memos 26.x container populated with 1,000 memos and 3,000 attachments
via its API imports with zero duplicates on a second run (row counts equal, sha256 set equal). A
50 GB Google Takeout of CC0 photos imports with every sidecar date honoured (sampled 100,
0 mismatches). `memosng export` then `memosng import export` into a fresh instance: memo count,
attachment sha256 set, collection membership and tags identical.

### Pillar 4 — Trust by design: the license set, the provenance, the history scrub, a security programme, supply chain

**Scene.** The repository's front page shows a license badge, a Scorecard badge, a "Report a
vulnerability" button that opens a private advisory, a `SECURITY.md` with a response-time promise,
signed releases, and a `PROVENANCE.md` that says plainly how the project relates to Memos. A
security researcher finds everything they need to disclose responsibly in under a minute.

**Mechanism.**

*Both repositories.*

- **License file set** (Appendix A): `LICENSE` (AGPL-3.0-only server, MIT iOS), `NOTICE`,
  `THIRD_PARTY_NOTICES.md` generated by `pip-licenses` + `license-checker` + a hand-written
  Debian section for ffmpeg (package version, `apt source` instructions), REUSE-style
  `SPDX-License-Identifier` headers via a `REUSE.toml` so `reuse lint` passes, and `PROVENANCE.md`
  (§ 1.3). `pillow-heif` → `pi-heif`.
- **Household scrub.** Delete `memory/parity-scope.md` from the tree; move the operations manual
  out of the tracked `CLAUDE.md` into a private overlay (the archimedes repo already carries
  per-project memory; the public `CLAUDE.md`/`AGENTS.md` keeps only what a contributor's agent
  needs: the gate, the conventions, the test rules); rewrite the README deploy section for
  strangers; neutralise every default (Pillar 2); replace `household` in user-visible strings and
  the schema description (`app/main.py:191`, the `single-household instance` text that lands in
  `schema.d.ts:1153`); make the library time zone a per-user setting with the browser zone as
  default (`librarySections.ts:12`).
- **History decision and scan** (owner decision (c)). Run `gitleaks` and `trufflehog` over all
  refs of both repositories; triage every hit by shape and length, never by printing; any real
  credential is rotated *first* (the consumer proves acceptance of the new value) and only then
  rewritten out with `git filter-repo`. Hostnames, LAN addresses and ticket keys are not secrets
  and stay. Recommended: publish the full history. 364 commits of a clean-room build are the
  provenance.
- **Security programme.** `SECURITY.md` (supported versions = latest minor; 72-hour
  acknowledgement; coordinated disclosure; GitHub private vulnerability reporting enabled),
  `/.well-known/security.txt` served by the app, a written threat model (`docs/THREAT_MODEL.md`:
  assets, actors, the signed-URL bearer model from ADR-0020, the guest door, the capture grant,
  what the share extension cannot do), `pip-audit` + `npm audit` + Trivy on the image in CI with
  HIGH/CRITICAL failing the build, Renovate already present (`renovate.json`), OpenSSF Scorecard
  with a target of 8.0 (branch protection, signed releases, token permissions, pinned actions,
  SAST), and one adversarial review of the public attack surface by a second model family before
  the flip (the Codex gate the house already runs, aimed at `app/routers/*` and `app/ingest/*`).
- **Supply chain.** Actions pinned by SHA; `permissions:` minimal per job; `uv.lock` and
  `package-lock.json` are the pins; images signed and attested (Pillar 2); release tarballs with
  `SHA256SUMS` like upstream Memos ships.
- **CI mirrors the local gate** (owner decision (d)): pyright, ruff check, ruff format, pytest
  against a Postgres service, `alembic check`, the mutation controls, vitest, `npm run build`, the
  places smoke; on the iOS side a macOS runner for `xcodegen` + build + tests. The maintainer's
  local gate remains the authority for the maintainer's merges; CI is what a contributor's PR gets.

**Hard parts.** The history scrub is the one act in this document that cannot be undone twice:
once the repository is public, a rewrite is a force-push to strangers. So the scan runs on the
mirror (the rehearsal), the owner reads the triage, and the flip waits a day. Scorecard's
"Branch-Protection" and "Signed-Releases" need settings outside the tree (done by hand, read back
by the Scorecard action). The REUSE headers touch every file once: one commit, no logic, reviewed
by diffstat (`+2` per file, nothing else).

**Acceptance probe.** `reuse lint` exit 0; `pip-licenses --fail-on GPL*` exit 0 after the
`pi-heif` swap (positive control: it fails *before* the swap); `gitleaks detect` over all refs of
the mirror exits 0 with the triage recorded; Scorecard ≥ 8.0 on the public repository within 48
hours of the flip; `git grep -c -i discomarder` on the public tree = 0 with a positive control
(`git grep -c memosng` > 0); a private vulnerability report submitted by a test account arrives.

### Pillar 5 — A product, not a household tool: the name, the docs site, the demo instance, i18n

**Scene.** The project has a name that is its own, a one-page site with three screenshots and
the install command, a docs site with "Install", "Configure", "Import", "Back up", "Develop",
"Security" and "API", and a public demo full of pictures nobody owns, reset nightly, with a visible
"demo" banner and a demo login that App Review and curious visitors both use.

**Mechanism.**

- **The name (owner decision (b)).** Week-1 procedure, run on five candidates: App Store search
  (US, DE, CH), Google Play search, GitHub and PyPI and npm name checks, `.app`/`.org` domain
  availability, a USPTO/EUIPO quick search, and a pronounceability test in English and German.
  The bundle id stays `live.discomarder.memosng` (§ 9 (g)); the App Store name, display name,
  repository, image name, CLI name and docs domain change together on rename day. The repository
  rename leaves `memos-ng` as a GitHub redirect.
- **Docs site.** MkDocs Material on GitHub Pages from `docs/`, built in CI; `docs/configuration.md`
  generated (Pillar 2); `docs/api.md` renders `openapi.json`; the two moonshots and the ADR index
  published as the public roadmap (ADRs today live in BookStack; the accepted ones that explain
  the architecture are exported to `docs/adr/` as part of the flip, with household specifics
  removed; this is the "Decisions" chapter made public).
- **Demo instance.** A small VPS outside the household network (owner decision (f)), the public
  image, a seeded library of CC0 photos and short clips with real EXIF (dates, GPS for Places),
  three demo users (admin, member, guest pass), `DEMO_MODE=1` which disables destructive actions
  and resets from a snapshot nightly, a banner, and *this* is the server in the App Review notes.
  It is also the second instance Principle 7 demands: every identity and config change lands
  here first.
- **Landing page.** The docs site's front page: the sentence from § 2, three screenshots, the
  compose snippet, the App Store badge, the license and Scorecard badges.
- **i18n.** `vue-i18n` with English and German message catalogues extracted from the 45 `.vue`
  files and the composables; a per-user locale and time zone in settings (the server already
  stores per-user settings); `<html lang>` follows the user. On iOS, a String Catalog with English
  and German. German because the household is the first non-English reviewer; the structure is
  what matters.
- **The README** becomes the front door: what it is, one screenshot, install in twelve lines,
  links. The changelog moves to `CHANGELOG.md`, kept (it is good history), and release notes
  are generated from it.

**Hard parts.** The rename touches two repositories, an App Store Connect record (the *name*
field can change with a submission; the SKU and bundle id cannot), a GHCR package, a docs domain,
and every mention in the household's own stack. One checklist, one day, one owner read-back. The
demo seed must be genuinely CC0 (Unsplash's licence is *not* CC0 since 2017; use Wikimedia Commons
CC0 or Pexels' licence with attribution checked per file), and nothing from the household's
library may ever reach it.

**Acceptance probe.** The name passes all seven checks, recorded in the decision ticket. The docs
site builds from a clean clone in CI. A stranger's phone (not a household device) installs the
App Store build and signs into the demo instance with the demo credentials; the Places map shows
the seeded GPS; the nightly reset is observed twice. The German UI is reviewed by a native speaker
on the five main flows with zero English leaks.

### Pillar 6 — The App Store: server-first onboarding, the review dress rehearsal, the listing

**Scene.** Install from the App Store. The first screen asks for your server's address, nothing
else; a "Try the demo" link fills in the demo server. Sign in opens your server's own login in a
browser sheet. Settings has "Privacy policy", "Licenses" and "Report a problem". The listing has
the real name, real screenshots, a support page, a privacy policy on the project's own domain.

**Mechanism.**

*iOS.*

- **Onboarding.** `SignInView` gets the server field on the first screen with `memos.example.com`
  as placeholder and *no* default (`SignInView.swift:20` loses its literal); the Advanced token
  sheet keeps its field for PAT users. The guest door base URL today is "fixed, never derived from a pass link a
  person opened: a guest token goes to this host and no other" (`GuestWire.swift:267-270`), a
  written rule. For strangers' servers it must become per-instance; the recommended shape keeps
  the rule's intent (a token never follows an arbitrary link) by pinning the host at *redeem*
  time: the app accepts a pass link, shows the host, and binds the guest token to that host and no
  other for its lifetime (§ 9 (i)). "Try the demo" sets the demo server. The server
  address is persisted next to the token in the Keychain (`CredentialVault`), one record per
  account, so a household member can switch instances later.
- **The dress rehearsal.** Week 1 submits a build to **external TestFlight with a public link**.
  External builds go through Beta App Review, which applies the same guidelines with lower stakes.
  Its outcome is the first real signal on 4.8 (Sign in with Apple: we rely on the exemption for
  apps that exclusively use their own account system, stated in the notes), 5.1.1(v) (account
  deletion: the app creates no accounts, so the requirement does not trigger; stated in the notes),
  2.1 (the demo server and credentials in the notes), 1.2 (user-generated content: a private
  self-hosted server's content is visible only to accounts the household's admin created; stated).
  Whatever the Beta review flags is fixed in week 2, not week 4.
- **In-app requirements.** Privacy-policy link in Settings pointing at the project domain (not
  the household server); an open-source licenses screen generated from `Package.resolved`
  licenses (13 SPM packages, licenses recorded in week 1); the privacy manifests already declare
  `NSPrivacyTracking false`, no collected data, and the two required-reason categories (file
  timestamps C617.1; UserDefaults CA92.1 and 1C8F.1), unchanged; `ITSAppUsesNonExemptEncryption
  = NO` (HTTPS only).
- **Entitlements.** Remove the `applinks`/`webcredentials` associated domains for
  `memos.discomarder.live` and `trip.discomarder.live` (`project.yml:113-138`); the custom scheme
  `memosng://auth-callback` is the permanent deep-link lane (ADR-0016) and is server-agnostic; the
  guest universal link becomes a scheme link or a scanned QR (the scanner already exists,
  `TripPassScannerView.swift`). Associated domains for the *project* domain return only if the
  project hosts an AASA, which it does not need to.
- **Listing machinery.** `fastlane deliver` with `fastlane/metadata/` in the repo (name,
  subtitle, description, keywords, support URL, marketing URL, privacy URL, release notes, en-US
  and de-DE), `fastlane snapshot` or a scripted simulator run producing the 6.9" set (1320×2868)
  against the demo server, plus the 6.1" set; the age-rating questionnaire answered under the
  2026 tiers (expect 4+ with "Unrestricted Web Access: No" and UGC answers reflecting a private
  server); a support URL on the docs site with an issue-tracker link.
- **Submission.** Week 3 Friday, with review notes that carry the demo server, credentials, the
  4.8/5.1.1/1.2 statements and a one-paragraph description of self-hosting. Week 4 absorbs one
  rejection loop. Release is manual ("Manually release this version") so the App Store release
  lands on launch day with the repository flip.

*Server.* `/privacy` already exists as a route (`openapi.json`); it becomes a template the
operator can edit, and the project's own policy lives on the docs site.

**Hard parts.** Beta App Review can take days; week 1's build must be ready by Wednesday. The
server field is the kind of change that breaks the household's muscle memory; the migration for
existing installs pre-fills the field from the stored account so nobody is signed out. iOS 26 is
the deployment target (`project.yml:17`), which is fine for the review (Xcode 26 SDK is mandatory
from 2026-04-28 anyway) but excludes anyone on iOS 18; that is a product decision already made
(iPhone 16 and up) and stays. Screenshots must not contain household faces: the demo library
provides them.

**Acceptance probe.** Beta App Review approved (week 1 build, external public link, a non-household
tester installs via the link). App Review approved (week 3 submission). On launch day, a
non-household iPhone installs from the App Store search by the new name, signs into the demo
instance, and uploads a picture from the share sheet that appears on the demo web UI. App Store
Connect read-back shows the version "Ready for Sale".

### Pillar 7 — Scale to a stranger's library: 250,000 items, two cores, many workers

**Scene.** Someone imports fifteen years of photos. The timeline opens in under a second, the day
mosaic scrolls without a stutter, the Places map draws the whole set, search filters are instant,
derivatives for the backlog grind through overnight on a four-core NAS without starving the UI,
and a second worker container on a beefier box joins and the backlog drains twice as fast.

**Mechanism.**

*Server.*

- **The synthetic library.** A generator (`memosng dev seed --items 250000`) creates realistic
  rows and derivative files (tiny JPEGs with real EXIF dates and GPS, a long-tail distribution
  across 15 years, 4 % videos) so the benchmark is reproducible on any machine.
- **Query budgets as tests.** Every list endpoint (timeline, library sections, collection
  pages, map markers, tag filters) gets a `pytest` that runs it against the seeded library and
  asserts a statement count (no N+1) and a wall-clock budget under `EXPLAIN (ANALYZE)` with the
  indexes the migration adds. Cursor pagination already exists (`app/services/cursors.py`); the
  map's whole-set markers endpoint (ADR-0018) gets a server-side simplification above 50,000
  points.
- **Worker replicas.** The queue is already `FOR UPDATE SKIP LOCKED` with a claim-time predicate
  and lease (`MEDIA_CLAIM_STALE_SECONDS`); the change is to make several *containers* safe:
  `claimed_by` already carries `hostname:pid` (`worker.py:56,402`); the heartbeat, today one
  file at `DATA_ROOT/worker.heartbeat` (`worker.py:292-293`), becomes a per-container row; the
  slot validation checks
  the container's own `WORKER_CPU_BUDGET`, and the compose documents `deploy.replicas` for the
  worker. The two-consumer design (transcode and light) stays per container.
- **Hardware transcoding, optional.** `FFMPEG_HWACCEL=vaapi|qsv|nvenc|videotoolbox-none`, probed at
  startup, falls back to software with a logged reason; the poster/HDR tone-map path stays
  software because correctness beats speed there.
- **Storage budgets.** Derived tiers are a cache in principle; a `DERIVED_BUDGET_GB` with an LRU
  over the mobile/web tiers (never thumbs, never originals) so a 2 TB library fits on a 500 GB
  cache disk; documented in the configuration reference with the default "unbounded".
- **Low-end defaults.** The compose ships `WORKER_CPU_BUDGET=2`, one light slot, one transcode
  slot at `FFMPEG_THREADS=1`; the docs show the four-core and eight-core profiles.

**Hard parts.** The owner's library is small (the household's few users and their memos), so none of the above has ever been measured; the budgets will move once the seed runs.
Multi-container workers change the heartbeat semantics that the stall detection relies on
(`WORKER_*_STALL_SECONDS`); the alert that fires today when the one worker goes silent must become
per-container. Hardware transcoding across vendors is a swamp; the first release supports VAAPI
(Intel/AMD, the common NAS case) and nothing else, with the flag reserved.

**Acceptance probe.** Against the 250,000-item seed on a four-core VM: timeline first page p95
< 300 ms, library day sections p95 < 500 ms, map markers for the whole set < 2 s, every list
endpoint ≤ 3 SQL statements per page (asserted). Two worker containers drain a 10,000-derivative
backlog with zero duplicate work (`claimed_by` distinct per job) and the household alert rule
fires when one of them is stopped. VAAPI transcodes a 4K clip on an Intel N100 in under real-time
with a bit-identical poster to the software path.

### Pillar 8 — Open platform and community: the API, the CLI, a read-only MCP server, the contributor path

**Scene.** A developer reads the API page, downloads the OpenAPI document, generates a client in
their language, and builds a Home Assistant card that shows today's pictures. An agent on their
laptop asks "where were we in May 2024?" through a read-only MCP server and gets an answer from
their own library. A first-time contributor opens a `good first issue`, runs the gate in CI, and
gets a review within the week.

**Mechanism.**

- **The API as a product.** `openapi.json` published per release on the docs site with a
  changelog of paths; the TypeScript types (`gen:api`) and the Swift client
  (`swift-openapi-generator`, already how the app consumes it) as the two reference consumers;
  the `apiMajor` on `/health` (ADR-0019) is the public compatibility promise. The legacy lanes
  `/api/v1/*` and `/file/*` move behind `LEGACY_COMPAT=1`, default off, documented as "for the
  one-way importer's transition period only".
- **The CLI** (Pillar 2) is also the scripting surface: JSON output, exit codes, documented.
- **Webhooks.** `memo.created`, `attachment.ready`, `collection.updated`, signed with a per-hook
  secret, retried with backoff; the SSE stream (`/api/events`) already carries the live events,
  the webhook is the same event fan-out to a URL.
- **MCP server, read-only.** The household moonshot's Pillar 10 names it; the public release ships
  the smallest honest version: a `memosng mcp` subcommand exposing `search_memos`,
  `list_collections`, `get_memo`, `library_summary`, authenticated with a PAT, refusing every
  write, logging every call. It is the feature that gets the project talked about in 2026 and it
  is a day of work on top of the existing services.
- **The contributor path.** `CONTRIBUTING.md` (set up in ten minutes with `compose.dev.yaml`,
  how the gate works, the test-realness rule, DCO sign-off), `CODE_OF_CONDUCT.md` (Contributor
  Covenant 2.1), issue and PR templates, `good first issue` labels seeded with twenty real items
  from the backlog that do not need household context, GitHub Discussions on, a public roadmap
  (the two moonshots and this one).

**Hard parts.** Twenty genuinely context-free issues are harder to write than they sound; the
backlog is full of ticket keys and household history. The MCP server must not become the write
path by accident: it is a separate process with a read-only PAT scope, which means PAT scopes
(today a PAT is the user) are a prerequisite, and that is the one schema change in this pillar.

**Acceptance probe.** A client generated from the published `openapi.json` with a third-party
generator (openapi-generator, Go) lists memos against the demo instance. A webhook delivery is
received by a test endpoint with a valid signature within 5 s of an upload. The MCP server
answers a date-range query from Claude Desktop against the demo library and its write attempt is
refused and logged. The first external PR runs CI green and is merged within the launch week.

---

## 5. The roadmap: four weeks, eight squads, one flip, one submission

### The dependency spine

```
Week 1  decide ──► license text, name, history verdict, pi-heif swap, scan on the mirror
        │          generic OIDC + local login spike on the demo instance
        │          external TestFlight (public link) submitted Wednesday ─► Beta App Review
        ▼
Week 2  make generic ──► setup wizard, passkeys, invitations; compose + GHCR arm64; config reference
        │                importers (memos, takeout); seed + first budgets; i18n scaffolding
        │                iOS: server-first onboarding, privacy link, licenses screen, metadata, screenshots
        ▼
Week 3  go public ──► FLIP (Wednesday 18 Nov): LICENSE, PROVENANCE, scrub, history, CI green
        │             docs site live, demo instance public, Scorecard run
        │             iOS: App Store submission (Friday 20 Nov) against the demo instance
        ▼
Week 4  launch ──► GHCR release, signed; export/import round trip; MCP; webhooks
                   App Store release on approval; announcement; scoreboard read
```

Three things gate everything else and are therefore Monday-of-week-1 work: the owner's license
and name decisions (an afternoon), the history scan on a private mirror (a day), and the external
TestFlight build (ready Wednesday, because Beta App Review is the only clock we do not control).

### Week 1 — "Decide and unblock" (2–6 Nov) · external TestFlight build Wednesday 4 Nov

- **Trust squad:** mirror both repositories privately; `gitleaks` + `trufflehog` over all refs;
  triage by shape; any real credential rotated and proven at its consumer, then the filter-repo
  plan written; `pi-heif` swap with the HEIC tests as the gate; `pip-licenses` / `license-checker`
  inventories; the license file set drafted for the owner's signature; `PROVENANCE.md` drafted
  from § 1.3.
- **Identity squad:** OIDC discovery with endpoint overrides; `OIDC_ADMIN_CLAIM`; local password
  login behind `LOCAL_LOGIN=1`; the demo instance (Pillar 5) stood up on a VPS from the branch
  image with Pocket ID as its provider. Nothing touches the household instance this week.
- **Install squad:** `compose.yaml` + `.env.example`; GHCR workflow building amd64 + arm64 from a
  tag on the private repo (GHCR private packages work before the flip); cosign + attest.
- **Product squad:** the five name candidates through the seven checks by Thursday; CC0 seed
  library sourced (Wikimedia Commons) with EXIF verified; MkDocs skeleton.
- **Store squad:** server field on the first screen, demo link, privacy link, licenses screen;
  `fastlane/metadata/` with the *working* name; build to external TestFlight with a public link
  on **Wednesday**, review notes carrying the demo server; the 13 SPM licenses recorded.
- **Scale squad:** the seed generator; first measurements on the household's own hardware profile
  and on a two-core VM; the query-budget test harness.
- **Import squad:** schema probe for upstream Memos across 0.2x and 26.x against real containers;
  the Takeout sidecar parser with a corpus of the known oddities.
- **Platform squad:** PAT scopes design (the one schema change), webhook table design, MCP spike.
- **Owner, Friday:** decisions (a) license, (b) name, (c) history, (d) CI, (e) accessibility
  baseline, (f) demo host, (g) bundle id, (h) legacy lanes, (i) guest door host, from § 9. Each recorded on its ticket.

### Week 2 — "Make it generic" (9–13 Nov) · household instance moves to generic OIDC Thursday 12 Nov

- **Identity:** setup wizard; passkeys (WebAuthn) with a registration and assertion test suite;
  invitations; the household instance switched to `OIDC_ISSUER`-only discovery **after** the demo
  instance has proven two providers, verified by a real browser login and a real phone login
  (Principle 7).
- **Install:** `memosng` CLI (`backup`, `restore`, `check`, `user`); the configuration reference
  generator in the gate; integrations verified hidden when unset; low-end defaults.
- **Trust:** REUSE headers in one commit; `SECURITY.md`, `security.txt`, threat model; CI workflows
  mirroring the local gate on the private repo (green before the flip is a flip precondition);
  the Codex adversarial pass over `app/routers/*` and `app/ingest/*`.
- **Product:** rename day prepared as a checklist; `vue-i18n` extraction, German catalogue; the
  per-user locale and zone; README rewritten; `CHANGELOG.md` split out.
- **Store:** fix whatever Beta App Review flagged; associated domains removed; String Catalog;
  screenshots generated against the demo instance (6.9" and 6.1"); age rating answered; the
  VoiceOver baseline on the five main flows if (e) says yes.
- **Scale:** indexes and N+1 fixes from the week-1 measurements as a migration; worker
  replicas with per-container heartbeat; the alert rule.
- **Import:** `import memos` generalised and re-runnable by hash; `import takeout`; `import folder`.
- **Platform:** PAT scopes migration (expand/contract); webhooks; `LEGACY_COMPAT` flag.

### Week 3 — "Go public" (16–20 Nov) · FLIP Wednesday 18 Nov · App Store submission Friday 20 Nov

- **Monday:** rename day. Both repositories, the App Store Connect name, GHCR package, docs
  domain, the household stack. One checklist, one read-back.
- **Tuesday:** flip rehearsal on the mirror: history as decided, LICENSE + NOTICE + THIRD_PARTY +
  PROVENANCE + SECURITY + CONTRIBUTING + CODE_OF_CONDUCT + templates committed; CI green on the
  mirror; `git grep -i discomarder` = 0 with its positive control; the owner reads the scan triage.
- **Wednesday:** **the flip.** Visibility public on both repositories; private vulnerability
  reporting on; branch protection on; Discussions on; Scorecard action runs; docs site live; demo
  instance public with its banner.
- **Thursday:** fix what the first strangers and Scorecard find (there will be things).
- **Friday:** **App Store submission** with the demo instance in the notes; release set to manual.
- Meanwhile: Import squad ships Apple Photos export + `EXPORT_FORMAT.md` + the round trip; Scale
  squad runs the 250k benchmark on the four-core VM and the Pi 5 and publishes the numbers in the
  docs; Platform squad ships `memosng mcp` read-only.

### Week 4 — "Launch" (23–27 Nov) · release Friday 27 Nov

- **Monday–Wednesday:** absorb the App Review loop (one rejection budgeted); the first external
  PRs reviewed; the twenty `good first issue`s live.
- **Thursday:** the release train: tag, GHCR images signed and attested, `SHA256SUMS`, release
  notes through the outgoing-text gate (first person, no AI attribution), the household stack
  moved to the GHCR image by digest (the same image strangers pull: Principle 9's last proof).
- **Friday:** App Store "Release this version"; the announcement (the project's own site, then
  r/selfhosted and Hacker News "Show HN", written and gated as outgoing text); the scoreboard
  (§ 6) read on the real surfaces and recorded.

### Squads and staffing

| Squad | Pillar | Owns | Hands |
|---|---|---|---|
| Identity | 1 | discovery, local login, passkeys, wizard, invitations | 3 server |
| Install | 2 | compose, GHCR, CLI, config reference, upgrade contract | 2 server + 1 infra |
| Import | 3 | memos/takeout/apple/folder importers, export format, round trip | 3 server |
| Trust | 4 | licenses, scrub, history, security programme, CI, supply chain | 2 server + 1 security |
| Product | 5 | name, docs site, demo instance, i18n, README | 1 server + 1 frontend + 1 writer |
| Store | 6 | onboarding, entitlements, listing, submission, Beta review loop | 3 iOS |
| Scale | 7 | seed, budgets, replicas, VAAPI, storage LRU | 2 server + 1 perf |
| Platform | 8 | PAT scopes, webhooks, MCP, contributor path | 2 server |

The owner's week is bounded: Friday decisions in week 1, the Thursday household cutover in
week 2, the Tuesday scan triage and Wednesday flip in week 3, the Friday release in week 4. Four
touchpoints, each with a prepared artifact to read.

---

## 6. The scoreboard

The moonshot is done when every line reads true on the real surface on Friday 27 November 2026.

| # | Probe | Surface |
|---|---|---|
| 1 | Both repositories public, `LICENSE` present, Scorecard ≥ 8.0 | github.com |
| 2 | `git grep -c -i discomarder` = 0 in both public trees, positive control > 0 | the public clones |
| 3 | `reuse lint` 0; `pip-licenses --fail-on 'GPL*'` 0; `THIRD_PARTY_NOTICES.md` names Debian's ffmpeg | CI log |
| 4 | A stranger's clean Ubuntu VM: `git clone` to first photo ≤ 10 min from the README alone | a timer, a screen recording |
| 5 | The same on a Raspberry Pi 5 with the arm64 image, `cosign verify` 0 | the Pi |
| 6 | `/setup` creates an admin with a passkey; an invite link creates a member; both log in on web and app | demo instance, a non-household phone |
| 7 | Keycloak and Pocket ID each log in with only `OIDC_ISSUER`/`CLIENT_ID`/`CLIENT_SECRET` set | demo instance |
| 8 | The household logs in through the same generic path with Authelia, unchanged, all month | `memos.discomarder.live`, a household phone |
| 9 | A Memos 26.x instance imports twice with zero duplicates; a Takeout imports with sidecar dates honoured | row counts, sha256 sets |
| 10 | `export` → fresh instance → `import` is lossless (memos, attachments, collections, tags) | diff of the two exports |
| 11 | 250,000 items on four cores: timeline p95 < 300 ms, sections p95 < 500 ms, markers < 2 s, ≤ 3 statements per page | benchmark report in docs |
| 12 | Two worker containers drain a backlog with no duplicate claims; stopping one fires the alert | `media_jobs`, Alertmanager |
| 13 | Beta App Review approved (week 1); App Review approved (week 3); "Ready for Sale" (week 4) | App Store Connect read-back |
| 14 | A non-household iPhone finds the app by its new name, signs into the demo, shares a photo that appears on the demo web | the phone, the demo web UI |
| 15 | The German UI has zero English leaks on the five main flows | a native speaker's checklist |
| 16 | A private vulnerability report reaches the maintainer; `security.txt` served | GitHub advisory inbox, `curl` |
| 17 | A third-party-generated client lists memos; a webhook arrives signed; the MCP server answers and refuses a write | demo instance logs |
| 18 | The first external PR merged with CI green | the public repository |
| 19 | The household stack runs the public GHCR image by digest | `docker inspect` on jupiter |
| 20 | Every owner decision in § 9 recorded on its ticket with the date | the tracker |

---

## 7. What we will not do

Saying no is most of the moonshot.

- **No multi-tenancy, no hosted service.** One household per instance. We publish software, not
  a service, and the demo instance is a demo.
- **No telemetry, no update pings, no crash reporting.** Not even opt-in in this month; "Data Not
  Collected" stays literally true.
- **No SQLite, no MySQL.** Postgres 17 is the database. A compose file with Postgres inside it is
  as easy as SQLite for the person installing it, and it keeps one code path.
- **No Kubernetes manifests, no Helm chart, no Unraid template** in this month. Compose is the
  contract; the community can wrap it.
- **No Android, no iPad layout, no macOS build.** The iPhone app ships; the household moonshot's
  Pillar 6 owns other screens later.
- **No chasing upstream Memos API compatibility.** Two legacy routes go behind a flag; the
  product is its own. Compatibility is the importer, not the API.
- **No cloud ML, no face recognition, no embeddings** in this release. Pillar 1 of the household
  moonshot, later, local-only.
- **No S3 or object storage.** `DATA_ROOT` on a filesystem is the storage; it is what a NAS has.
  The abstraction exists in `storage.py` for the day that changes.
- **No accessibility *programme*** (owner, 2026-09-20), but see § 9 (e): a one-week VoiceOver
  baseline is proposed because the App Store is a public surface.
- **No CLA.** DCO sign-off only; contributors keep their copyright.
- **No marketing beyond the announcement.** One site page, one Show HN, one r/selfhosted post,
  all first-person, all through the outgoing-text gate.

---

## 8. Risks and kill criteria

| Risk | What it would look like | Kill criterion / fallback |
|---|---|---|
| History contains a live credential that cannot be rotated in time | The week-1 scan finds a secret whose consumer we no longer control | Publish a squashed "public genesis" history instead; the full history stays private; `PROVENANCE.md` states the squash and the commit count |
| Generic OIDC breaks the household login | Authelia login fails after the week-2 switch | Roll back to endpoint overrides (they remain supported); the switch is re-tried only after the demo instance proves Authelia itself as a third provider |
| Beta App Review rejects on 4.8 (Sign in with Apple) | Apple does not accept the own-account-system exemption for a self-hosted client | Add Sign in with Apple as an *optional* server-side OIDC provider (Apple is an OIDC issuer); the setup wizard offers it; nothing else changes |
| App Review rejects on 2.1 (cannot reach the server) | The reviewer does not use the demo credentials | Built-in demo mode (a bundled read-only sample library) with Apple's prior approval requested in the notes; the demo server remains for everyone else |
| App Review rejects on 1.2 (UGC) | Apple wants report/block for content shown in the app | Add "Report" and "Hide from this device" on a memo card, backed by an admin inbox; one squad-week |
| The name fails a late check | A trademark or an App Store name collision surfaces after rename day | Rename day is Monday of week 3 so a second rename fits before the Wednesday flip; the App Store name changes with the submission anyway |
| `pi-heif` decodes something `pillow-heif` decoded | A HEIC variant in the HEIC test corpus fails to open | Keep `pillow-heif` and ship `THIRD_PARTY_NOTICES.md` with the GPLv2 x265 disclosed and the server under AGPL (compatible); the iOS repo is unaffected |
| arm64 image does not build | A wheel is missing for `linux/arm64` | Ship amd64 only at launch with an issue open; Pi 5 probe moves to the first patch release |
| The 250k benchmark exposes a schema problem that needs a destructive migration | A list endpoint cannot meet its budget without restructuring a table | Expand/contract across two releases; the budget for that endpoint is documented as "known" at launch; nothing destructive without the owner |
| Strangers arrive before the docs are good enough | Issues are "how do I…" rather than bugs | The docs squad triages issues into docs pages daily in week 4; the FAQ is written from the first twenty questions |
| The owner's four touchpoints slip | A decision day passes without the decision | The standing rule: the recommended option is taken and recorded; the owner course-corrects later (the autonomy table) |

Programme-level: the household keeps using the product while its identity layer changes.
Principle 7 is the only real safety net, and it is cheap: the demo instance exists from week 1.

---

## 9. Owner decision points

Every other choice in this document takes the recommended option under the standing rule. These
nine either reverse something decided on purpose, or are irreversible, or spend something only
the owner can spend.

| # | Decision | Today | Recommended | Why it is the owner's |
|---|---|---|---|---|
| (a) | Licenses | "Private household project. Not licensed for redistribution." (`README.md:1221-1223`) | Server **AGPL-3.0-only**; iOS app **MIT**; `openapi.json` and generated clients **MIT**; docs **CC BY 4.0** | A license is a permanent grant; it cannot be withdrawn from anyone who received the code under it |
| (b) | The public name | `memos-ng` / `memosng` | A new name that passes the seven checks (Pillar 5), chosen Friday of week 1; the GitHub repo keeps a redirect | Naming is the owner's brand decision; also an App Review risk under 4.1(c)/2.3.7 |
| (c) | History | 364 + 409 private commits with hostnames, ticket keys and one by-design dev secret | **Publish the full history** after the scan triage; rotate-then-rewrite only real credentials | Irreversible once public; a rewrite later is a force-push to strangers |
| (d) | CI on GitHub Actions | "The LOCAL gate is the gate — there is NO CI" (MEMNG-1 cancelled) | **Re-open:** CI mirrors the local gate for contributors' PRs; the local gate stays the maintainer's authority for merges | Reverses a recorded decision |
| (e) | Accessibility | "No accessibility programme" (owner, 2026-09-20) | **A one-week VoiceOver baseline** on sign-in, timeline, library, viewer, compose; no ongoing programme | Reverses the spirit of a recorded decision because the surface becomes public |
| (f) | Where the demo instance runs | Nothing | **A small VPS outside the household network** (not pluto, not jupiter), public image, nightly reset | A public, attacker-facing instance must not share a network, a database or a Cloudflare tunnel with the household |
| (g) | The iOS bundle id | `live.discomarder.memosng` | **Keep it.** It is invisible to users; changing it means a new App Store record and the loss of the TestFlight history | A household domain lives on in the public app's identifier forever; only the owner can accept that |
| (h) | Legacy compat lanes | `/api/v1/*` and `/file/*` served to everyone | **Behind `LEGACY_COMPAT=1`, default off** | Changes a public-facing route set on the household's instance |
| (i) | The guest door host | "Fixed, never derived from a pass link" (`GuestWire.swift:267-270`) | **Bind the host at redeem time**: the app shows the pass's host, the guest token is pinned to it for life | Reverses a written security rule in the client |

Each is recorded, once decided, on its ticket as `Decided <date> … the recommended option; the
owner may course-correct later`, and (a), (c) and (d) become ADRs in the public `docs/adr/`.

---

## Appendix A — The licensing file set

| File | Server repo | iOS repo | Content |
|---|---|---|---|
| `LICENSE` | AGPL-3.0-only (full text) | MIT (full text) | the grant |
| `NOTICE` | project name, copyright line, "contains third-party software, see THIRD_PARTY_NOTICES.md" | same | short |
| `THIRD_PARTY_NOTICES.md` | Python (from `pip-licenses`), npm (from `license-checker`), Debian `ffmpeg` (version, `--enable-gpl`, how to `apt source`), basemap (ODbL, Protomaps) | the 13 SPM packages | generated + one hand section |
| `PROVENANCE.md` | § 1.3 text | one paragraph pointing at the server's | the clean-room statement |
| `REUSE.toml` + `LICENSES/` | AGPL-3.0-only, MIT (for `openapi.json`, `frontend/src/api/schema.d.ts`), CC-BY-4.0 (docs) | MIT | `reuse lint` passes |
| `SECURITY.md` | supported versions, 72-hour acknowledgement, private reporting link, PGP optional | same | |
| `CONTRIBUTING.md` | dev setup, the gate, test-realness, DCO | the Mac gate, xcodegen | |
| `CODE_OF_CONDUCT.md` | Contributor Covenant 2.1 | same | |
| `.github/ISSUE_TEMPLATE/*`, `PULL_REQUEST_TEMPLATE.md`, `FUNDING.yml` (optional) | | | |

## Appendix B — The configuration reference that must exist

Generated from `app/config.py` (`class Settings`, 101 fields, 0 required today). Groups and the
fields whose *defaults* change in this moonshot:

- **Instance:** `PUBLIC_URL` (new; drives `OIDC_REDIRECT_URI`, `NATIVE_CALLBACK_URL`,
  `TRIP_PASS_BASE_URL`, `VAPID_SUBJECT`), `ENV`, `APP_ROLE`, `DEMO_MODE` (new), `LEGACY_COMPAT`
  (new, off), `LOCAL_LOGIN` (new, on when no OIDC issuer is set).
- **Identity:** `OIDC_ISSUER` (discovery), the six endpoint fields as overrides,
  `OIDC_ADMIN_CLAIM`/`OIDC_ADMIN_VALUE` (new), `SESSION_*`, `PAT_*`, `NATIVE_*`, grants, `APPLE_TEAM_ID`,
  `IOS_BUNDLE_ID`, `AASA_KEYS`.
- **Storage and media:** `DATA_ROOT`, `TILES_DIR`, `ASSET_URL_*`, `UPLOAD_*`, `MAX_UPLOAD_MIB`,
  `DERIVED_BUDGET_GB` (new).
- **Worker:** `WORKER_*`, `FFMPEG_THREADS`, `FFMPEG_HWACCEL` (new), `THERMAL_*` (opt-in),
  `MEDIA_*`, `SLOW_TRANSCODE_TIMEOUT_S`.
- **Guests and ingest:** `TRIP_PASS_BASE_URL`, `INGEST_*`, `GUEST_*`, `TRIP_INGEST_*`.
- **Integrations (optional, unset = hidden):** `VAPID_*`, `SMTP_*`, `FRAME_*`, `IMGMANAGER_*`,
  `SIBLING_OWNER_IDS`, `DROPBOX_*`, `WEBHOOK_*` (new).
- **Database:** `DATABASE_URL`, `DATABASE_URL_SYNC`, `DB_POOL_*`.

## Appendix C — App Store Connect checklist (the record exists; this is what changes)

| Item | Today | Launch |
|---|---|---|
| App name | `memosng` | the new name (changes with the submission) |
| Bundle id / SKU | `live.discomarder.memosng` / `memosng-ios` | unchanged (§ 9 (g)) |
| Privacy policy URL | `https://memos.discomarder.live/privacy` | `https://<project-domain>/privacy` |
| In-app privacy link | none | Settings → Privacy policy |
| App Privacy | "Data Not Collected", published | unchanged, re-confirmed against the manifests |
| Privacy manifests | tracking false; C617.1, CA92.1, 1C8F.1 | unchanged |
| Export compliance | not set per build | `ITSAppUsesNonExemptEncryption = NO` in `project.yml` |
| Age rating | not answered for the store | 2026 questionnaire; UGC answered for a private server |
| Screenshots | none | 6.9" (1320×2868) and 6.1" sets from the demo instance, en-US and de-DE |
| Support URL / marketing URL | none | docs site pages |
| Review notes | none | demo server, demo credentials, the 4.8 / 5.1.1(v) / 1.2 statements, "self-hosted" paragraph |
| TestFlight | internal group only | external public link (week 1 rehearsal) |
| Release | n/a | manual release on launch day |
| Associated domains | `memos.discomarder.live`, `trip.discomarder.live` | removed |

## Appendix D — Existing tickets and decisions this absorbs or touches

- **MEMNG-1** (CI, cancelled): re-opened as decision (d).
- **ADR-0008** (frozen main bar), **ADR-0016** (custom scheme is the permanent deep-link lane),
  **ADR-0019** (independent semver, `apiMajor` on `/health`), **ADR-0020** (signed asset URL is a
  bearer): all kept and cited in the public docs; none changed.
- **The upload moonshot** (iOS `docs/moonshot.md`, decided 2026-10-27): runs in the same four
  weeks. Its 2.7.0 build is the one that goes to external TestFlight in week 1 of this moonshot
  if its week-1 build is ready; otherwise the current main is. Its decision (e) (dedupe by hash
  with shared originals) is a prerequisite of Pillar 3's import dedupe and is built once.
- **The household moonshot** (`docs/moonshot-household-memory.md`): Pillar 10 (platform and
  agents) is partially advanced by Pillar 8 here (read-only MCP); Pillar 8 (archival fidelity,
  the way out) is partially advanced by Pillar 3's export format and Pillar 2's backup manifest.
- **Autonomy table (CLAUDE.md, 2026-10-24):** App Store release and production deploys stay
  autonomous on green gates; the nine decisions in § 9 are outside it and go to the owner.
- **`CLAUDE.md` rules that change meaning in public:** "there is NO CI" (decision (d)); the
  BookStack page registry and tracker keys move out of the tracked file into the private overlay;
  everything about jupiter, Saturn, merkur and the stack goes with them.
