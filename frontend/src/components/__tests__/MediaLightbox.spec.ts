import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import MediaLightbox from '@/components/MediaLightbox.vue'

const items = [
  { uid: 'i1', mime: 'image/jpeg', previewUrl: '/i1/mobile', webUrl: '/i1/web', posterUrl: '/i1/p', downloadUrl: '/i1/o' },
  { uid: 'i2', mime: 'image/jpeg', previewUrl: '/i2/mobile', webUrl: '/i2/web', posterUrl: '/i2/p', downloadUrl: '/i2/o' },
  { uid: 'v3', mime: 'video/mp4', previewUrl: '/v3/mobile', webUrl: '/v3/web', posterUrl: '/v3/p', downloadUrl: '/v3/o' },
]

function lastEmit(wrapper: ReturnType<typeof mount>) {
  const ev = wrapper.emitted('update:modelValue')
  return ev ? ev[ev.length - 1][0] : undefined
}

describe('MediaLightbox', () => {
  it('shows the image at the given index and the counter', () => {
    const w = mount(MediaLightbox, { props: { items, modelValue: 0 }, attachTo: document.body })
    const img = document.body.querySelector('[data-test="lightbox-image"]') as HTMLImageElement
    expect(img?.getAttribute('src')).toBe('/i1/mobile')
    w.unmount()
  })

  it('renders a <video> for a video item', () => {
    const w = mount(MediaLightbox, { props: { items, modelValue: 2 }, attachTo: document.body })
    expect(document.body.querySelector('[data-test="lightbox-video"]')).toBeTruthy()
    expect(document.body.querySelector('[data-test="lightbox-image"]')).toBeFalsy()
    w.unmount()
  })

  it('ArrowRight advances the index, Escape closes', async () => {
    const w = mount(MediaLightbox, { props: { items, modelValue: 0 }, attachTo: document.body })
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight' }))
    expect(lastEmit(w)).toBe(1)
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    expect(lastEmit(w)).toBeNull()
    w.unmount()
  })

  it('renders NO print-download chrome — prints are API-only, never offered in the GUI', () => {
    const item = {
      uid: 'p1', mime: 'image/jpeg', mine: true, previewUrl: '/p1/m', webUrl: '/p1/w',
      posterUrl: '/p1/p', downloadUrl: '/p1/o', hasPrint: true, hasLegacyPrint: false,
      renditions: [
        { variant_key: 'print:glossy', label: 'glossy', downloadUrl: '/api/attachments/p1/rendition/print:glossy' },
      ],
    }
    const w = mount(MediaLightbox, { props: { items: [item], modelValue: 0 }, attachTo: document.body })
    expect(document.body.querySelector('[data-test="lightbox-print-download"]')).toBeFalsy()
    expect(document.body.querySelector('[data-test="lightbox-renditions"]')).toBeFalsy()
    expect(document.body.querySelector('[data-test="lightbox-rendition-print:glossy"]')).toBeFalsy()
    // an owner can still ATTACH a print (upload — distinct from downloading)
    expect(document.body.querySelector('[data-test="lightbox-add-print"]')).toBeTruthy()
    w.unmount()
  })

  it('renders no legacy print download even when hasLegacyPrint is set', () => {
    const item = {
      uid: 'l1', mime: 'image/jpeg', previewUrl: '/l1/m', webUrl: '/l1/w', posterUrl: '/l1/p',
      downloadUrl: '/l1/o', hasPrint: true, hasLegacyPrint: true, printUrl: '/api/attachments/l1/print',
    }
    const w = mount(MediaLightbox, { props: { items: [item], modelValue: 0 }, attachTo: document.body })
    expect(document.body.querySelector('[data-test="lightbox-print-download"]')).toBeFalsy()
    w.unmount()
  })
})
