import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import DayMosaic from '@/components/DayMosaic.vue'

const bucket = (key: string, count: number) => ({
  key,
  count,
  coverUid: `cover-${key}`,
  coverThumbnailUrl: `/api/attachments/cover-${key}/thumb`,
})

// Newest first, as the buckets endpoint answers: September has nine days (three are dropped, the
// 4th and the 8th tie at 4 and the later one stays), August has two.
const days = [
  bucket('2026-09-09', 1),
  bucket('2026-09-08', 4),
  bucket('2026-09-07', 9),
  bucket('2026-09-06', 2),
  bucket('2026-09-05', 7),
  bucket('2026-09-04', 4),
  bucket('2026-09-03', 8),
  bucket('2026-09-02', 6),
  bucket('2026-09-01', 5),
  bucket('2026-08-31', 2),
  bucket('2026-08-12', 1),
]

const sections = (w: ReturnType<typeof mount>) => w.findAll('[data-test="mosaic-section"]')

describe('DayMosaic', () => {
  it('titles each month once, newest month first, "Sep 2026" style', () => {
    const w = mount(DayMosaic, { props: { days } })
    expect(w.findAll('[data-test="mosaic-title"]').map((t) => t.text())).toEqual(['Sep 2026', 'Aug 2026'])
  })

  it('renders one tile per PICKED day, day numbers in date order, each with its day’s cover', () => {
    const w = mount(DayMosaic, { props: { days } })
    const [sep, aug] = sections(w)
    const dayNumbers = (s: typeof sep) => s!.findAll('[data-test="mosaic-day"]').map((d) => d.text())
    expect(dayNumbers(sep)).toEqual(['1', '2', '3', '5', '7', '8'])
    expect(dayNumbers(aug)).toEqual(['12', '31'])
    expect(sep!.findAll('[data-test="mosaic-tile"] img').map((i) => i.attributes('src'))).toEqual(
      ['01', '02', '03', '05', '07', '08'].map((d) => `/api/attachments/cover-2026-09-${d}/thumb`),
    )
  })

  it('ends the capped month with "and 3 more days" and the short month with nothing', () => {
    const w = mount(DayMosaic, { props: { days } })
    const [sep, aug] = sections(w)
    expect(sep!.find('[data-test="mosaic-more"]').text()).toBe('and 3 more days')
    expect(aug!.find('[data-test="mosaic-more"]').exists()).toBe(false)
  })

  it('says "day" for one more day', () => {
    const w = mount(DayMosaic, { props: { days: days.slice(0, 7) } })
    expect(w.find('[data-test="mosaic-more"]').text()).toBe('and 1 more day')
  })

  it('frames the tiles by the cycle, each month starting one pattern further on', () => {
    const w = mount(DayMosaic, { props: { days } })
    const slots = (s: ReturnType<typeof sections>[number]) =>
      s.findAll('[data-test="mosaic-tile"]').map((t) => t.attributes('data-slot'))
    const [sep, aug] = sections(w)
    expect(slots(sep!)).toEqual(['hero-left', 'side-right', 'side-right', 'square', 'square', 'square'])
    expect(slots(aug!)).toEqual(['square', 'square'])
  })

  it('shows no count on a day tile', () => {
    const w = mount(DayMosaic, { props: { days } })
    const tile = w.find('[data-test="mosaic-tile"]')
    expect(tile.text()).toBe('1')
  })

  it('jumps to a tapped day', async () => {
    const w = mount(DayMosaic, { props: { days } })
    await sections(w)[1]!.findAll('[data-test="mosaic-tile"]')[1]!.trigger('click')
    expect(w.emitted('jump')).toEqual([['2026-08-31']])
  })

  it('jumps to the month’s first SHOWN day from the "more days" row', async () => {
    const w = mount(DayMosaic, { props: { days: days.slice(0, 7).concat(bucket('2026-09-01', 0)) } })
    // The 1st has the fewest items and is dropped: the row opens the earliest day still shown.
    await w.find('[data-test="mosaic-more"]').trigger('click')
    expect(w.emitted('jump')).toEqual([['2026-09-03']])
  })

  it('renders nothing for no days', () => {
    const w = mount(DayMosaic, { props: { days: [] } })
    expect(sections(w)).toHaveLength(0)
  })
})
