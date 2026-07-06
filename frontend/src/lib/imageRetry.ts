// Thumbnail load resilience.
//
// A thumbnail can fail to load two ways: a transient network blip, or a signed media URL whose
// bucket rolled while the feed sat open (the URLs are valid only ~1-2h — ASSET_URL_TTL/BUCKET —
// so a lazily-revealed image in a long-lived PWA session hits 401/403). The browser caches the
// failed response for that exact URL, so the tile stays blank.
//
// nextRetrySrc returns the URL with a fresh cache-bust param so the next <img> load re-requests it
// (fixes the transient case), up to `max` attempts; null once exhausted. An expired signature can't
// be cache-busted back to life — the signature is unchanged — so an exhausted retry is the signal
// to RE-MINT (refetch the feed for freshly-signed URLs), which shouldRemint gates at the feed level.

export const MAX_THUMB_RETRIES = 2
const THUMB_RETRY_DELAYS_MS = [1_000, 4_000] as const

// `attempt` is how many retries have already been made (0 on the first failure). The cache-bust key
// is `_r`; signed URLs already carry ?exp&scope&sig, and the serve route reads only those, so an
// extra param is ignored by signature verification but still forces a fresh request.
export function nextRetrySrc(url: string, attempt: number, max = MAX_THUMB_RETRIES): string | null {
  if (attempt >= max) return null
  const sep = url.includes('?') ? '&' : '?'
  return `${url}${sep}_r=${attempt + 1}`
}

export function retryDelayMs(attempt: number, max = MAX_THUMB_RETRIES): number | null {
  if (attempt >= max) return null
  return THUMB_RETRY_DELAYS_MS[attempt] ?? THUMB_RETRY_DELAYS_MS[THUMB_RETRY_DELAYS_MS.length - 1]
}

export const REMINT_COOLDOWN_MS = 30_000

// Whether a feed-level re-mint (timeline refetch) should fire now. Not while one is already in
// flight (concurrent tile failures coalesce), and at most once per cooldown window — so a steady
// stream of failing tiles during an outage triggers one refetch per window, not a refetch loop
// after each request settles. Pure: the caller supplies the clock and the last-fire time.
export function shouldRemint(
  now: number,
  lastRemint: number,
  isFetching: boolean,
  cooldownMs = REMINT_COOLDOWN_MS,
): boolean {
  if (isFetching) return false
  return now - lastRemint >= cooldownMs
}
