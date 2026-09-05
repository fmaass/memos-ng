import { describe, expect, it } from 'vitest'

import {
  autoMemoDate,
  captureOrder,
  resolveCivil,
  spanDays,
  type CaptureItem,
} from '@/lib/autoMemoDate'

import { artefact, instantOf, type VectorItem } from './artefact'

/**
 * The Auto-date rules are held to `fixtures/auto-memo-date.json` — the ONE normative artefact the
 * backend suite and the iOS client are held to as well. Every case here comes out of that file by
 * id (see ./artefact); nothing is restated locally, so a rule can only change in one place.
 */

const asItems = (items: VectorItem[]): CaptureItem[] =>
  items.map((i) => ({
    id: i.id,
    captureState: 'done',
    captureInstant: i.instant,
    captureCivil: i.civil,
  }))

/**
 * Render for comparison — after asserting the precision on the RAW value. Slicing the ISO string
 * would floor sub-second digits by itself, so a formatter-only assertion passes even if the
 * production flooring is deleted; the millisecond check is what actually holds the rule.
 */
const iso = (d: Date | null): string | null => {
  if (d === null) return null
  expect(d.getMilliseconds()).toBe(0) // whole seconds, before any formatting
  return `${d.toISOString().slice(0, 19)}Z`
}

describe('autoMemoDate (fixture vectors)', () => {
  for (const v of artefact.vectors.autoMemoDate) {
    it(v.id, () => {
      const got = autoMemoDate(asItems(v.items), v.zone)
      expect(iso(got.instant)).toBe(v.expect.instant)
      expect(got.approximate).toBe(v.expect.approximate)
      expect(got.span).toBe(v.expect.span)
    })
  }
})

describe('captureOrder (fixture vectors)', () => {
  for (const v of artefact.vectors.captureOrder) {
    it(v.id, () => {
      expect(captureOrder(asItems(v.items), v.zone).map((i) => i.id)).toEqual(v.expect)
    })
  }
})

describe('spanDays (fixture vectors)', () => {
  for (const v of artefact.vectors.span) {
    it(v.id, () => {
      expect(spanDays(v.instants.map((s) => new Date(s)), v.zone)).toBe(v.expect)
    })
  }
})

describe('resolveCivil (fixture vectors)', () => {
  for (const v of artefact.vectors.civilResolution) {
    it(v.id, () => {
      expect(iso(resolveCivil(v.civil, v.zone))).toBe(v.expect)
    })
  }
})

describe('pending items never settle a date or an order', () => {
  // The dated item is the artefact's own "single-exact" vector; only its STATE is the subject here.
  const dated = instantOf('single-exact', 'A')
  const items: CaptureItem[] = [
    { id: 'A', captureState: 'pending', captureInstant: null, captureCivil: null },
    { id: 'B', captureState: 'done', captureInstant: dated, captureCivil: null },
  ]
  it('a pending item contributes no instant', () => {
    expect(iso(autoMemoDate(items, 'Europe/Berlin').instant)).toBe(dated)
  })
  it('a pending item sorts as undated', () => {
    expect(captureOrder(items, 'Europe/Berlin').map((i) => i.id)).toEqual(['B', 'A'])
  })
})
