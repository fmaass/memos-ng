---
name: parity-scope
description: Full feature/settings parity memos-ng must reach before cutover (user mandate)
metadata:
  type: project
---

User mandate (2026-06-20): "all use cases, incl. of all settings, must be taken over
from old memos to memos-ng — no half-baked subset." Read-only audit of prod (agent
a81f3460) established exactly what prod uses, so parity is a bounded set.

**Prod reality:** 2 users (fabian admin + custom avatar, laura), 108 memos (106 PUBLIC /
2 PRIVATE, 0 pinned/archived), 607 media files, tags (in payload), 4 live PATs, SSO via
Authelia (key by preferred_username), 1 reaction. Settings actually set: per-user
default memo visibility = PUBLIC (both), theme = dark (fabian only). Workspace:
MAX_UPLOAD_SIZE_MIB=1024, LOCAL storage. EMPTY/unused (DO NOT build): memo_relation,
memo_share, inbox/comments, webhooks, S3, Telegram, custom-profile/branding.

**Parity gaps memos-ng must close before cutover (user confirmed build ALL of these):**
1. **Per-user settings store** (memos-ng has NONE). Persist: (a) **default memo visibility**
   — FIXES A REAL REGRESSION: MemoCreate currently defaults PRIVATE, prod defaults PUBLIC;
   (b) **theme** server-persisted per user (replace localStorage-only). GET/PATCH settings
   endpoint + SettingsView controls. Migrate both users' values (visibility=PUBLIC, fabian
   theme=dark).
2. **Upload size cap** (1024 MiB) enforced (config + finalize check).
3. **Reactions** (user said BUILD): model + endpoint + UI + compat serialization (stop
   hard-coding reactions:[]) + migrate the 1 existing reaction.
4. **User avatars** (user said BUILD): avatar field + upload/display + migrate fabian's.
5. Theme decision: **persist per-user on server** (user chose this, not per-device).

**Migration (T4.2) must import:** users (map username→subject=preferred_username, role,
nickname/email/avatar), memos (uid/content/visibility/ts/tags), attachments (cp -p bytes
+ reuse derived tiers, no re-transcode), per-user settings, the 1 reaction. PATs CANNOT be
migrated (only hashes stored) → satellites get NEW memos-ng PATs at cutover (cutover task).

The username fix (key by preferred_username via userinfo, not sub UUID) is done (85fad38).
Links: [[codex-homelab-calibration]] [[phase-gated-delivery]].
