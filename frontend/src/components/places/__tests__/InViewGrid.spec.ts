import { mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import { describe, expect, it } from 'vitest'

import InViewGrid from '@/components/places/InViewGrid.vue'
import { marker } from '@/lib/places/__tests__/fixtures'

// Input order is deliberately the opposite of display order: the grid sorts newest first, so a
// tap on the FIRST tile must identify the NEWEST photo — an index into the input would open the
// other one, and "Show in memo" would then jump to the other photo's memo.
const older = marker({ uid: 'older', memo_uid: 'memo-older', taken_at: '2024-01-01T00:00:00Z' })
const newer = marker({ uid: 'newer', memo_uid: 'memo-newer', taken_at: '2026-01-01T00:00:00Z' })

function mountGrid(markers = [older, newer], filtered = false) {
  return mount(InViewGrid, { global: { plugins: [PrimeVue] }, props: { markers, filtered } })
}

describe('InViewGrid', () => {
  it('identifies the tapped photo by uid, matching what the tile shows', async () => {
    const wrapper = mountGrid()
    const tiles = wrapper.findAll('[data-test="in-view-tile"]')
    expect(tiles).toHaveLength(2)
    expect(tiles[0]?.find('img').attributes('src')).toContain('newer')

    await tiles[0]?.trigger('click')
    expect(wrapper.emitted('open')?.[0]).toEqual(['newer'])

    await tiles[1]?.trigger('click')
    expect(wrapper.emitted('open')?.[1]).toEqual(['older'])
  })

  it('counts what is in view and offers the reset only while filtered', () => {
    expect(mountGrid().find('[data-test="in-view-count"]').text()).toBe('2 photos in view')
    expect(mountGrid().find('[data-test="in-view-reset"]').exists()).toBe(false)
    expect(mountGrid([newer], true).find('[data-test="in-view-count"]').text()).toBe(
      '1 photo in view',
    )
    expect(mountGrid([newer], true).find('[data-test="in-view-reset"]').exists()).toBe(true)
  })
})
