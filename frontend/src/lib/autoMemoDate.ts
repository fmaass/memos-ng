// The Auto memo date and the capture order — pure, DOM-free, and held to the ONE normative
// artefact `fixtures/auto-memo-date.json` (MEMNG-262), which pytest and the iOS client read too.
//
// Two ideas carry the whole module:
//
//  * an EXACT instant (EXIF with an offset, a QuickTime creationdate) and an APPROXIMATE one (EXIF
//    without an offset, resolved here in the browser's zone) compete on equal terms — on their
//    resolved epoch seconds. The only trace of the difference is the "≈" the composer shows when
//    the winner was a resolved civil time;
//  * an item whose capture decision has not SETTLED contributes nothing, and neither does one the
//    file had no date for. Both sort after the dated items, in pick order.
//
// Zone maths goes through Intl — no date library is added for it (the SPA has none).

export type CaptureState = 'pending' | 'done'

/** A composer tile as this module needs it: an identity, a decision, and what the file said.
 *  The field names are the composer queue's own (`useResumableUpload`), so an `UploadItem` IS a
 *  `CaptureItem` and neither side needs an adapter that could drift. */
export interface CaptureItem {
  id: number | string
  captureState: CaptureState
  captureInstant: string | null
  captureCivil: string | null
}

export interface AutoDate {
  /** The earliest eligible capture instant, or null when nothing eligible was attached. */
  instant: Date | null
  /** The winner came from a civil time this browser resolved — the composer marks it "≈". */
  approximate: boolean
  /** Inclusive calendar-day count when the earliest and latest fall on different days, else 0. */
  span: number
}

const SECOND = 1000
const DAY = 86_400_000

const floorToSecond = (ms: number): number => Math.floor(ms / SECOND) * SECOND

const CIVIL = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?$/

interface Wall {
  year: number
  month: number
  day: number
  hour: number
  minute: number
  second: number
}

const formatters = new Map<string, Intl.DateTimeFormat>()

function formatterFor(zone: string): Intl.DateTimeFormat {
  let dtf = formatters.get(zone)
  if (!dtf) {
    // hourCycle h23, not hour12:false — some runtimes render midnight as "24" under the latter.
    dtf = new Intl.DateTimeFormat('en-US', {
      timeZone: zone,
      hourCycle: 'h23',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    })
    formatters.set(zone, dtf)
  }
  return dtf
}

/** The wall-clock reading `zone` shows at instant `ms`. */
function wallAt(ms: number, zone: string): Wall {
  const parts = formatterFor(zone).formatToParts(new Date(ms))
  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value ?? '0')
  return {
    year: get('year'),
    month: get('month'),
    day: get('day'),
    hour: get('hour'),
    minute: get('minute'),
    second: get('second'),
  }
}

const asPseudoUtc = (w: Wall): number =>
  Date.UTC(w.year, w.month - 1, w.day, w.hour, w.minute, w.second)

/**
 * The instant a wall-clock reading names in `zone`, or null when it names none.
 *
 * Guess, then VERIFY: apply the zone's offset (read on both sides of the target, see below) and
 * format the candidate back — a candidate that does not read back as the civil time asked for is
 * not that civil time. What survives decides the two edges the rules name:
 * a spring-forward GAP leaves no surviving candidate (null — the item counts as undated), and an
 * autumn OVERLAP leaves two, of which the EARLIER wins.
 */
export function resolveCivil(civil: string, zone: string): Date | null {
  const m = CIVIL.exec(civil.trim())
  if (!m) return null
  const wall: Wall = {
    year: Number(m[1]),
    month: Number(m[2]),
    day: Number(m[3]),
    hour: Number(m[4]),
    minute: Number(m[5]),
    second: Number(m[6] ?? '0'),
  }
  const target = asPseudoUtc(wall)
  if (Number.isNaN(target)) return null
  // Probe the zone's offset a day either side of the target as well as at it: around a DST
  // transition the two sides disagree, and it is exactly that disagreement that produces the two
  // candidates an overlap has (a single probe converges on one of them and would silently drop
  // the earlier). Every candidate is then VERIFIED by formatting it back.
  const candidates: number[] = []
  for (const probe of [target - DAY, target, target + DAY]) {
    const guess = target - (asPseudoUtc(wallAt(probe, zone)) - probe)
    if (asPseudoUtc(wallAt(guess, zone)) === target && !candidates.includes(guess)) {
      candidates.push(guess)
    }
  }
  if (candidates.length === 0) return null // the civil time falls in a DST gap
  return new Date(floorToSecond(Math.min(...candidates)))
}

/** The zone's calendar day of an instant, as days since the epoch (comparable, not displayable). */
function calendarDay(ms: number, zone: string): number {
  const w = wallAt(ms, zone)
  return Math.floor(Date.UTC(w.year, w.month - 1, w.day) / DAY)
}

/**
 * The inclusive calendar-day count the instants cover in `zone` — 0 when they all fall on one day.
 *
 * There is no threshold: two instants twenty minutes apart across midnight span two days, and two
 * nine hours apart on one afternoon span none. The hint is about the CALENDAR, not the duration.
 */
export function spanDays(instants: readonly Date[], zone: string): number {
  if (instants.length < 2) return 0
  const days = instants.map((d) => calendarDay(d.getTime(), zone))
  const first = Math.min(...days)
  const last = Math.max(...days)
  return last === first ? 0 : last - first + 1
}

interface Settled {
  instant: Date | null
  approximate: boolean
}

/**
 * One item's eligible instant, or null. A `pending` item has no decision yet and so has no
 * instant — which is what makes "settle before posting" the only way the date can be right.
 */
export function settledInstant(item: CaptureItem, zone: string): Settled {
  if (item.captureState !== 'done') return { instant: null, approximate: false }
  if (item.captureInstant) {
    const exact = new Date(item.captureInstant)
    if (!Number.isNaN(exact.getTime())) {
      return { instant: new Date(floorToSecond(exact.getTime())), approximate: false }
    }
  }
  if (item.captureCivil) {
    const resolved = resolveCivil(item.captureCivil, zone)
    if (resolved) return { instant: resolved, approximate: true }
  }
  return { instant: null, approximate: false }
}

/**
 * The memo's Auto date: the EARLIEST eligible capture instant among the attached media, with the
 * span hint and the approximation flag the chip reads.
 *
 * A tie is decided by pick order (the first item that reaches the minimum keeps it), so the answer
 * does not depend on the comparison's direction. Nothing is averaged, and no fallback is invented:
 * with no eligible instant the answer is null, and the composer posts no `created_at` at all.
 */
export function autoMemoDate(items: readonly CaptureItem[], zone: string): AutoDate {
  const settled: Settled[] = []
  for (const item of items) {
    const one = settledInstant(item, zone)
    if (one.instant) settled.push(one)
  }
  if (settled.length === 0) return { instant: null, approximate: false, span: 0 }
  let winner = settled[0]
  for (const candidate of settled) {
    if (candidate.instant!.getTime() < winner.instant!.getTime()) winner = candidate
  }
  return {
    instant: winner.instant,
    approximate: winner.approximate,
    span: spanDays(
      settled.map((s) => s.instant!),
      zone,
    ),
  }
}

/**
 * The items in CAPTURE ORDER: everything with a settled instant first, ascending; everything
 * without one after it, in pick order; ties in pick order.
 *
 * This is what the composer shows AND what it sends — `upload_uids` in this order is what the
 * server persists as each attachment's `position`, so the memo reads back the way it looked.
 */
export function captureOrder<T extends CaptureItem>(items: readonly T[], zone: string): T[] {
  const dated: { item: T; at: number; pick: number }[] = []
  const undated: T[] = []
  items.forEach((item, pick) => {
    const { instant } = settledInstant(item, zone)
    if (instant) dated.push({ item, at: instant.getTime(), pick })
    else undated.push(item)
  })
  dated.sort((a, b) => a.at - b.at || a.pick - b.pick)
  return [...dated.map((d) => d.item), ...undated]
}

/** Seconds precision on the wire, the same as the server renders and the iOS client sends. */
export function toWireInstant(date: Date): string {
  return `${new Date(floorToSecond(date.getTime())).toISOString().slice(0, 19)}Z`
}
