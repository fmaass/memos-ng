import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import JustifiedGallery from '@/components/JustifiedGallery.vue'
import { printMasterUploads } from '@/composables/usePrintMaster'

function item(uid = 'a1') {
  return {
    uid,
    mime: 'image/jpeg',
    status: 'ready' as const,
    blurhash: null,
    sha256: null,
    width: 400,
    height: 300,
    thumbnailUrl: `/api/attachments/${uid}/thumb`,
    previewUrl: `/api/attachments/${uid}/mobile`,
    webUrl: `/api/attachments/${uid}/web`,
    posterUrl: `/api/attachments/${uid}/poster`,
  }
}

describe('JustifiedGallery', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(private cb: ResizeObserverCallback) {}
        observe() {
          this.cb([{ contentRect: { width: 400 } } as ResizeObserverEntry], this as ResizeObserver)
        }
        disconnect() {}
        unobserve() {}
      },
    )
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  // Each tile is a MediaTile, whose cache-bust retries wait out a backoff (1 s, then 4 s) before
  // the next src goes in; the timers are advanced past each wait so every step is observed.
  async function failOnce(wrapper: ReturnType<typeof mount>) {
    await wrapper.find('[data-test="tile-image"]').trigger('error')
    await vi.advanceTimersByTimeAsync(5_000)
  }

  it('shows failed affordance and emits retry when image retries are exhausted', async () => {
    vi.useFakeTimers()
    const wrapper = mount(JustifiedGallery, { props: { items: [item()], targetHeight: 200 } })
    await flushPromises()

    await failOnce(wrapper)
    expect(wrapper.find('[data-test="tile-image"]').attributes('src')).toBe('/api/attachments/a1/thumb?_r=1')
    await failOnce(wrapper)
    expect(wrapper.find('[data-test="tile-image"]').attributes('src')).toBe('/api/attachments/a1/thumb?_r=2')
    await failOnce(wrapper)

    expect(wrapper.find('[data-test="tile-failed"]').exists()).toBe(true)
    expect(wrapper.emitted('retry')?.[0]).toEqual(['a1'])
  })

  it('recovers failed tiles when connectivity returns (online event)', async () => {
    vi.useFakeTimers()
    const wrapper = mount(JustifiedGallery, { props: { items: [item()], targetHeight: 200 } })
    await flushPromises()
    await failOnce(wrapper)
    await failOnce(wrapper)
    await failOnce(wrapper)
    expect(wrapper.find('[data-test="tile-failed"]').exists()).toBe(true)

    window.dispatchEvent(new Event('online'))
    await wrapper.vm.$nextTick()

    expect(wrapper.find('[data-test="tile-failed"]').exists()).toBe(false)
    expect(wrapper.emitted('retry')).toEqual([['a1'], ['a1']])
  })

  it('manual retry clears the failed overlay and requests a Resources re-mint', async () => {
    vi.useFakeTimers()
    const wrapper = mount(JustifiedGallery, { props: { items: [item()], targetHeight: 200 } })
    await flushPromises()
    await failOnce(wrapper)
    await failOnce(wrapper)
    await failOnce(wrapper)

    await wrapper.find('[data-test="tile-retry"]').trigger('click')

    expect(wrapper.find('[data-test="tile-failed"]').exists()).toBe(false)
    expect(wrapper.emitted('retry')).toEqual([['a1'], ['a1']])
    expect(wrapper.find('[data-test="tile-image"]').attributes('src')).toBe(
      '/api/attachments/a1/thumb?_r=1',
    )
  })

  // These cases name no renderer-specific test id: the photo is the one <img> that is not the
  // aria-hidden blurhash, a failed tile is the one showing a Retry button, the video overlay is the
  // play icon. They describe what a user sees, so they hold whichever element draws the tile.
  function photo(wrapper: ReturnType<typeof mount>) {
    return wrapper.find('img:not([aria-hidden="true"])')
  }
  function showsRetry(wrapper: ReturnType<typeof mount>): boolean {
    return wrapper.findAll('button').some((b) => b.text() === 'Retry')
  }
  function video(uid = 'v1') {
    return { ...item(uid), mime: 'video/mp4' }
  }

  it('opens a video when the play overlay itself is clicked (MEMNG-502)', async () => {
    // The overlay covers the whole tile, so it is what a tap on a video lands on.
    const wrapper = mount(JustifiedGallery, {
      props: { items: [item('a1'), video('v1')], targetHeight: 200 },
    })
    await flushPromises()

    await wrapper.find('.pi-play-circle').trigger('click')

    expect(wrapper.emitted('open')).toEqual([[1]])
  })

  it('opens from a native button, so Enter and Space both activate a tile (MEMNG-502)', async () => {
    // happy-dom synthesises no click from a key, so the keyboard contract is asserted where the
    // platform grants it: a native <button type="button"> is activated by Enter AND Space in every
    // browser, where an <img role="button"> with an Enter handler answered Enter only.
    const wrapper = mount(JustifiedGallery, {
      props: { items: [item('a1'), video('v1')], targetHeight: 200 },
    })
    await flushPromises()

    const targets = wrapper.findAll('button, [role="button"]')
    expect(targets).toHaveLength(2)
    for (const [i, t] of targets.entries()) {
      expect(t.element.tagName).toBe('BUTTON')
      expect(t.attributes('type')).toBe('button')
      await t.trigger('click')
      expect(wrapper.emitted('open')?.[i]).toEqual([i])
    }
  })

  it('heals a failed tile when its attachment arrives with a new URL (MEMNG-503)', async () => {
    // The re-mint refetch hands the SAME uid back with a fresh signed URL; nothing else happens,
    // so the tile must take the new URL and drop its failed state on its own.
    vi.useFakeTimers()
    try {
      const wrapper = mount(JustifiedGallery, { props: { items: [item()], targetHeight: 200 } })
      await flushPromises()
      for (let n = 0; n < 3; n++) {
        await photo(wrapper).trigger('error')
        await vi.advanceTimersByTimeAsync(5_000)
      }
      expect(showsRetry(wrapper)).toBe(true)
      expect(wrapper.emitted('retry')?.[0]).toEqual(['a1'])

      await wrapper.setProps({ items: [{ ...item(), thumbnailUrl: '/api/attachments/a1/thumb?sig=new' }] })

      expect(photo(wrapper).attributes('src')).toBe('/api/attachments/a1/thumb?sig=new')
      expect(showsRetry(wrapper)).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  describe('print-master state', () => {
    afterEach(() => printMasterUploads().clear())

    it('chips a photo that carries a print master', async () => {
      const wrapper = mount(JustifiedGallery, {
        props: {
          items: [
            {
              ...item(),
              renditions: [
                {
                  variant_key: 'print:glossy',
                  label: 'glossy',
                  mime: 'image/tiff',
                  size: 8_598_323,
                  downloadUrl: '/api/attachments/a1/rendition/print:glossy',
                  rev: 'a1b2c3d4',
                },
              ],
            },
          ],
          targetHeight: 200,
        },
      })
      await flushPromises()
      expect(wrapper.find('[data-test="print-chip"]').text()).toBe('PRINT')
    })

    it('offers RETRY here for a failed upload — an orphan photo has no memo tile to fall back on', async () => {
      // Resources is where orphan attachments live: without this the only report of a failed
      // print-master upload is a toast that is already gone, with nothing left to retry from.
      printMasterUploads().set('a1', { status: 'error', progress: 40 })
      const wrapper = mount(JustifiedGallery, { props: { items: [item()], targetHeight: 200 } })
      await flushPromises()

      const chip = wrapper.find('[data-test="print-chip"]')
      expect(chip.text()).toBe('PRINT FAILED · RETRY')

      await chip.trigger('click')
      expect(wrapper.emitted('print-retry')?.[0]).toEqual(['a1'])
    })
  })
})
