// Everything the UI decides about PRINT MASTERS, as pure functions (MEMNG-75).
//
// One word, everywhere the user can see it: a photo's high-resolution, separately-uploaded file is
// a "print master". `rendition` and `variant_key` stay API vocabulary and never reach a label.
// (`printready` is a third thing again — the generated 4096 px photo-book rung — and is not managed
// here.)
//
// The chip state machine, the master selection and the meta formatting live here rather than in the
// components because that is what can be tested honestly: the memo card's tiles and the PhotoSwipe
// print row are DOM shells around these decisions.

import type { components } from '@/api/schema'

type Rendition = components['schemas']['RenditionItem']
type RenditionDetail = components['schemas']['RenditionDetail']

/** The default key for a master someone attaches through the UI (the ingest path's bare `print`). */
export const DEFAULT_PRINT_VARIANT_KEY = 'print'

/**
 * A rendition holding a print master. Mirrors `printready.is_print_variant` exactly — matched, not
 * prefixed, so a future `printable`/`print-proof` key is NOT mistaken for one.
 */
export function isPrintVariant(variantKey: string): boolean {
  return variantKey === 'print' || variantKey.startsWith('print:')
}

/**
 * THE print master of a photo — the one the print row shows and Replace/Remove act on.
 *
 * The first print variant in server order, which is id order (the relationship is ordered by id):
 * the same row `printready.print_rendition` picks as the photo book's source. A photo carrying both
 * `print:glossy` and `print:matte` therefore shows, replaces and prints the SAME master; choosing
 * between several is a selection feature nobody has asked for yet, and disagreeing with the backend
 * about which one is "the" master would be worse than not offering the choice.
 */
export function selectPrintMaster<T extends { variant_key: string }>(
  renditions: readonly T[] | null | undefined,
): T | null {
  return renditions?.find((r) => isPrintVariant(r.variant_key)) ?? null
}

/** The key a UI upload writes: replacing keeps the master's own key, a first attach takes `print`. */
export function printUploadVariantKey(master: { variant_key: string } | null | undefined): string {
  return master?.variant_key ?? DEFAULT_PRINT_VARIANT_KEY
}

interface PhotoLike {
  mime: string
  renditions?: readonly Rendition[]
}

/** Photos (not videos — a clip has no print master) and how many of them carry one. */
export function printMasterCount(attachments: readonly PhotoLike[] | null | undefined): {
  photos: number
  withMaster: number
} {
  const photos = (attachments ?? []).filter((a) => a.mime.startsWith('image/'))
  return {
    photos: photos.length,
    withMaster: photos.filter((a) => selectPrintMaster(a.renditions) !== null).length,
  }
}

/**
 * The memo-level badge: a COUNT, not a flag. The old per-memo 🖨 could not say which photo it meant
 * — three photos with one master looked exactly like three photos with three.
 * Null when no photo in the memo has a master (nothing to say).
 */
export function printBadgeLabel(attachments: readonly PhotoLike[] | null | undefined): string | null {
  const { photos, withMaster } = printMasterCount(attachments)
  return withMaster === 0 ? null : `🖨 ${withMaster} of ${photos}`
}

export type PrintChipKind = 'present' | 'uploading' | 'failed'

export interface PrintChip {
  kind: PrintChipKind
  label: string
  /** 0–100 while uploading; undefined otherwise. */
  progress?: number
}

/** An in-flight or failed print-master upload for one photo (see usePrintMaster's queue). */
export interface PrintUploadState {
  status: 'uploading' | 'error'
  progress: number
}

/**
 * The per-photo chip on a memo-card tile. An upload in flight (or failed) OUTRANKS the stored
 * master: while a replace is running, what the user needs to see is the replace, not the file it is
 * about to overwrite. Null = no chip at all (a photo with no master and nothing happening is not
 * worth a badge, and a video never is).
 */
export function printChipState(
  attachment: PhotoLike,
  upload?: PrintUploadState | null,
): PrintChip | null {
  if (!attachment.mime.startsWith('image/')) return null
  if (upload?.status === 'uploading') {
    return { kind: 'uploading', label: `UPLOADING ${upload.progress}%`, progress: upload.progress }
  }
  if (upload?.status === 'error') return { kind: 'failed', label: 'PRINT FAILED · RETRY' }
  return selectPrintMaster(attachment.renditions) ? { kind: 'present', label: 'PRINT' } : null
}

const SIZE_UNITS = ['bytes', 'KB', 'MB', 'GB', 'TB']

/**
 * A file size for humans, 1024-based (what a file manager shows). One decimal from KB up, so an
 * 8.2 MB master does not read as "8 MB" next to a 8.9 MB one.
 */
export function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return ''
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < SIZE_UNITS.length - 1) {
    value /= 1024
    unit += 1
  }
  return unit === 0 ? `${Math.round(value)} bytes` : `${value.toFixed(1)} ${SIZE_UNITS[unit]}`
}

/**
 * The format badge: TIFF, JPEG, DNG… Derived from the mime, falling back to the filename's
 * extension when the mime says nothing (`application/octet-stream` is what a RAW/TIFF upload from a
 * browser that does not know the type arrives as).
 */
export function formatMasterFormat(mime: string, filename?: string | null): string {
  const subtype = mime.split('/')[1] ?? ''
  const cleaned = subtype
    .replace(/^x-/, '')
    .replace(/^vnd\./, '')
    .split(/[.+-]/)
    .pop()
  if (cleaned && cleaned !== 'octet' && cleaned !== 'stream') return cleaned.toUpperCase()
  const ext = filename?.includes('.') ? filename.split('.').pop() : undefined
  return ext ? ext.toUpperCase() : 'FILE'
}

/** What the print row states about the master: `4032 × 3024 · 8.2 MB · TIFF`. */
export function printMasterMeta(master: {
  width?: number | null
  height?: number | null
  size: number
  mime: string
  filename?: string | null
}): string {
  const parts: string[] = []
  // Dimensions are measured off the file and can legitimately be unknown (an exotic TIFF/RAW the
  // server could not parse) — the row then states what it does know instead of printing "null × null".
  if (master.width && master.height) parts.push(`${master.width} × ${master.height}`)
  parts.push(formatFileSize(master.size))
  parts.push(formatMasterFormat(master.mime, master.filename))
  return parts.join(' · ')
}

/** The print master of one photo, in the shape the viewer's print row renders. */
export interface PrintMasterInfo {
  variantKey: string
  meta: string
  downloadUrl: string
  width: number | null
  height: number | null
}

/**
 * The URL to actually fetch a master from: its route plus the content revision.
 *
 * The route serves masters `immutable` with a long max-age — they are large archive files fetched
 * rarely — but a master can be REPLACED under the same variant_key, and the URL alone would then be
 * a lie: a browser that cached one before the replace would keep serving the displaced bytes for
 * the whole max-age. `rev` is the master's own content digest, so replacing mints a new cache key
 * and nothing else does. A row with no digest is left un-busted rather than given a fake version.
 */
export function renditionUrl(downloadUrl: string, rev?: string | null): string {
  if (!rev) return downloadUrl
  return `${downloadUrl}${downloadUrl.includes('?') ? '&' : '?'}v=${encodeURIComponent(rev)}`
}

export function toPrintMasterInfo(master: RenditionDetail | null | undefined): PrintMasterInfo | null {
  if (!master) return null
  return {
    variantKey: master.variant_key,
    meta: printMasterMeta(master),
    downloadUrl: renditionUrl(master.downloadUrl, master.rev),
    width: master.width,
    height: master.height,
  }
}

export type PhotoFace = 'screen' | 'print'

/** The label pair of the Screen|Print control, in order. */
export const FACE_LABELS: Record<PhotoFace, string> = { screen: 'Screen', print: 'Print' }

export type PrintRowAction = 'view' | 'download' | 'replace' | 'remove' | 'attach'

export const PRINT_ROW_LABELS: Record<PrintRowAction, string> = {
  view: 'View',
  download: 'Download',
  replace: 'Replace…', // the ellipsis promises a file picker, then a confirmation
  remove: 'Remove',
  attach: 'Attach print master…',
}

export interface PrintRowInput {
  isPhoto: boolean
  /** The master lookup has ANSWERED for this photo (`master: null` then means "has none"). */
  resolved: boolean
  mine: boolean
  master: PrintMasterInfo | null
  canAttach: boolean // the host wired an attach handler
  canRemove: boolean // …and a remove handler
}

export interface PrintRowState {
  visible: boolean
  faceToggle: boolean
  title: string
  meta: string
  actions: PrintRowAction[]
}

/**
 * What the viewer's print row says and offers — the decision, separated from the PhotoSwipe DOM
 * that draws it (which no unit test can drive: happy-dom has no zoom canvas).
 *
 * Two rules carry the weight. The row stays HIDDEN until the lookup has answered, so it never
 * flashes "not attached yet" at a photo that has a master. And the verbs are owner-gated exactly
 * like the endpoints behind them: anyone in the household may look at and download a master
 * (viewing is household-wide), only its owner may replace or remove it.
 */
export function printRowState(input: PrintRowInput): PrintRowState {
  const { isPhoto, resolved, mine, master, canAttach, canRemove } = input
  const mayAttach = mine && canAttach
  const visible = isPhoto && resolved && (master !== null || mayAttach)
  const actions: PrintRowAction[] = []
  if (master !== null) {
    actions.push('view', 'download')
    if (mayAttach) actions.push('replace')
    if (mine && canRemove) actions.push('remove')
  } else {
    actions.push('attach')
  }
  return {
    visible,
    faceToggle: isPhoto && master !== null,
    title: 'Print master',
    meta: master?.meta ?? 'Not attached yet',
    actions,
  }
}
