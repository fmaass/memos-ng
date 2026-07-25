import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import MediaTile from '@/components/MediaTile.vue'

// blurhashToDataUrl decodes via canvas, which happy-dom lacks — mock it (keep tileKind real).
vi.mock('@/lib/media', async (orig) => {
  const actual = await orig<typeof import('@/lib/media')>()
  return { ...actual, blurhashToDataUrl: (h: string | null) => (h ? 'data:image/png;base64,blur' : null) }
})

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
  afterEach(() => {
    vi.useRealTimers()
  })

  it('shows a placeholder while pending', () => {
    const w = mount(MediaTile, { props: { att: att('pending') } })
    expect(w.find('[data-test="tile-placeholder"]').exists()).toBe(true)
  })

  it('shows the poster + percent while processing', () => {
    const w = mount(MediaTile, { props: { att: att('processing', 'video/mp4'), progress: 42 } })
    expect(w.find('[data-test="tile-processing"]').exists()).toBe(true)
    expect(w.text()).toContain('42%')
  })

  it('renders a lazy-loaded thumb (not the heavy mobile tier) for a ready image', () => {
    const w = mount(MediaTile, { props: { att: att('ready', 'image/png') } })
    const img = w.find('[data-test="tile-image"]')
    expect(img.attributes('src')).toBe('/api/attachments/a1/thumb') // small tier in the grid
    expect(img.attributes('loading')).toBe('lazy')
  })

  it('offers both image tiers with the pixel width each really serves', () => {
    // 3000×4000 portrait: the tiers cap the HEIGHT, so the widths are 600 / 1536, not 800 / 2048.
    const w = mount(MediaTile, {
      props: { att: att('ready', 'image/jpeg', { width: 3000, height: 4000 }), sizes: '832px' },
    })
    const img = w.find('[data-test="tile-image"]')
    expect(img.attributes('srcset')).toBe(
      '/api/attachments/a1/thumb 600w, /api/attachments/a1/mobile 1536w',
    )
    expect(img.attributes('sizes')).toBe('832px')
  })

  it('ships no srcset while the dims are unknown — the plain thumb src stands alone', () => {
    const w = mount(MediaTile, { props: { att: att('ready', 'image/png'), sizes: '832px' } })
    const img = w.find('[data-test="tile-image"]')
    expect(img.attributes('srcset')).toBeUndefined()
    expect(img.attributes('sizes')).toBeUndefined() // a sizes without a srcset means nothing
    expect(img.attributes('src')).toBe('/api/attachments/a1/thumb')
  })

  it('cache-busts every srcset candidate on retry (a stale srcset outranks src)', async () => {
    vi.useFakeTimers()
    const w = mount(MediaTile, {
      props: { att: att('ready', 'image/jpeg', { width: 4000, height: 3000 }) },
    })
    await w.find('[data-test="tile-image"]').trigger('error')
    await vi.advanceTimersByTimeAsync(1_000)
    expect(w.find('[data-test="tile-image"]').attributes('srcset')).toBe(
      '/api/attachments/a1/thumb?_r=1 800w, /api/attachments/a1/mobile?_r=1 2048w',
    )
  })

  it('leaves a video tile on its single poster (the image tiers are not its own)', () => {
    const w = mount(MediaTile, {
      props: { att: att('ready', 'video/mp4', { width: 1920, height: 1080, blurhash: 'LEHV6nWB' }) },
    })
    expect(w.findAll('img').every((i) => i.attributes('srcset') === undefined)).toBe(true)
  })

  it('shows the blurhash instantly behind a ready image (no blank box while it loads)', () => {
    const w = mount(MediaTile, { props: { att: att('ready', 'image/png', { blurhash: 'LEHV6nWB' }) } })
    expect(w.find('[data-test="tile-blur"]').exists()).toBe(true) // instant blurred preview
    expect(w.find('[data-test="tile-image"]').exists()).toBe(true) // real thumb loads on top
  })

  it('takes the intrinsic aspect ratio when dims are known (no forced square crop)', () => {
    const w = mount(MediaTile, {
      props: { att: att('ready', 'image/jpeg'), width: 3000, height: 2000 },
    })
    const box = w.find('[data-test="media-tile"]')
    expect(box.attributes('style')).toContain('aspect-ratio: 1.5')
    expect(box.classes()).not.toContain('aspect-square')
  })

  it('keeps the fallback box for a video, whose stored dims ignore the rotation tag', () => {
    // ffprobe records CODED dims, so a rotated portrait clip stores 1920x1080. Laying the box out
    // from that would frame the (correctly rotated) poster at 16:9.
    const w = mount(MediaTile, {
      props: {
        att: att('ready', 'video/mp4', { width: 1920, height: 1080 }),
        width: 1920,
        height: 1080,
      },
    })
    const box = w.find('[data-test="media-tile"]')
    expect(box.classes()).toContain('aspect-square')
    expect(box.attributes('style')).toBeUndefined()
  })

  it('keeps the square box while dims are unknown', () => {
    const w = mount(MediaTile, { props: { att: att('ready', 'image/jpeg') } })
    const box = w.find('[data-test="media-tile"]')
    expect(box.classes()).toContain('aspect-square')
    expect(box.attributes('style')).toBeUndefined()
  })

  it('makes a ready image a real button so the viewer opens by keyboard, not just by click', async () => {
    const w = mount(MediaTile, { props: { att: att('ready', 'image/png') } })
    const opener = w.find('[data-test="tile-open"]')
    expect(opener.element.tagName).toBe('BUTTON') // focusable + Enter/Space activate natively
    expect(opener.attributes('type')).toBe('button')
    expect(opener.attributes('aria-label')).toBe('Open image')
    await opener.trigger('click')
    expect(w.emitted('open')).toEqual([['a1']])
  })

  it('emits open exactly once when the image itself is clicked (unchanged contract)', async () => {
    const w = mount(MediaTile, { props: { att: att('ready', 'image/png') } })
    await w.find('[data-test="tile-image"]').trigger('click')
    expect(w.emitted('open')).toEqual([['a1']])
  })

  it('stretches the blurhash to the tile ratio instead of cropping it square', () => {
    const w = mount(MediaTile, {
      props: { att: att('ready', 'image/png', { blurhash: 'LEHV6nWB' }), width: 3000, height: 2000 },
    })
    const classes = w.find('[data-test="tile-blur"]').classes()
    expect(classes).toContain('object-fill')
    expect(classes).not.toContain('object-cover')
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

  it('cache-busts the thumbnail after the first retry delay (no re-mint yet)', async () => {
    vi.useFakeTimers()
    const w = mount(MediaTile, { props: { att: att('ready', 'image/png') } })
    expect(w.find('[data-test="tile-image"]').attributes('src')).toBe('/api/attachments/a1/thumb')
    await w.find('[data-test="tile-image"]').trigger('error')
    await vi.advanceTimersByTimeAsync(999)
    expect(w.find('[data-test="tile-image"]').attributes('src')).toBe('/api/attachments/a1/thumb')
    await vi.advanceTimersByTimeAsync(1)
    expect(w.find('[data-test="tile-image"]').attributes('src')).toBe('/api/attachments/a1/thumb?_r=1')
    expect(w.emitted('retry')).toBeUndefined() // still retrying in place — the feed is not re-minted
  })

  it('uses the longer delay for the second in-place retry', async () => {
    vi.useFakeTimers()
    const w = mount(MediaTile, { props: { att: att('ready', 'image/png') } })
    await w.find('[data-test="tile-image"]').trigger('error')
    await vi.advanceTimersByTimeAsync(1_000)
    expect(w.find('[data-test="tile-image"]').attributes('src')).toBe('/api/attachments/a1/thumb?_r=1')

    await w.find('[data-test="tile-image"]').trigger('error')
    await vi.advanceTimersByTimeAsync(3_999)
    expect(w.find('[data-test="tile-image"]').attributes('src')).toBe('/api/attachments/a1/thumb?_r=1')
    await vi.advanceTimersByTimeAsync(1)
    expect(w.find('[data-test="tile-image"]').attributes('src')).toBe('/api/attachments/a1/thumb?_r=2')
  })

  it('shows the failed state and asks the parent to re-mint once retries are exhausted', async () => {
    vi.useFakeTimers()
    const w = mount(MediaTile, { props: { att: att('ready', 'image/png') } })
    // MAX_THUMB_RETRIES in-place retries, then the next error exhausts → expired-signature signal.
    await w.find('[data-test="tile-image"]').trigger('error') // _r=1
    await vi.advanceTimersByTimeAsync(1_000)
    await w.find('[data-test="tile-image"]').trigger('error') // _r=2
    await vi.advanceTimersByTimeAsync(4_000)
    await w.find('[data-test="tile-image"]').trigger('error') // exhausted
    expect(w.find('[data-test="tile-image"]').exists()).toBe(false)
    expect(w.find('[data-test="tile-failed"]').exists()).toBe(true)
    expect(w.emitted('retry')?.[0]).toEqual(['a1'])
  })

  it('recovers a failed tile when connectivity returns (online event)', async () => {
    vi.useFakeTimers()
    const w = mount(MediaTile, { props: { att: att('ready', 'image/png') } })
    await w.find('[data-test="tile-image"]').trigger('error')
    await vi.advanceTimersByTimeAsync(1_000)
    await w.find('[data-test="tile-image"]').trigger('error')
    await vi.advanceTimersByTimeAsync(4_000)
    await w.find('[data-test="tile-image"]').trigger('error')
    expect(w.find('[data-test="tile-failed"]').exists()).toBe(true)
    // A re-mint inside the same signing bucket returns the identical URL, so only
    // the online signal can clear the failed state without user action.
    window.dispatchEvent(new Event('online'))
    await w.vm.$nextTick()
    expect(w.find('[data-test="tile-failed"]').exists()).toBe(false)
    expect(w.find('[data-test="tile-image"]').attributes('src')).toBe('/api/attachments/a1/thumb?_r=1')
    vi.useRealTimers()
  })

  it('resets and retries from scratch when a fresh (re-minted) URL arrives', async () => {
    vi.useFakeTimers()
    const w = mount(MediaTile, { props: { att: att('ready', 'image/png') } })
    await w.find('[data-test="tile-image"]').trigger('error')
    await vi.advanceTimersByTimeAsync(1_000)
    await w.find('[data-test="tile-image"]').trigger('error')
    await vi.advanceTimersByTimeAsync(4_000)
    await w.find('[data-test="tile-image"]').trigger('error') // failed
    expect(w.find('[data-test="tile-failed"]').exists()).toBe(true)
    await w.setProps({ att: att('ready', 'image/png', { thumbnailUrl: '/api/attachments/a1/thumb?exp=2' }) })
    expect(w.find('[data-test="tile-failed"]').exists()).toBe(false)
    expect(w.find('[data-test="tile-image"]').attributes('src')).toBe('/api/attachments/a1/thumb?exp=2')
  })

  it('cancels a pending retry when unmounted', async () => {
    vi.useFakeTimers()
    const w = mount(MediaTile, { props: { att: att('ready', 'image/png') } })
    await w.find('[data-test="tile-image"]').trigger('error')
    w.unmount()
    await vi.advanceTimersByTimeAsync(1_000)
    expect(w.emitted('retry')).toBeUndefined()
  })

  it('cancels a pending retry when the base URL changes', async () => {
    vi.useFakeTimers()
    const w = mount(MediaTile, { props: { att: att('ready', 'image/png') } })
    await w.find('[data-test="tile-image"]').trigger('error')
    await vi.advanceTimersByTimeAsync(999)
    await w.setProps({ att: att('ready', 'image/png', { thumbnailUrl: '/api/attachments/a1/thumb?exp=2' }) })
    await vi.advanceTimersByTimeAsync(1)
    expect(w.find('[data-test="tile-image"]').attributes('src')).toBe('/api/attachments/a1/thumb?exp=2')
  })
})
