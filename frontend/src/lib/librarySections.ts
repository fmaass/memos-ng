// The All view's month sections. A month is a CALENDAR month in `tz` (default Europe/Zurich,
// estate ADR-0007), read through Intl with that zone: a UTC slice of the instant (toISOString, as
// lib/timeline.ts does for days) would file a late-evening photo on the last of a month — and
// every New Year's Eve photo after 23:00 — under the month before.
export interface MonthSection<T> {
  /** `YYYY-MM` in `tz` — the same key the buckets endpoint returns for a month. */
  key: string
  label: string
  items: T[]
}

export const LIBRARY_TIME_ZONE = 'Europe/Zurich'
export const UNDATED_KEY = 'undated'

const partsFormats = new Map<string, Intl.DateTimeFormat>()
const labelFormats = new Map<string, Intl.DateTimeFormat>()

function cached(
  cache: Map<string, Intl.DateTimeFormat>,
  tz: string,
  options: Intl.DateTimeFormatOptions,
): Intl.DateTimeFormat {
  let fmt = cache.get(tz)
  if (!fmt) {
    fmt = new Intl.DateTimeFormat('en', { ...options, timeZone: tz })
    cache.set(tz, fmt)
  }
  return fmt
}

function monthKey(instant: Date, tz: string): string {
  const parts = cached(partsFormats, tz, { year: 'numeric', month: '2-digit' }).formatToParts(instant)
  const year = parts.find((p) => p.type === 'year')?.value
  const month = parts.find((p) => p.type === 'month')?.value
  return `${year}-${month}`
}

const dayFormats = new Map<string, Intl.DateTimeFormat>()

/** The `tz` calendar day of an ISO instant as `YYYY-MM-DD` — the buckets endpoint's day key — or
 * null for an instant that cannot be read. */
export function dayKey(takenAt: string, tz: string = LIBRARY_TIME_ZONE): string | null {
  const instant = new Date(takenAt)
  if (Number.isNaN(instant.getTime())) return null
  const parts = cached(dayFormats, tz, { year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(
    instant,
  )
  const part = (type: string) => parts.find((p) => p.type === type)?.value
  return `${part('year')}-${part('month')}-${part('day')}`
}

// Sections follow the list's own order (the server sorts by takenAt in either direction), so the
// grid never re-sorts what it was handed; a month's items keep their list order too.
export function groupByMonth<T extends { takenAt: string }>(
  items: T[],
  tz: string = LIBRARY_TIME_ZONE,
): MonthSection<T>[] {
  const sections: MonthSection<T>[] = []
  const byKey = new Map<string, MonthSection<T>>()
  for (const item of items) {
    const instant = new Date(item.takenAt)
    // Intl throws on an invalid date, and one unreadable takenAt must not take the grid down with
    // it: such an item is shown, under its own section, rather than lost or fatal.
    const valid = !Number.isNaN(instant.getTime())
    const key = valid ? monthKey(instant, tz) : UNDATED_KEY
    let section = byKey.get(key)
    if (!section) {
      const label = valid
        ? cached(labelFormats, tz, { year: 'numeric', month: 'long' }).format(instant)
        : 'Undated'
      section = { key, label, items: [] }
      byKey.set(key, section)
      sections.push(section)
    }
    section.items.push(item)
  }
  return sections
}

// The All view's day groups (plan decision 13): each `tz` calendar day of the list is one group,
// in the list's own order, like groupByMonth; an unreadable takenAt keeps a group of its own.
export interface DaySection<T> {
  /** `YYYY-MM-DD` in `tz`, or UNDATED_KEY. */
  key: string
  items: T[]
}

export function groupByDay<T extends { takenAt: string }>(
  items: T[],
  tz: string = LIBRARY_TIME_ZONE,
): DaySection<T>[] {
  const sections: DaySection<T>[] = []
  const byKey = new Map<string, DaySection<T>>()
  for (const item of items) {
    const key = dayKey(item.takenAt, tz) ?? UNDATED_KEY
    let section = byKey.get(key)
    if (!section) {
      section = { key, items: [] }
      byKey.set(key, section)
      sections.push(section)
    }
    section.items.push(item)
  }
  return sections
}

// A day key is already a `tz` calendar date, so it is formatted as a UTC noon in UTC — a pure
// calendar date no zone can move (as mosaicSections does for months). The parts are assembled by
// hand because en-GB abbreviates September "Sept" and en orders the month first.
const dayLabelFormat = new Intl.DateTimeFormat('en', {
  weekday: 'short',
  day: 'numeric',
  month: 'short',
  year: 'numeric',
  timeZone: 'UTC',
})

function dayLabelParts(key: string): Record<'weekday' | 'day' | 'month' | 'year', string> | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(key)
  if (!match) return null
  const [year, month, day] = match.slice(1).map(Number) as [number, number, number]
  const date = new Date(Date.UTC(year, month - 1, day, 12))
  // Date.UTC rolls 30 February over into March; a key that names no real day is not a day.
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return null
  const parts = dayLabelFormat.formatToParts(date)
  const part = (type: string) => parts.find((p) => p.type === type)?.value ?? ''
  return { weekday: part('weekday'), day: part('day'), month: part('month'), year: part('year') }
}

/** The day heading row: `{ weekday: 'Sat', date: '26 Sep' }`. */
export function dayHeadingParts(key: string): { weekday: string; date: string } {
  const p = dayLabelParts(key)
  return p ? { weekday: p.weekday, date: `${p.day} ${p.month}` } : { weekday: 'Undated', date: '' }
}

/** The scroll pill: `'Sat · 26 Sep 2026'`. */
export function dayPillLabel(key: string): string {
  const p = dayLabelParts(key)
  return p ? `${p.weekday} · ${p.day} ${p.month} ${p.year}` : 'Undated'
}

// The day the scroll pill names: the last heading, in document order, whose top has reached the
// threshold line; null while none has (the page is at its top and the pill stays hidden).
export function topDay(headings: readonly { key: string; top: number }[], threshold: number): string | null {
  let found: string | null = null
  for (const h of headings) {
    if (h.top <= threshold) found = h.key
  }
  return found
}
