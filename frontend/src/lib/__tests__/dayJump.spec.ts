import { describe, expect, it, vi } from 'vitest'

import { type DayPager, scrollToDay } from '@/lib/dayJump'

type Item = { uid: string; takenAt: string }
const at = (uid: string, takenAt: string): Item => ({ uid, takenAt })

// A scripted list: `pages[0]` is loaded when the jump starts, every fetchNext lands the next one.
// The mock is the fetcher only — the loop under test is the real one.
function pagerOver(pages: Item[][]) {
  let loaded = 1
  const snapshot = () => ({ items: pages.slice(0, loaded).flat(), hasNext: loaded < pages.length })
  const fetchNext = vi.fn(async () => {
    loaded += 1
    return snapshot()
  })
  const pager: DayPager<Item> = { current: snapshot, fetchNext }
  return { pager, fetchNext }
}

const newestFirst = [
  [at('p1a', '2026-09-03T10:00:00Z'), at('p1b', '2026-09-02T10:00:00Z')],
  [at('p2a', '2026-09-01T08:00:00Z'), at('p2b', '2026-08-31T22:30:00Z'), at('p2c', '2026-08-31T12:00:00Z')],
  [at('p3a', '2026-08-30T10:00:00Z')],
  [at('p4a', '2026-08-29T10:00:00Z')],
]

describe('scrollToDay', () => {
  it('fetches nothing when the day is already loaded', async () => {
    const { pager, fetchNext } = pagerOver(newestFirst)
    expect(await scrollToDay('2026-09-02', pager)).toBe('p1b')
    expect(fetchNext).not.toHaveBeenCalled()
  })

  it('stops at the page holding the day, never the page after, and returns its first tile', async () => {
    const { pager, fetchNext } = pagerOver(newestFirst)
    expect(await scrollToDay('2026-09-01', pager)).toBe('p2a')
    expect(fetchNext).toHaveBeenCalledTimes(1)
  })

  it('walks as many pages as the day needs', async () => {
    const { pager, fetchNext } = pagerOver(newestFirst)
    expect(await scrollToDay('2026-08-29', pager)).toBe('p4a')
    expect(fetchNext).toHaveBeenCalledTimes(3)
  })

  it('reads the day in Europe/Zurich: 22:30Z on 31 August is the 1st of September', async () => {
    const late = [[at('x', '2026-09-02T10:00:00Z')], [at('late', '2026-08-31T22:30:00Z')], [at('aug', '2026-08-31T12:00:00Z')]]
    const sept = pagerOver(late)
    expect(await scrollToDay('2026-09-01', sept.pager)).toBe('late')
    expect(sept.fetchNext).toHaveBeenCalledTimes(1)
    // …and NOT the 31st: the 31st's first tile is the noon item a page later.
    const aug = pagerOver(late)
    expect(await scrollToDay('2026-08-31', aug.pager)).toBe('aug')
    expect(aug.fetchNext).toHaveBeenCalledTimes(2)
  })

  it('returns the FIRST tile of the day in list order, whatever the order is', async () => {
    const oldestFirst = [[at('o1', '2026-08-31T12:00:00Z'), at('o2', '2026-08-31T13:00:00Z')]]
    expect(await scrollToDay('2026-08-31', pagerOver(oldestFirst).pager)).toBe('o1')
  })

  it('gives up with null at the end of the list, having fetched every page once', async () => {
    const { pager, fetchNext } = pagerOver(newestFirst)
    expect(await scrollToDay('2026-01-01', pager)).toBeNull()
    expect(fetchNext).toHaveBeenCalledTimes(3)
  })

  it('gives up with null when a page fails, without retrying it', async () => {
    const fetchNext = vi.fn(async () => null)
    const pager: DayPager<Item> = { current: () => ({ items: newestFirst[0]!, hasNext: true }), fetchNext }
    expect(await scrollToDay('2026-08-29', pager)).toBeNull()
    expect(fetchNext).toHaveBeenCalledTimes(1)
  })

  it('waits for a first page still loading before it looks', async () => {
    const { pager, fetchNext } = pagerOver(newestFirst)
    const slow: DayPager<Item> = { current: async () => pager.current(), fetchNext }
    expect(await scrollToDay('2026-09-03', slow)).toBe('p1a')
    expect(fetchNext).not.toHaveBeenCalled()
  })

  it('stops a superseded walk before its next page and returns null', async () => {
    const { pager, fetchNext } = pagerOver(newestFirst)
    let live = true
    fetchNext.mockImplementationOnce(async () => {
      live = false // a newer jump took over while this page was on the wire
      return { items: newestFirst.slice(0, 2).flat(), hasNext: true }
    })
    expect(await scrollToDay('2026-08-29', pager, undefined, () => live)).toBeNull()
    expect(fetchNext).toHaveBeenCalledTimes(1)
  })

  it('asks for no page at all when it is superseded before it starts', async () => {
    const { pager, fetchNext } = pagerOver(newestFirst)
    expect(await scrollToDay('2026-09-03', pager, undefined, () => false)).toBeNull()
    expect(fetchNext).not.toHaveBeenCalled()
  })

  it('skips an item whose takenAt cannot be read instead of throwing', async () => {
    const pages = [[at('bad', 'not a date'), at('good', '2026-09-02T10:00:00Z')]]
    expect(await scrollToDay('2026-09-02', pagerOver(pages).pager)).toBe('good')
  })
})
