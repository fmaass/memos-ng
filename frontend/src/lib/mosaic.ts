// The Months view's day mosaic (plan decision 9): per month, the busiest days only, laid out in a
// fixed cycle of frame patterns. Pure, so the layout and the pick are tested without a DOM.

// A slot names where a tile sits in the 3-column grid; DayMosaic maps each to its span.
export type MosaicSlot = 'hero-left' | 'side-right' | 'square' | 'hero-right' | 'side-left' | 'wide'

export interface DayBucket {
  /** `YYYY-MM-DD`, the Zurich calendar day the server bucketed (app/services/attachments.py). */
  key: string
  count: number
}

// A: hero left (2×2) with two squares stacked right; B: three squares; C: hero right (2×2) with two
// squares stacked left; D: one full-width 16:9 hero. Each pattern names its hero first, so a pattern
// cut short still leads with its biggest frame.
const PATTERNS: readonly (readonly MosaicSlot[])[] = [
  ['hero-left', 'side-right', 'side-right'],
  ['square', 'square', 'square'],
  ['hero-right', 'side-left', 'side-left'],
  ['wide'],
]

// The cycle starts at the pattern `offset` names (the section index, so consecutive months open on
// different frames) and the last pattern keeps only the slots there are tiles for.
export function mosaicSlots(n: number, offset = 0): MosaicSlot[] {
  const out: MosaicSlot[] = []
  let p = ((offset % PATTERNS.length) + PATTERNS.length) % PATTERNS.length
  while (out.length < n) {
    for (const slot of PATTERNS[p]!) {
      if (out.length === n) break
      out.push(slot)
    }
    p = (p + 1) % PATTERNS.length
  }
  return out
}

// The All view's day blocks (plan decision 13 and its 20:05 amendment) walk the same patterns in the
// order A B C B D, and a day starts one step later than the day before it (`dayIndex`, counted
// across month boundaries), so neighbouring days never open alike. A pattern is placed whole or
// not at all: a remainder too short for the next one is squares, a single leftover one wide frame,
// and a day of one or two pictures is wide frames only.
const DAY_CYCLE: readonly (readonly MosaicSlot[])[] = [PATTERNS[0]!, PATTERNS[1]!, PATTERNS[2]!, PATTERNS[1]!, PATTERNS[3]!]

export function daySlots(n: number, dayIndex = 0): MosaicSlot[] {
  if (n <= 2) return Array.from({ length: Math.max(n, 0) }, () => 'wide' as const)
  const out: MosaicSlot[] = []
  let p = ((dayIndex % DAY_CYCLE.length) + DAY_CYCLE.length) % DAY_CYCLE.length
  while (out.length < n) {
    const pattern = DAY_CYCLE[p]!
    const left = n - out.length
    if (left < pattern.length) {
      out.push(...(left === 1 ? (['wide'] as const) : Array.from({ length: left }, () => 'square' as const)))
      break
    }
    out.push(...pattern)
    p = (p + 1) % DAY_CYCLE.length
  }
  return out
}

const byDate = (a: DayBucket, b: DayBucket) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0)

// The `cap` days with the most items; a tie goes to the later day (keys are ISO dates, so string
// order IS date order). Shown in date order, with the number of days left out for the "and N more
// days" line.
export function pickDays<T extends DayBucket>(
  days: readonly T[],
  cap = 6,
): { shown: T[]; dropped: number } {
  const kept = [...days].sort((a, b) => b.count - a.count || byDate(b, a)).slice(0, cap)
  return { shown: kept.sort(byDate), dropped: days.length - kept.length }
}

// The year → months filter. The server's keys are already Zurich calendar strings, so a prefix
// test on the year field is exact; a Date parse would re-interpret them in the browser's zone.
export function daysOfYear<T extends DayBucket>(days: readonly T[], year: string): T[] {
  return days.filter((d) => d.key.startsWith(`${year}-`))
}

export interface MosaicSection<T> {
  /** `YYYY-MM` */
  key: string
  /** "Sep 2026" */
  label: string
  shown: T[]
  dropped: number
}

const monthLabel = new Intl.DateTimeFormat('en', { month: 'short', year: 'numeric', timeZone: 'UTC' })

// Day buckets grouped by their `YYYY-MM` prefix, months in the order they came (the server's is
// newest first), each reduced by pickDays. The label formats a UTC noon of that calendar month in
// UTC — a pure calendar date, so no zone can move it to a neighbouring month.
export function mosaicSections<T extends DayBucket>(days: readonly T[], cap = 6): MosaicSection<T>[] {
  const byMonth = new Map<string, T[]>()
  for (const d of days) {
    const key = d.key.slice(0, 7)
    const list = byMonth.get(key)
    if (list) list.push(d)
    else byMonth.set(key, [d])
  }
  return [...byMonth].map(([key, list]) => {
    const [year, month] = key.split('-').map(Number)
    const label = monthLabel.format(new Date(Date.UTC(year!, month! - 1, 1, 12)))
    return { key, label, ...pickDays(list, cap) }
  })
}
