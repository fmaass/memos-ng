import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import DigestRuns from '@/components/DigestRuns.vue'
import type { DigestRun } from '@/lib/digest'

function run(over: Partial<DigestRun> = {}): DigestRun {
  return {
    id: 1,
    trigger: 'scheduled',
    started_at: '2026-08-30T05:00:00Z',
    finished_at: '2026-08-30T05:01:00Z',
    status: 'sent',
    memos_sent: 7,
    parts: 2,
    parts_sent: 2,
    parts_claimed: 2,
    recipients: 6,
    error: null,
    local_date: '2026-08-30',
    ...over,
  }
}

describe('DigestRuns', () => {
  it('says so when nothing has run', () => {
    expect(mount(DigestRuns, { props: { runs: [] } }).find('[data-test="digest-runs-empty"]').exists()).toBe(
      true,
    )
    expect(
      mount(DigestRuns, { props: { runs: undefined } })
        .find('[data-test="digest-runs-empty"]')
        .exists(),
    ).toBe(true)
  })

  it('lists the runs in the order it was given them and shows an in-flight one as running', () => {
    const wrapper = mount(DigestRuns, {
      props: { runs: [run({ id: 2, status: null }), run({ id: 1, local_date: '2026-08-29' })] },
    })
    const rows = wrapper.findAll('[data-test="digest-run-row"]')
    expect(rows).toHaveLength(2)
    expect(wrapper.findAll('[data-test="digest-run-status"]')[0].text()).toBe('running')
    expect(wrapper.findAll('[data-test="digest-run-status"]')[1].text()).toBe('sent')
  })

  it('opens one row at a time to show what it recorded', async () => {
    const wrapper = mount(DigestRuns, {
      props: {
        runs: [
          run({ id: 2, status: 'failed', error: 'part 1: refused 2 recipient(s)' }),
          run({ id: 1 }),
        ],
      },
    })
    expect(wrapper.find('[data-test="digest-run-detail"]').exists()).toBe(false)

    await wrapper.findAll('[data-test="digest-run-row"]')[0].trigger('click')
    expect(wrapper.find('[data-test="digest-run-error"]').text()).toBe(
      'part 1: refused 2 recipient(s)',
    )

    await wrapper.findAll('[data-test="digest-run-row"]')[1].trigger('click')
    expect(wrapper.findAll('[data-test="digest-run-detail"]')).toHaveLength(1)
    expect(wrapper.find('[data-test="digest-run-no-error"]').exists()).toBe(true)

    await wrapper.findAll('[data-test="digest-run-row"]')[1].trigger('click')
    expect(wrapper.find('[data-test="digest-run-detail"]').exists()).toBe(false)
  })
})
