import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  autoMemoDate,
  captureOrder,
  resolveCivil,
  spanDays,
  type CaptureItem,
} from '@/lib/autoMemoDate'

/**
 * The Auto-date rules are held to `fixtures/auto-memo-date.json` — the ONE normative artefact the
 * backend suite and the iOS client are held to as well. Every case here comes out of that file by
 * id; nothing is restated locally, so a rule can only change in one place.
 */
interface VectorItem {
  id: string
  instant: string | null
  civil: string | null
}
interface Artefact {
  vectors: {
    autoMemoDate: {
      id: string
      zone: string
      items: VectorItem[]
      expect: { instant: string | null; approximate: boolean; span: number }
    }[]
    captureOrder: { id: string; zone: string; items: VectorItem[]; expect: string[] }[]
    span: { id: string; zone: string; instants: string[]; expect: number }[]
    civilResolution: { id: string; civil: string; zone: string; expect: string | null }[]
  }
}

const artefact: Artefact = JSON.parse(
  readFileSync(join(import.meta.dirname, '../../../../fixtures/auto-memo-date.json'), 'utf8'),
)

const asItems = (items: VectorItem[]): CaptureItem[] =>
  items.map((i) => ({
    id: i.id,
    captureState: 'done',
    captureInstant: i.instant,
    captureCivil: i.civil,
  }))

const iso = (d: Date | null): string | null =>
  d === null ? null : `${d.toISOString().slice(0, 19)}Z`

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

describe('resolveCivil DST edges, stated explicitly', () => {
  // Named here as well as in the artefact: these two are the rule ("a gap is not a candidate; an
  // overlap takes the earlier instant"), and a fixture edit must not be able to quietly drop them.
  it('rejects a civil time inside the Europe/Berlin spring-forward gap', () => {
    expect(resolveCivil('2026-03-29T02:30:00', 'Europe/Berlin')).toBeNull()
  })
  it('takes the earlier instant in the Europe/Berlin autumn overlap', () => {
    expect(iso(resolveCivil('2026-10-25T02:30:00', 'Europe/Berlin'))).toBe('2026-10-25T00:30:00Z')
  })
})

describe('pending items never settle a date or an order', () => {
  const items: CaptureItem[] = [
    { id: 'A', captureState: 'pending', captureInstant: null, captureCivil: null },
    { id: 'B', captureState: 'done', captureInstant: '2026-01-01T13:00:00Z', captureCivil: null },
  ]
  it('a pending item contributes no instant', () => {
    expect(iso(autoMemoDate(items, 'Europe/Berlin').instant)).toBe('2026-01-01T13:00:00Z')
  })
  it('a pending item sorts as undated', () => {
    expect(captureOrder(items, 'Europe/Berlin').map((i) => i.id)).toEqual(['B', 'A'])
  })
})
