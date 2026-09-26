import { describe, expect, it } from 'vitest'

import { daysOfYear, mosaicSections, type MosaicSlot, mosaicSlots, pickDays } from '@/lib/mosaic'

const A: MosaicSlot[] = ['hero-left', 'side-right', 'side-right']
const B: MosaicSlot[] = ['square', 'square', 'square']
const C: MosaicSlot[] = ['hero-right', 'side-left', 'side-left']
const D: MosaicSlot[] = ['wide']

describe('mosaicSlots', () => {
  // Written out tile by tile rather than derived from the patterns, so a wrong cycle cannot pass by
  // agreeing with itself.
  const expected: MosaicSlot[][] = [
    [],
    ['hero-left'],
    ['hero-left', 'side-right'],
    ['hero-left', 'side-right', 'side-right'],
    ['hero-left', 'side-right', 'side-right', 'square'],
    ['hero-left', 'side-right', 'side-right', 'square', 'square'],
    ['hero-left', 'side-right', 'side-right', 'square', 'square', 'square'],
    ['hero-left', 'side-right', 'side-right', 'square', 'square', 'square', 'hero-right'],
    ['hero-left', 'side-right', 'side-right', 'square', 'square', 'square', 'hero-right', 'side-left'],
    [
      'hero-left', 'side-right', 'side-right', 'square', 'square', 'square',
      'hero-right', 'side-left', 'side-left',
    ],
    [
      'hero-left', 'side-right', 'side-right', 'square', 'square', 'square',
      'hero-right', 'side-left', 'side-left', 'wide',
    ],
    [
      'hero-left', 'side-right', 'side-right', 'square', 'square', 'square',
      'hero-right', 'side-left', 'side-left', 'wide', 'hero-left',
    ],
    [
      'hero-left', 'side-right', 'side-right', 'square', 'square', 'square',
      'hero-right', 'side-left', 'side-left', 'wide', 'hero-left', 'side-right',
    ],
  ]
  for (let n = 0; n <= 12; n++) {
    it(`lays out ${n} tile(s) as A, B, C, D cycling, a partial pattern cut at its last filled slot`, () => {
      expect(mosaicSlots(n)).toEqual(expected[n])
    })
  }

  it('reaches D at tile 10 and restarts the cycle at tile 11', () => {
    const slots = mosaicSlots(11)
    expect(slots[9]).toBe('wide')
    expect(slots[10]).toBe('hero-left')
  })

  it('starts the cycle at the pattern the offset names, 0 through 3', () => {
    expect(mosaicSlots(10, 0)).toEqual([...A, ...B, ...C, ...D])
    expect(mosaicSlots(10, 1)).toEqual([...B, ...C, ...D, ...A])
    expect(mosaicSlots(10, 2)).toEqual([...C, ...D, ...A, ...B])
    expect(mosaicSlots(10, 3)).toEqual([...D, ...A, ...B, ...C])
  })

  it('wraps an offset past the last pattern, so any section index is a valid offset', () => {
    expect(mosaicSlots(4, 4)).toEqual(mosaicSlots(4, 0))
    expect(mosaicSlots(4, 7)).toEqual([...D, ...A])
  })

  it('cuts a partial pattern after an offset too', () => {
    expect(mosaicSlots(2, 2)).toEqual(['hero-right', 'side-left'])
    expect(mosaicSlots(2, 3)).toEqual(['wide', 'hero-left'])
  })
})

const day = (key: string, count: number) => ({ key, count, coverUid: `c-${key}` })

describe('pickDays', () => {
  // Nine days of September, newest first as the buckets endpoint returns them. The two days with 4
  // tie for the last place: the later one (the 8th) wins it.
  const nine = [
    day('2026-09-09', 1),
    day('2026-09-08', 4),
    day('2026-09-07', 9),
    day('2026-09-06', 2),
    day('2026-09-05', 7),
    day('2026-09-04', 4),
    day('2026-09-03', 8),
    day('2026-09-02', 6),
    day('2026-09-01', 5),
  ]

  it('keeps the six busiest of nine days in date order and reports three dropped', () => {
    const { shown, dropped } = pickDays(nine)
    expect(shown.map((d) => d.key)).toEqual([
      '2026-09-01',
      '2026-09-02',
      '2026-09-03',
      '2026-09-05',
      '2026-09-07',
      '2026-09-08',
    ])
    expect(dropped).toBe(3)
  })

  it('breaks a tie for the last place toward the later day', () => {
    const tied = [day('2026-09-02', 3), day('2026-09-01', 3)]
    expect(pickDays(tied, 1).shown.map((d) => d.key)).toEqual(['2026-09-02'])
    // Whatever order the input arrives in.
    expect(pickDays([...tied].reverse(), 1).shown.map((d) => d.key)).toEqual(['2026-09-02'])
  })

  it('passes six days through untouched with zero dropped', () => {
    const six = nine.slice(3).reverse()
    const { shown, dropped } = pickDays(six)
    expect(shown).toEqual(six)
    expect(dropped).toBe(0)
  })

  it('passes fewer than six days through in date order, whatever order they came in', () => {
    const { shown, dropped } = pickDays([day('2026-09-09', 1), day('2026-09-01', 5)])
    expect(shown.map((d) => d.key)).toEqual(['2026-09-01', '2026-09-09'])
    expect(dropped).toBe(0)
  })

  it('hands back the buckets themselves, cover and all', () => {
    expect(pickDays(nine).shown[0]).toBe(nine[8])
  })

  it('takes an empty month to nothing shown and nothing dropped', () => {
    expect(pickDays([])).toEqual({ shown: [], dropped: 0 })
  })
})

describe('daysOfYear (the year → months filter)', () => {
  const days = [
    day('2027-01-01', 1),
    day('2026-12-31', 2),
    day('2026-06-21', 3),
    day('2025-12-31', 4),
    day('12026-01-01', 5),
  ]

  it('keeps exactly the day buckets whose key starts with that year', () => {
    expect(daysOfYear(days, '2026').map((d) => d.key)).toEqual(['2026-12-31', '2026-06-21'])
  })

  it('matches the year as a whole field, never a substring of another year', () => {
    expect(daysOfYear(days, '202').map((d) => d.key)).toEqual([])
    expect(daysOfYear(days, '026').map((d) => d.key)).toEqual([])
  })

  it('keeps the buckets in the order they came', () => {
    expect(daysOfYear(days, '2027')).toEqual([days[0]])
  })
})

describe('mosaicSections', () => {
  it('groups by the YYYY-MM prefix in the order the months came, titled "Sep 2026", each picked', () => {
    const days = [
      ...Array.from({ length: 9 }, (_, i) => day(`2026-09-${String(9 - i).padStart(2, '0')}`, i + 1)),
      day('2026-08-31', 2),
      day('2026-08-01', 1),
    ]
    const sections = mosaicSections(days)
    expect(sections.map((s) => [s.key, s.label, s.shown.length, s.dropped])).toEqual([
      ['2026-09', 'Sep 2026', 6, 3],
      ['2026-08', 'Aug 2026', 2, 0],
    ])
    expect(sections[1]!.shown.map((d) => d.key)).toEqual(['2026-08-01', '2026-08-31'])
  })

  it('titles January and December of their own year', () => {
    const sections = mosaicSections([day('2027-01-01', 1), day('2026-12-31', 1)])
    expect(sections.map((s) => s.label)).toEqual(['Jan 2027', 'Dec 2026'])
  })
})
