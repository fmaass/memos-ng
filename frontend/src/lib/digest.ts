// Pure logic behind the Mail tab (MEMNG-235). Everything here decides something the panel would
// otherwise decide inside a component: which tab a URL means, whether an address is storable,
// whether a settings draft is legal, whether removing a recipient would leave an armed digest with
// nobody to send to, and what the status row says. It is unit-tested directly, per the repo's
// frontend rule — happy-dom has no round-trip through a real browser to lean on.

import type { components } from '@/api/schema'

export type DigestSettings = components['schemas']['DigestSettingsOut']
export type DigestRecipient = components['schemas']['DigestRecipientOut']
export type DigestRun = components['schemas']['DigestRunOut']
export type DigestSettingsPut = components['schemas']['DigestSettingsPut']

// --- the tab in the URL -------------------------------------------------------------------

export type SettingsTab = 'general' | 'mail' | 'export'

/** The tabs only an admin is shown. General is everyone's and is the fallback for all three cases
 * below, so it is deliberately not in here. */
export const ADMIN_TABS: readonly SettingsTab[] = ['mail', 'export']

/** Which tab `?tab=` asks for. Unknown values and a non-admin asking for an admin tab land on
 * General.
 *
 * The admin argument is not decoration: the Mail and Export tabs are not rendered for a non-admin,
 * so a `?tab=mail` or `?tab=export` they were linked (or that survived a role change) must resolve
 * to a tab that exists — otherwise the view would show an empty tab strip position and no panel at
 * all. One resolver answers for every tab: a second copy for a later tab is how the address bar and
 * the panel start disagreeing.
 */
export function tabFromQuery(value: unknown, isAdmin: boolean): SettingsTab {
  const raw = Array.isArray(value) ? value[0] : value
  if (!isAdmin || typeof raw !== 'string') return 'general'
  return (ADMIN_TABS as readonly string[]).includes(raw) ? (raw as SettingsTab) : 'general'
}

// --- addresses ----------------------------------------------------------------------------

// Deliberately the same shape the API stores by (`app/routers/admin_digest.py`'s `_EMAIL_RE`):
// RFC 5322 addr-spec, trimmed and lower-cased, 320 characters at most. Keeping the two in step is
// what makes the panel's duplicate check agree with the server's UNIQUE index — a client rule that
// accepted `A@B.com` next to `a@b.com` would offer an add that can only ever come back 409.
const ATEXT = "[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]"
const EMAIL_RE = new RegExp(
  `^${ATEXT}+(?:\\.${ATEXT}+)*` +
    '@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\\.)+[A-Za-z]{2,63}$',
)
export const MAX_EMAIL_LENGTH = 320

/** `raw` trimmed and lower-cased, or null when it is not an address this API will store. */
export function normaliseEmail(raw: string): string | null {
  const candidate = raw.trim()
  if (!candidate || candidate.length > MAX_EMAIL_LENGTH || !EMAIL_RE.test(candidate)) return null
  return candidate.toLowerCase()
}

export type RecipientCheck = { ok: true; email: string } | { ok: false; reason: string }

/** The languages a digest recipient reads (MEMNG-1245) — the API's enum, in send order. */
export const DIGEST_LANGUAGES = ['de', 'es', 'en'] as const
export type DigestLanguage = (typeof DIGEST_LANGUAGES)[number]
export const DEFAULT_DIGEST_LANGUAGE: DigestLanguage = 'de'
export const DIGEST_LANGUAGE_LABELS: Record<DigestLanguage, string> = {
  de: 'Deutsch',
  es: 'Español',
  en: 'English',
}

export type LanguageCheck = { ok: true; language: DigestLanguage } | { ok: false; reason: string }

/** A recipient row's language, checked before it is sent: exactly de, es or en and nothing else
 * (no other case, no surrounding space) — the same set the API answers 422 outside of. */
export function checkRecipientLanguage(value: unknown): LanguageCheck {
  if (typeof value === 'string' && (DIGEST_LANGUAGES as readonly string[]).includes(value)) {
    return { ok: true, language: value as DigestLanguage }
  }
  return { ok: false, reason: 'Choose Deutsch, Español or English.' }
}

/** Whether this typed address can be added, and the stored form it would take. */
export function checkNewRecipient(raw: string, existing: readonly string[]): RecipientCheck {
  if (!raw.trim()) return { ok: false, reason: 'Enter an e-mail address.' }
  if (raw.trim().length > MAX_EMAIL_LENGTH)
    return { ok: false, reason: `An address may be at most ${MAX_EMAIL_LENGTH} characters.` }
  const email = normaliseEmail(raw)
  if (email === null) return { ok: false, reason: 'That is not a valid e-mail address.' }
  if (existing.some((e) => e.trim().toLowerCase() === email))
    return { ok: false, reason: 'That address is already a recipient.' }
  return { ok: true, email }
}

// --- the last enabled recipient -------------------------------------------------------------

export type RecipientGuard = { allowed: true } | { allowed: false; reason: string }

/** Mirrors `_refuse_leaving_nobody_to_send_to`: an ARMED digest may not be left with no enabled
 * recipient. Shown as a reason on a disabled control rather than surfaced as a bare 422 — the
 * server still decides (it holds a row lock while it counts), this only explains it in advance.
 */
export function recipientChangeGuard(
  target: Pick<DigestRecipient, 'id' | 'enabled'>,
  all: readonly Pick<DigestRecipient, 'id' | 'enabled'>[],
  digestEnabled: boolean,
): RecipientGuard {
  if (!target.enabled) return { allowed: true }
  if (!digestEnabled) return { allowed: true }
  const others = all.filter((r) => r.id !== target.id && r.enabled).length
  if (others > 0) return { allowed: true }
  return {
    allowed: false,
    reason:
      'This is the last enabled recipient of an enabled digest — turn the digest off, or enable ' +
      'another recipient first.',
  }
}

// --- the grouped configuration ---------------------------------------------------------------

/** The fields behind the Save/Discard bar. `enabled` is deliberately NOT one of them: the switch
 * is its own mutation and must not be able to ride along on a draft the admin has not saved.
 */
export interface DigestConfigDraft {
  send_time: string
  timezone: string
  visibility_scope: DigestSettings['visibility_scope']
  start_date: string
  grace_minutes: number
  max_email_bytes: number
  max_videos_per_memo: number
  max_memos_per_run: number
  mail_title: string
  from_name: string
  from_email: string
  hosting_enabled: boolean
  dropbox_folder: string
  retention_days: number
  link_images_full: boolean
}

export const CONFIG_FIELDS = [
  'send_time',
  'timezone',
  'visibility_scope',
  'start_date',
  'grace_minutes',
  'max_email_bytes',
  'max_videos_per_memo',
  'max_memos_per_run',
  'mail_title',
  'from_name',
  'from_email',
  'hosting_enabled',
  'dropbox_folder',
  'retention_days',
  'link_images_full',
] as const satisfies readonly (keyof DigestConfigDraft)[]

/** `07:00:00` from the API and `07:00` from an `<input type="time">` are the same instant; without
 * this the form would read dirty the moment it loaded. Seconds are kept when they are not zero.
 */
export function normaliseSendTime(value: string): string {
  const match = /^(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value.trim())
  if (!match) return value.trim()
  const [, hh, mm, ss] = match
  return ss && ss !== '00' ? `${hh}:${mm}:${ss}` : `${hh}:${mm}`
}

export function draftFromSettings(settings: DigestSettings): DigestConfigDraft {
  return {
    send_time: normaliseSendTime(settings.send_time),
    timezone: settings.timezone,
    visibility_scope: settings.visibility_scope,
    start_date: settings.start_date,
    grace_minutes: settings.grace_minutes,
    max_email_bytes: settings.max_email_bytes,
    max_videos_per_memo: settings.max_videos_per_memo,
    max_memos_per_run: settings.max_memos_per_run,
    mail_title: settings.mail_title,
    from_name: settings.from_name,
    from_email: settings.from_email,
    hosting_enabled: settings.hosting_enabled,
    dropbox_folder: settings.dropbox_folder,
    retention_days: settings.retention_days,
    link_images_full: settings.link_images_full,
  }
}

/** Which grouped fields differ from the server's row — the Save/Discard bar's whole condition. */
export function dirtyConfigFields(
  draft: DigestConfigDraft,
  settings: DigestSettings,
): (keyof DigestConfigDraft)[] {
  const server = draftFromSettings(settings)
  return CONFIG_FIELDS.filter((field) => draft[field] !== server[field])
}

/** The PUT body: the WHOLE row, so `enabled` comes from the server's value and never from a draft. */
export function putBodyFromDraft(
  draft: DigestConfigDraft,
  enabled: boolean,
): DigestSettingsPut {
  return { ...draft, send_time: normaliseSendTime(draft.send_time), enabled }
}

// --- validating a draft -----------------------------------------------------------------------

export type ConfigErrors = Partial<Record<keyof DigestConfigDraft, string>>

const RANGES = {
  grace_minutes: [0, 1440],
  // The API's floor is 1 MiB (nothing with a single photo fits below it) and its ceiling 100 MB.
  max_email_bytes: [1048576, 104857600],
  max_videos_per_memo: [0, 50],
  max_memos_per_run: [1, 1000],
  retention_days: [1, 3650],
} as const satisfies Record<string, readonly [number, number]>

function isKnownTimezone(tz: string): boolean {
  try {
    new Intl.DateTimeFormat('en-CA', { timeZone: tz })
    return true
  } catch {
    return false
  }
}

/** Is this `YYYY-MM-DD` a day that exists? `2026-02-30` parses and is not. */
export function isCalendarDate(value: string): boolean {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (!match) return false
  const [, y, m, d] = match.map(Number)
  const probe = new Date(Date.UTC(y, m - 1, d))
  return probe.getUTCFullYear() === y && probe.getUTCMonth() === m - 1 && probe.getUTCDate() === d
}

export function isClockTime(value: string): boolean {
  const match = /^(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value)
  if (!match) return false
  const [, h, m, s] = match
  return Number(h) <= 23 && Number(m) <= 59 && Number(s ?? '0') <= 59
}

/** Every reason this draft cannot be saved, keyed by the field that carries the message. Mirrors
 * the API's own bounds so the admin is told before a 422, never instead of one.
 */
export function validateConfigDraft(draft: DigestConfigDraft): ConfigErrors {
  const errors: ConfigErrors = {}

  if (!isClockTime(draft.send_time)) errors.send_time = 'Use a 24-hour time, e.g. 07:00.'
  if (!draft.timezone.trim()) errors.timezone = 'A time zone is required.'
  else if (draft.timezone.length > 64) errors.timezone = 'At most 64 characters.'
  else if (!isKnownTimezone(draft.timezone))
    errors.timezone = 'Not a time zone this browser knows, e.g. Europe/Zurich.'
  if (!isCalendarDate(draft.start_date)) errors.start_date = 'Use a real date, e.g. 2026-06-01.'

  for (const [field, [min, max]] of Object.entries(RANGES) as [
    keyof typeof RANGES,
    readonly [number, number],
  ][]) {
    const value = draft[field]
    if (!Number.isInteger(value)) errors[field] = 'A whole number is required.'
    else if (value < min || value > max) errors[field] = `Between ${min} and ${max}.`
  }

  for (const field of ['mail_title', 'from_name', 'dropbox_folder'] as const) {
    if (!draft[field].trim()) errors[field] = 'This is required.'
    else if (draft[field].length > 255) errors[field] = 'At most 255 characters.'
  }

  // Empty is meaningful and is the seeded value: "send as whatever address authenticates to SMTP".
  // Anything else has to be an address the API will accept, or the sender would not match.
  if (draft.from_email !== '' && normaliseEmail(draft.from_email) === null)
    errors.from_email = 'Use an e-mail address, or leave it empty to send as the SMTP user.'

  return errors
}

// --- the status row ---------------------------------------------------------------------------

/** Today's date in a time zone, as `YYYY-MM-DD` — the same shape `digest_runs.local_date` uses. */
export function localDateIn(timezone: string, now: Date): string | null {
  try {
    return new Intl.DateTimeFormat('en-CA', {
      timeZone: timezone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).format(now)
  } catch {
    return null
  }
}

function wallClockIn(timezone: string, now: Date): string | null {
  try {
    return new Intl.DateTimeFormat('en-GB', {
      timeZone: timezone,
      hour: '2-digit',
      minute: '2-digit',
      // h23, not `hour12: false` alone: some engines render midnight as 24:00 under h24, which
      // would compare above every send time and push the next send to tomorrow all night.
      hourCycle: 'h23',
    }).format(now)
  } catch {
    return null
  }
}

// A run that consumes the day. `test` renders or probes and `import` is the satellite's one-shot
// bookkeeping row — neither is a delivery, so neither may push the next send to tomorrow.
const DAY_CONSUMING_TRIGGERS = new Set(['scheduled', 'manual'])

/** Has a scheduled or manual run already taken `today` (in the configured zone)? */
export function ranOn(runs: readonly DigestRun[] | undefined, day: string): boolean {
  return (runs ?? []).some((r) => r.local_date === day && DAY_CONSUMING_TRIGGERS.has(r.trigger))
}

export type NextSend =
  | { kind: 'off' }
  | { kind: 'unknown-timezone'; time: string; timezone: string }
  | { kind: 'scheduled'; when: 'today' | 'tomorrow'; time: string; timezone: string }
  | { kind: 'first'; date: string; time: string; timezone: string }

/** When the next digest would go out, judged in the configured zone.
 *
 * Only ever a calendar answer — today, tomorrow, or the start date still ahead. The worker owns the
 * real gate; this line exists so the admin can see that the schedule they typed means what they
 * think it means, and it must not promise a send today that the day ledger has already spent.
 */
export function describeNextSend(
  settings: Pick<DigestSettings, 'enabled' | 'send_time' | 'timezone' | 'start_date'>,
  now: Date,
  runs?: readonly DigestRun[],
): NextSend {
  const time = normaliseSendTime(settings.send_time)
  if (!settings.enabled) return { kind: 'off' }
  const today = localDateIn(settings.timezone, now)
  const clock = wallClockIn(settings.timezone, now)
  if (today === null || clock === null)
    return { kind: 'unknown-timezone', time, timezone: settings.timezone }

  // A day that already had a scheduled or manual run is spent, whatever the clock says: nothing
  // sends twice on one local date, and a FAILED run is not retried before tomorrow either (the
  // status row offers Send now for that). `grace_minutes` is a media-readiness window inside the
  // send, not a second chance at the day, so it deliberately does not enter this line.
  const when = !ranOn(runs, today) && clock < time.slice(0, 5) ? 'today' : 'tomorrow'
  const nextDay =
    when === 'today'
      ? today
      : new Date(Date.parse(`${today}T00:00:00Z`) + 86400000).toISOString().slice(0, 10)
  if (settings.start_date > nextDay)
    return { kind: 'first', date: settings.start_date, time, timezone: settings.timezone }
  return { kind: 'scheduled', when, time, timezone: settings.timezone }
}

export function nextSendLabel(next: NextSend): string {
  switch (next.kind) {
    case 'off':
      return 'The digest is off — nothing is scheduled.'
    case 'unknown-timezone':
      return `Scheduled for ${next.time} in ${next.timezone}, a zone this browser cannot resolve.`
    case 'first':
      return `First send on ${next.date} at ${next.time} (${next.timezone}).`
    case 'scheduled':
      return `Next send ${next.when} at ${next.time} (${next.timezone}).`
  }
}

export type LastRun =
  | { kind: 'none' }
  | {
      kind: 'run'
      run: DigestRun
      status: 'running' | 'sent' | 'failed' | 'other'
      severity: 'info' | 'success' | 'danger' | 'secondary'
      /** Today's run failed and nothing retries before tomorrow — Send now is the way back. */
      needsSameDayRecovery: boolean
    }

/** The last-run chip. `runs` arrives newest-first from the API and is not re-sorted here. */
export function summariseLastRun(
  runs: readonly DigestRun[] | undefined,
  timezone: string,
  now: Date,
): LastRun {
  const run = runs?.[0]
  if (!run) return { kind: 'none' }
  const status =
    run.status === null
      ? 'running'
      : run.status === 'sent'
        ? 'sent'
        : run.status === 'failed'
          ? 'failed'
          : 'other'
  const severity =
    status === 'running'
      ? 'info'
      : status === 'sent'
        ? 'success'
        : status === 'failed'
          ? 'danger'
          : 'secondary'
  const today = localDateIn(timezone, now)
  // "The last run FOR TODAY failed" — a failure from yesterday is history, not a call to act.
  const todaysRun = today === null ? undefined : runs?.find((r) => r.local_date === today)
  return {
    kind: 'run',
    run,
    status,
    severity,
    needsSameDayRecovery: todaysRun?.status === 'failed',
  }
}

export function lastRunLabel(last: LastRun): string {
  if (last.kind === 'none') return 'No run yet.'
  const { run, status } = last
  if (status === 'running') return `Running since ${run.started_at}`
  const sent = `${run.memos_sent} memo${run.memos_sent === 1 ? '' : 's'}`
  const parts = `${run.parts_sent}/${run.parts} part${run.parts === 1 ? '' : 's'}`
  return `${run.local_date} · ${status} · ${sent} · ${parts}`
}

/** A byte count as MiB, for the hint under the mail-size cap. */
export function formatMib(bytes: number): string {
  return `${(bytes / 1048576).toFixed(1)} MiB`
}

// --- what a refusal says ----------------------------------------------------------------------

/** The message an inline notice shows for a failed request.
 *
 * FastAPI answers a refusal with `{detail: "…"}` and a validation error with `{detail: [{msg}]}`;
 * openapi-fetch hands the parsed body back as `error` and the status separately. Preferring the
 * server's own sentence is the point — the last-recipient guard and the duplicate-address 409 both
 * explain themselves, and a generic "something went wrong" would throw that away.
 */
export function apiErrorMessage(error: unknown, status?: number): string {
  const detail = (error as { detail?: unknown } | null | undefined)?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail)) {
    const first = detail[0] as { msg?: unknown } | undefined
    if (typeof first?.msg === 'string' && first.msg.trim()) return first.msg
  }
  if (status === 409) return 'That conflicts with something already there.'
  if (typeof status === 'number') return `The request failed (${status}).`
  return 'The request failed.'
}
