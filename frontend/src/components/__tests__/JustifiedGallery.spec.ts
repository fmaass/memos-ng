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
    vi.unstubAllGlobals()
  })

  it('shows failed affordance and emits retry when image retries are exhausted', async () => {
    const wrapper = mount(JustifiedGallery, { props: { items: [item()], targetHeight: 200 } })
    await flushPromises()
    const img = wrapper.find('[data-test="gallery-img"]')

    await img.trigger('error')
    expect(img.attributes('src')).toBe('/api/attachments/a1/thumb?_r=1')
    await img.trigger('error')
    expect(img.attributes('src')).toBe('/api/attachments/a1/thumb?_r=2')
    await img.trigger('error')

    expect(wrapper.find('[data-test="gallery-failed"]').exists()).toBe(true)
    expect(wrapper.emitted('retry')?.[0]).toEqual(['a1'])
  })

  it('recovers failed tiles when connectivity returns (online event)', async () => {
    const wrapper = mount(JustifiedGallery, { props: { items: [item()], targetHeight: 200 } })
    await flushPromises()
    const img = wrapper.find('[data-test="gallery-img"]')
    await img.trigger('error')
    await img.trigger('error')
    await img.trigger('error')
    expect(wrapper.find('[data-test="gallery-failed"]').exists()).toBe(true)

    window.dispatchEvent(new Event('online'))
    await wrapper.vm.$nextTick()

    expect(wrapper.find('[data-test="gallery-failed"]').exists()).toBe(false)
    expect(wrapper.emitted('retry')).toEqual([['a1'], ['a1']])
  })

  it('manual retry clears the failed overlay and requests a Resources re-mint', async () => {
    const wrapper = mount(JustifiedGallery, { props: { items: [item()], targetHeight: 200 } })
    await flushPromises()
    const img = wrapper.find('[data-test="gallery-img"]')
    await img.trigger('error')
    await img.trigger('error')
    await img.trigger('error')

    await wrapper.find('[data-test="gallery-retry"]').trigger('click')

    expect(wrapper.find('[data-test="gallery-failed"]').exists()).toBe(false)
    expect(wrapper.emitted('retry')).toEqual([['a1'], ['a1']])
    expect(wrapper.find('[data-test="gallery-img"]').attributes('src')).toBe(
      '/api/attachments/a1/thumb?_r=1',
    )
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
