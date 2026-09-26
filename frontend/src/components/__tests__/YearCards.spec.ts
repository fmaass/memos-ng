import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import YearCards from '@/components/YearCards.vue'

const bucket = (key: string, count: number) => ({
  key,
  count,
  coverUid: `cover-${key}`,
  coverThumbnailUrl: `/api/attachments/cover-${key}/thumb`,
})

describe('YearCards', () => {
  const years = [bucket('2026', 1171), bucket('2025', 3)]

  it('renders one card per year bucket, in the order given, each with its year and its count', () => {
    const w = mount(YearCards, { props: { years } })
    const cards = w.findAll('[data-test="year-card"]')
    expect(cards).toHaveLength(2)
    expect(cards.map((c) => c.find('[data-test="year-label"]').text())).toEqual(['2026', '2025'])
    expect(cards.map((c) => c.find('[data-test="year-count"]').text())).toEqual(['1,171', '3'])
  })

  it('shows each year’s cover', () => {
    const w = mount(YearCards, { props: { years } })
    const covers = w.findAll('[data-test="year-card"] img')
    expect(covers.map((img) => img.attributes('src'))).toEqual([
      '/api/attachments/cover-2026/thumb',
      '/api/attachments/cover-2025/thumb',
    ])
  })

  it('asks for that year when a card is tapped', async () => {
    const w = mount(YearCards, { props: { years } })
    await w.findAll('[data-test="year-card"]')[1]!.trigger('click')
    expect(w.emitted('select')).toEqual([['2025']])
  })

  it('is a 16:9 card, full width', () => {
    const w = mount(YearCards, { props: { years } })
    expect(w.find('[data-test="year-card"]').classes()).toEqual(
      expect.arrayContaining(['w-full', 'aspect-video']),
    )
  })
})
