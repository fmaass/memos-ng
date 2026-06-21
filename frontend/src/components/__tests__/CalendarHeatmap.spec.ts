import { mount } from '@vue/test-utils'
import { ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import CalendarHeatmap from '@/components/CalendarHeatmap.vue'

// Empty data → the cold-start full window (deterministic regardless of the real `today`, since the
// component uses a live new Date()). The activity-clamped window is unit-tested in heatmap.spec.ts.
vi.mock('@/composables/useHeatmap', () => ({
  useHeatmap: () => ({ data: ref({}) }),
}))

describe('CalendarHeatmap', () => {
  it('renders the full 12-week window when there is no activity', () => {
    const wrapper = mount(CalendarHeatmap)
    expect(wrapper.find('[data-test="heatmap"]').exists()).toBe(true)
    expect(wrapper.findAll('[data-test="heat-cell"]').length).toBe(84) // 12 weeks cold-start
  })
})
