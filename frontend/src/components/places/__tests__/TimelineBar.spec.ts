import { mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import { describe, expect, it } from 'vitest'

import TimelineBar from '@/components/places/TimelineBar.vue'
import { marker } from '@/lib/places/__tests__/fixtures'
import { buildHistogram, type TimelineState } from '@/lib/places/timeline'
import { BAR_WIDTH } from '@/lib/places/timelineBar'

const histogram = buildHistogram([
  marker({ uid: 'a', taken_at: '2024-12-10T12:00:00Z' }),
  marker({ uid: 'b', taken_at: '2025-01-10T12:00:00Z' }),
  marker({ uid: 'c', taken_at: '2025-01-20T12:00:00Z' }),
  marker({ uid: 'd', taken_at: '2025-02-10T12:00:00Z' }),
])

function mountBar(over: Partial<TimelineState> = {}, phone = false) {
  const state: TimelineState = {
    index: 1,
    playing: false,
    speed: 1,
    mode: 'accumulate',
    bucketCount: histogram.buckets.length,
    ...over,
  }
  return mount(TimelineBar, {
    global: { plugins: [PrimeVue] },
    props: { histogram, state, phone },
  })
}

describe('TimelineBar', () => {
  it('draws one bar per month and names the month the playhead is on', () => {
    const wrapper = mountBar()
    const bars = wrapper.findAll('[data-test="timeline-bucket"]')
    expect(bars.map((bar) => bar.attributes('data-key'))).toEqual([
      '2024-12',
      '2025-01',
      '2025-02',
    ])
    expect(wrapper.find('[data-test="timeline-label"]').text()).toBe('Jan 2025')
  })

  it('labels the years proportionally to the months they cover', () => {
    const years = mountBar().findAll('[data-test="timeline-year"]')
    expect(years.map((year) => year.text())).toEqual(['2024', '2025'])
    expect(years[1]?.attributes('style')).toContain('66.6')
  })

  it('asks for play or pause depending on what it is doing now', async () => {
    const paused = mountBar()
    await paused.find('[data-test="timeline-play"]').trigger('click')
    expect(paused.emitted('action')?.[0]).toEqual([{ type: 'play' }])

    const playing = mountBar({ playing: true })
    await playing.find('[data-test="timeline-play"]').trigger('click')
    expect(playing.emitted('action')?.[0]).toEqual([{ type: 'pause' }])
  })

  it('seeks to the month under a click on the bar', async () => {
    const wrapper = mountBar()
    await wrapper
      .find('[data-test="timeline-chart"]')
      .trigger('click', { clientX: BAR_WIDTH - 1 })
    expect(wrapper.emitted('action')?.[0]).toEqual([{ type: 'seek', index: 2 }])
  })

  it('offers the desktop every speed and both modes', async () => {
    const wrapper = mountBar()
    expect(wrapper.attributes('data-variant')).toBe('full')
    const speeds = wrapper.findAll('[data-test="timeline-speed"] .p-togglebutton')
    expect(speeds.map((option) => option.text())).toEqual(['1×', '2×', '4×'])
    await speeds[1]?.trigger('click')
    expect(wrapper.emitted('action')?.[0]).toEqual([{ type: 'setSpeed', speed: 2 }])

    const modes = wrapper.findAll('[data-test="timeline-mode"] .p-togglebutton')
    expect(modes.map((option) => option.text())).toEqual(['All so far', 'Past 12 months'])
    await modes[1]?.trigger('click')
    expect(wrapper.emitted('action')?.[1]).toEqual([{ type: 'setMode', mode: 'window12' }])
  })

  it('gives the phone one speed chip that cycles, and no mode toggle', async () => {
    const wrapper = mountBar({ speed: 2 }, true)
    expect(wrapper.attributes('data-variant')).toBe('compact')
    expect(wrapper.find('[data-test="timeline-speed"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="timeline-mode"]').exists()).toBe(false)
    const chip = wrapper.find('[data-test="timeline-speed-chip"]')
    expect(chip.text()).toBe('2×')
    await chip.trigger('click')
    expect(wrapper.emitted('action')?.[0]).toEqual([{ type: 'setSpeed', speed: 4 }])
  })

  it('keeps the phone bar on one line: nothing wraps and the label cannot break', () => {
    const wrapper = mountBar({}, true)
    expect(wrapper.classes()).toContain('flex-nowrap')
    expect(wrapper.find('[data-test="timeline-label"]').classes()).toContain('whitespace-nowrap')
  })

  it('says so rather than drawing an empty bar when no photo carries a date', () => {
    const empty = mount(TimelineBar, {
      global: { plugins: [PrimeVue] },
      props: {
        histogram: buildHistogram([]),
        state: { index: 0, playing: false, speed: 1, mode: 'accumulate', bucketCount: 0 },
        phone: false,
      },
    })
    expect(empty.findAll('[data-test="timeline-bucket"]')).toHaveLength(0)
    expect(empty.find('[data-test="timeline-label"]').text()).toBe('No dates')
    expect(empty.find('[data-test="timeline-play"]').attributes('disabled')).toBeDefined()
  })
})
