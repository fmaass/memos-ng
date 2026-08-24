// Pure composer helpers — DOM-free so they're unit-tested directly.
import type { MarkdownAction } from '@/lib/markdownToolbar'

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const pad2 = (n: number) => String(n).padStart(2, '0')

/** A backdated memo's display date, e.g. "Jun 21, 2026 14:30" (full, no truncation). */
export function formatMemoDate(d: Date): string {
  return `${MONTHS[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`
}

/**
 * Whether the composer has something postable: non-empty text OR at least one finished upload.
 * A photo-only memo (no text) is valid, like old memos. Queued/uploading/errored files don't count
 * — only `done` uploads finalize into attachments on post.
 */
export function canPost(content: string, uploads: { status: string }[]): boolean {
  return content.trim().length > 0 || uploads.some((u) => u.status === 'done')
}

/** Map a composer keydown to an intent: ⌘/Ctrl+Enter = post; ⌘/Ctrl+B/I/K = a markdown action. */
export function composerKeyAction(
  e: { key: string; metaKey: boolean; ctrlKey: boolean },
): 'save' | MarkdownAction | null {
  if (!(e.metaKey || e.ctrlKey)) return null
  if (e.key === 'Enter') return 'save'
  const map: Record<string, MarkdownAction> = { b: 'bold', i: 'italic', k: 'link' }
  return map[e.key.toLowerCase()] ?? null
}

/** A queue tile as this module needs it: the pending upload's uid, and the file name to show. */
export interface StagedUpload {
  uploadUid?: string
  name: string
}

const asStrings = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : []

/**
 * The user-facing reason a memo write was refused because its uploads could not be linked — the
 * server's 409 `uploads_unlinkable` (MEMNG-113) — or null for any other error, which callers must
 * keep handling as before.
 *
 * The SPA reaches this from a composer left open past the upload expiry: the tiles still read
 * `done`, but the sessions behind them were reaped, and the post is refused whole rather than
 * silently saved without the photos. `uploads` is the queue the uids were taken from, used to name
 * the files; when it cannot name every one of them (the edit path posts from the feed, which never
 * saw the tiles) the message counts them instead of naming some and hiding the rest.
 */
export function uploadsUnlinkableMessage(
  error: unknown,
  uploads: readonly StagedUpload[] = [],
): string | null {
  const detail = (error as { detail?: unknown } | null | undefined)?.detail
  if (detail === null || typeof detail !== 'object') return null
  const body = detail as Record<string, unknown>
  if (body.code !== 'uploads_unlinkable') return null
  const uids = [...asStrings(body.missing_upload_uids), ...asStrings(body.incomplete_upload_uids)]
  if (uids.length === 0) return null
  const names = uids
    .map((uid) => uploads.find((u) => u.uploadUid === uid)?.name)
    .filter((name): name is string => Boolean(name))
  const plural = uids.length === 1 ? '' : 's'
  const what = names.length === uids.length ? names.join(', ') : `${uids.length} attachment${plural}`
  return `${what} could not be attached: the upload is no longer available. Add the file${plural} again, then post.`
}
