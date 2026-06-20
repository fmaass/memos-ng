import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import MediaTile from '@/components/MediaTile.vue'

type Status = 'uploading' | 'pending' | 'processing' | 'ready' | 'failed'
function att(status: Status, mime = 'image/jpeg', extra: Record<string, unknown> = {}) {
  return {
    uid: 'a1',
    mime,
    status,
    blurhash: null,
    width: null,
    height: null,
    duration: null,
    thumbnailUrl: '/api/attachments/a1/thumb',
    previewUrl: '/api/attachments/a1/mobile',
    webUrl: '/api/attachments/a1/web',
    posterUrl: '/api/attachments/a1/poster',
    downloadUrl: '/api/attachments/a1/original',
    ...extra,
  }
}

describe('MediaTile', () => {
  it('shows a placeholder while pending', () => {
    const w = mount(MediaTile, { props: { att: att('pending') } })
    expect(w.find('[data-test="tile-placeholder"]').exists()).toBe(true)
  })

  it('shows the poster + percent while processing', () => {
    const w = mount(MediaTile, { props: { att: att('processing', 'video/mp4'), progress: 42 } })
    expect(w.find('[data-test="tile-processing"]').exists()).toBe(true)
    expect(w.text()).toContain('42%')
  })

  it('renders an image when a ready image', () => {
    const w = mount(MediaTile, { props: { att: att('ready', 'image/png') } })
    expect(w.find('[data-test="tile-image"]').attributes('src')).toBe('/api/attachments/a1/mobile')
  })

  it('renders a video element when a ready video', () => {
    const w = mount(MediaTile, { props: { att: att('ready', 'video/mp4') } })
    expect(w.find('[data-test="tile-video"]').exists()).toBe(true)
  })

  it('offers retry on failure and emits the uid', async () => {
    const w = mount(MediaTile, { props: { att: att('failed') } })
    await w.find('[data-test="tile-retry"]').trigger('click')
    expect(w.emitted('retry')?.[0]).toEqual(['a1'])
  })
})
