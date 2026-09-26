import { mount } from '@vue/test-utils'
import { defineComponent, h, markRaw, nextTick, ref } from 'vue'
import { describe, expect, it } from 'vitest'

import {
  createDockSegment,
  DOCK_SEGMENT_KEY,
  useDockSegment,
} from '@/composables/useDockSegment'

const PillA = markRaw(defineComponent({ render: () => h('span', 'A') }))
const PillB = markRaw(defineComponent({ render: () => h('span', 'B') }))

function viewRegistering(pill: typeof PillA) {
  return defineComponent({
    setup() {
      useDockSegment(pill)
      return () => h('div')
    },
  })
}

describe('useDockSegment', () => {
  it('registers the view’s segment on mount and withdraws it on unmount', async () => {
    const registration = createDockSegment()
    const shown = ref(true)
    const View = viewRegistering(PillA)
    const Host = defineComponent({ render: () => (shown.value ? h(View) : null) })
    mount(Host, { global: { provide: { [DOCK_SEGMENT_KEY as symbol]: registration } } })

    expect(registration.segment.value).toBe(PillA)
    shown.value = false
    await nextTick()
    expect(registration.segment.value).toBeNull()
  })

  it('never lets a view that unmounts late clear the segment a newer view registered', () => {
    const registration = createDockSegment()
    registration.register(PillA)
    registration.register(PillB)
    registration.unregister(PillA)
    expect(registration.segment.value).toBe(PillB)
    registration.unregister(PillB)
    expect(registration.segment.value).toBeNull()
  })

  it('is a no-op outside a shell that offers the registration', () => {
    const View = viewRegistering(PillA)
    const wrapper = mount(View)
    expect(() => wrapper.unmount()).not.toThrow()
  })
})
