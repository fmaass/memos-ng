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
