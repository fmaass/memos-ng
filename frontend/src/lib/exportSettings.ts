// Pure logic behind the Export tab (MEMNG-239). Everything a component would otherwise decide
// inline lives here and is unit-tested directly, per the repo's frontend rule: which profile fields
// a draft changed, whether a draft is legal, what the PUT body is, and what the "how to query"
// command reads. Nothing in this file talks to the network — that is `useExportSettings.ts`, and
// only that.
//
// THE ROW IS HAND-DECLARED, DELIBERATELY. `GET/PUT /api/admin/export/settings` is not in
// `openapi.json` yet (it lands in a sibling change), so `ExportSettings` and `ExportSettingsPut`
// below mirror the agreed column set by hand. When the endpoint reaches the schema, these two
// aliases become `components['schemas'][…]` from '@/api/schema' and nothing else here moves.

// `isCalendarDate` is the Mail tab's, unchanged and reused rather than re-written: one answer to
// "is this a day that exists" is the only way both tabs can stay agreed on it.
import { isCalendarDate } from '@/lib/digest'

// --- the row --------------------------------------------------------------------------------

/** How a print-quality JPEG is derived. `0` for the long edge means "the master's own size". */
export type PrintLongEdge = 4096 | 5000 | 0
export type MetadataPolicy = 'strip' | 'keep_dates'
/** The digest's inline CID images. 1200/1600/2048 px are the three sizes the design names:
 * 2× a 600 px and an 800 px rendered width, and the 2048 px `mobile` tier the digest embeds today. */
export type MailLongEdge = 1200 | 1600 | 2048
export type Subsampling = '4:2:0' | '4:4:4'

/** The whole settings row as the API answers it. */
export interface ExportSettings {
  print_long_edge: PrintLongEdge
  print_jpeg_quality: number
  print_metadata_policy: MetadataPolicy
  print_grayscale_single_channel: boolean
  mail_long_edge: MailLongEdge
  mail_jpeg_quality: number
  mail_subsampling: Subsampling
  mail_progressive: boolean
  updated_at: string
  updated_by: string | null
}

/** The whole settings row as the API takes it back. PUT replaces everything, so this is every
 * writable column and not a patch — the two Save bars each build one from the freshest server row. */
export interface ExportSettingsPut {
  print_long_edge: PrintLongEdge
  print_jpeg_quality: number
  print_metadata_policy: MetadataPolicy
  print_grayscale_single_channel: boolean
  mail_long_edge: MailLongEdge
  mail_jpeg_quality: number
  mail_subsampling: Subsampling
  mail_progressive: boolean
}

// --- the two drafts -------------------------------------------------------------------------

export interface PrintProfileDraft {
  print_long_edge: PrintLongEdge
  print_jpeg_quality: number
  print_metadata_policy: MetadataPolicy
  print_grayscale_single_channel: boolean
}

export interface MailProfileDraft {
  mail_long_edge: MailLongEdge
  mail_jpeg_quality: number
  mail_subsampling: Subsampling
  mail_progressive: boolean
}

export const PRINT_FIELDS = [
  'print_long_edge',
  'print_jpeg_quality',
  'print_metadata_policy',
  'print_grayscale_single_channel',
] as const satisfies readonly (keyof PrintProfileDraft)[]

export const MAIL_FIELDS = [
  'mail_long_edge',
  'mail_jpeg_quality',
  'mail_subsampling',
  'mail_progressive',
] as const satisfies readonly (keyof MailProfileDraft)[]

export function printDraftFromSettings(settings: ExportSettings): PrintProfileDraft {
  return {
    print_long_edge: settings.print_long_edge,
    print_jpeg_quality: settings.print_jpeg_quality,
    print_metadata_policy: settings.print_metadata_policy,
    print_grayscale_single_channel: settings.print_grayscale_single_channel,
  }
}

export function mailDraftFromSettings(settings: ExportSettings): MailProfileDraft {
  return {
    mail_long_edge: settings.mail_long_edge,
    mail_jpeg_quality: settings.mail_jpeg_quality,
    mail_subsampling: settings.mail_subsampling,
    mail_progressive: settings.mail_progressive,
  }
}

/** Which Print fields differ from the server's row — the Print Save/Discard bar's whole condition. */
export function dirtyPrintFields(
  draft: PrintProfileDraft,
  settings: ExportSettings,
): (keyof PrintProfileDraft)[] {
  const server = printDraftFromSettings(settings)
  return PRINT_FIELDS.filter((field) => draft[field] !== server[field])
}

export function dirtyMailFields(
  draft: MailProfileDraft,
  settings: ExportSettings,
): (keyof MailProfileDraft)[] {
  const server = mailDraftFromSettings(settings)
  return MAIL_FIELDS.filter((field) => draft[field] !== server[field])
}

/** The PUT body for a Print save: the WHOLE row, with every Mail field taken from the server's
 * current values. A Mail edit the admin has not saved cannot ride along on the Print bar, and the
 * mirror of that is true of `putBodyFromMailDraft`. */
export function putBodyFromPrintDraft(
  draft: PrintProfileDraft,
  current: ExportSettings,
): ExportSettingsPut {
  return { ...mailDraftFromSettings(current), ...draft }
}

export function putBodyFromMailDraft(
  draft: MailProfileDraft,
  current: ExportSettings,
): ExportSettingsPut {
  return { ...printDraftFromSettings(current), ...draft }
}

// --- validating a draft ---------------------------------------------------------------------

export type PrintErrors = Partial<Record<keyof PrintProfileDraft, string>>
export type MailErrors = Partial<Record<keyof MailProfileDraft, string>>

/** Long edge 4096 keeps the existing print cache valid; 5000 is Blurb's published full-bleed rule;
 * 0 hands over the master's own size. (Design /link/3291, decision 2.) */
export const PRINT_LONG_EDGE_OPTIONS = [
  { label: '4096 px', value: 4096 as PrintLongEdge },
  { label: '5000 px', value: 5000 as PrintLongEdge },
  { label: 'Original', value: 0 as PrintLongEdge },
]

/** 90–95 is the vendor-documented band; 100 is weight without visible gain. */
export const PRINT_QUALITY_RANGE = [90, 95] as const

export const METADATA_POLICY_OPTIONS = [
  { label: 'Strip', value: 'strip' as MetadataPolicy },
  { label: 'Keep dates', value: 'keep_dates' as MetadataPolicy },
]

export const MAIL_LONG_EDGE_OPTIONS = [
  { label: '1200 px', value: 1200 as MailLongEdge },
  { label: '1600 px', value: 1600 as MailLongEdge },
  { label: '2048 px', value: 2048 as MailLongEdge },
]

/** q≈80 (77–83) is the documented target for inline mail images. */
export const MAIL_QUALITY_RANGE = [77, 83] as const

export const SUBSAMPLING_OPTIONS = [
  { label: '4:2:0', value: '4:2:0' as Subsampling },
  { label: '4:4:4', value: '4:4:4' as Subsampling },
]

function inRange(value: number, [min, max]: readonly [number, number]): string | null {
  if (!Number.isInteger(value)) return 'A whole number is required.'
  if (value < min || value > max) return `Between ${min} and ${max}.`
  return null
}

const PRINT_LONG_EDGES: readonly number[] = PRINT_LONG_EDGE_OPTIONS.map((o) => o.value)
const METADATA_POLICIES: readonly string[] = METADATA_POLICY_OPTIONS.map((o) => o.value)
const MAIL_LONG_EDGES: readonly number[] = MAIL_LONG_EDGE_OPTIONS.map((o) => o.value)
const SUBSAMPLINGS: readonly string[] = SUBSAMPLING_OPTIONS.map((o) => o.value)

/** Every reason this Print draft cannot be saved, keyed by the field that carries the message.
 * The radios are checked as well as the number: a value the page never offered can still arrive
 * through a stale draft or a hand-edited request, and a settings row that names a size the derivation
 * cannot make would silently produce nothing. */
export function validatePrintDraft(draft: PrintProfileDraft): PrintErrors {
  const errors: PrintErrors = {}

  if (!PRINT_LONG_EDGES.includes(draft.print_long_edge))
    errors.print_long_edge = 'Choose 4096 px, 5000 px or the original size.'

  const quality = inRange(draft.print_jpeg_quality, PRINT_QUALITY_RANGE)
  if (quality) errors.print_jpeg_quality = quality

  if (!METADATA_POLICIES.includes(draft.print_metadata_policy))
    errors.print_metadata_policy = 'Choose whether to strip metadata or keep the dates.'

  if (typeof draft.print_grayscale_single_channel !== 'boolean')
    errors.print_grayscale_single_channel = 'This is on or off.'

  return errors
}

export function validateMailDraft(draft: MailProfileDraft): MailErrors {
  const errors: MailErrors = {}

  if (!MAIL_LONG_EDGES.includes(draft.mail_long_edge))
    errors.mail_long_edge = 'Choose 1200 px, 1600 px or 2048 px.'

  const quality = inRange(draft.mail_jpeg_quality, MAIL_QUALITY_RANGE)
  if (quality) errors.mail_jpeg_quality = quality

  if (!SUBSAMPLINGS.includes(draft.mail_subsampling))
    errors.mail_subsampling = 'Choose 4:2:0 or 4:4:4.'

  if (typeof draft.mail_progressive !== 'boolean') errors.mail_progressive = 'This is on or off.'

  return errors
}

// --- how to query ---------------------------------------------------------------------------

/** What the Print profile currently means, in one line, for the "how to query" block: the export
 * carries these settings, so a command shown without them would be telling half the truth. */
export function describePrintProfile(settings: ExportSettings): string {
  const size =
    settings.print_long_edge === 0
      ? 'the original size'
      : `${settings.print_long_edge} px on the long edge`
  const metadata =
    settings.print_metadata_policy === 'strip' ? 'metadata stripped' : 'capture dates kept'
  const mono = settings.print_grayscale_single_channel
    ? 'neutral monochrome as single-channel'
    : 'every image in colour'
  return `${size}, JPEG quality ${settings.print_jpeg_quality}, ${metadata}, ${mono}`
}

export interface ExportQuery {
  from: string
  to: string
}

/** What the two date fields start on: the last 30 days, ending today, in the reader's own local
 * time. A UTC day would be the wrong day for anyone west of it for part of every evening, and the
 * command it produces would quietly miss a memo. */
export function defaultExportQuery(now: Date): ExportQuery {
  const to = localCalendarDate(now)
  const from = localCalendarDate(new Date(now.getTime() - 29 * 86400000))
  return { from, to }
}

function localCalendarDate(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

/** The read-only command the block shows. No token is ever interpolated: `$MEMOS_PAT` stays a shell
 * variable so the line can be copied into a terminal, and pasted into a message, without carrying a
 * secret with it. */
export function exportCurlCommand(query: ExportQuery, origin: string): string {
  const base = origin.replace(/\/+$/, '')
  return [
    'curl -L -H "Authorization: Bearer $MEMOS_PAT" \\',
    `  "${base}/api/exports/photobook?from=${query.from}&to=${query.to}&format=zip" \\`,
    `  -o photobook-${query.from}_${query.to}.zip`,
  ].join('\n')
}

/** Is this `YYYY-MM-DD` a day that exists, and does the range run forwards? The block is read-only,
 * but the two dates are typed, and a command with a backwards range would be copied out broken. */
export function validateExportQuery(query: ExportQuery): Partial<Record<keyof ExportQuery, string>> {
  const errors: Partial<Record<keyof ExportQuery, string>> = {}
  if (!isCalendarDate(query.from)) errors.from = 'Use a real date, e.g. 2026-06-01.'
  if (!isCalendarDate(query.to)) errors.to = 'Use a real date, e.g. 2026-06-30.'
  if (!errors.from && !errors.to && query.from > query.to)
    errors.to = 'The end of the range comes before its start.'
  return errors
}
