import { mount } from '@vue/test-utils'
import { ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import CalendarHeatmap from '@/components/CalendarHeatmap.vue'

vi.mock('@/composables/useHeatmap', () => ({
  useHeatmap: () => ({ data: ref({ '2026-06-20': 2 }) }),
}))

describe('CalendarHeatmap', () => {
  it('renders a cell per day across the window', () => {
    const wrapper = mount(CalendarHeatmap)
    expect(wrapper.find('[data-test="heatmap"]').exists()).toBe(true)
    expect(wrapper.findAll('[data-test="heat-cell"]').length).toBe(84) // 12 weeks
  })
})
