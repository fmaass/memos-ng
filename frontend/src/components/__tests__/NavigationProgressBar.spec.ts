import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import NavigationProgressBar from '@/components/NavigationProgressBar.vue'

describe('NavigationProgressBar', () => {
  it('draws nothing while no navigation is waiting', () => {
    const wrapper = mount(NavigationProgressBar, { props: { visible: false } })
    expect(wrapper.find('[data-test="nav-progress"]').exists()).toBe(false)
  })

  it('announces the wait to a screen reader as well as to the eye', () => {
    const wrapper = mount(NavigationProgressBar, { props: { visible: true } })
    const bar = wrapper.find('[data-test="nav-progress"]')

    expect(bar.exists()).toBe(true)
    expect(bar.attributes('role')).toBe('progressbar')
    expect(bar.attributes('aria-label')).toBeTruthy()
    // Indeterminate: there is no progress to report, only that there is a wait.
    expect(bar.attributes('aria-valuenow')).toBeUndefined()
  })

  it('leaves the animation out for a viewer who asked for less motion', () => {
    const wrapper = mount(NavigationProgressBar, { props: { visible: true } })
    const classes = wrapper.find('[data-test="nav-progress-fill"]').classes()

    expect(classes).toContain('animate-pulse')
    expect(classes).toContain('motion-reduce:animate-none')
  })
})
