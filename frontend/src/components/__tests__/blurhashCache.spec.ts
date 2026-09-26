import { flushPromises, mount } from '@vue/test-utils'
import { decode } from 'blurhash'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import JustifiedGallery from '@/components/JustifiedGallery.vue'

// The REAL decoder, wrapped so its calls can be counted: every call still decodes the hash.
vi.mock('blurhash', async (importOriginal) => {
  const actual = await importOriginal<typeof import('blurhash')>()
  return { ...actual, decode: vi.fn(actual.decode) }
})

// A valid hash (the reference image from the BlurHash README).
const HASH = 'LEHV6nWB2yk8pyo0adR*.7kCMdnj'

function item(uid: string) {
  return {
    uid,
    mime: 'image/jpeg',
    status: 'ready' as const,
    blurhash: HASH,
    width: 400,
    height: 300,
    thumbnailUrl: `/api/attachments/${uid}/thumb`,
    previewUrl: `/api/attachments/${uid}/mobile`,
    webUrl: `/api/attachments/${uid}/web`,
    posterUrl: `/api/attachments/${uid}/poster`,
  }
}

describe('blurhash placeholder decoding', () => {
  beforeEach(() => {
    vi.mocked(decode).mockClear()
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
    // happy-dom has no canvas: the smallest 2D context the data-URL step needs.
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation((() => ({
      createImageData: (w: number, h: number) => ({ data: new Uint8ClampedArray(w * h * 4) }),
      putImageData: () => {},
    })) as unknown as HTMLCanvasElement['getContext'])
    vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/png;base64,Zm9v')
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('decodes a uid once across two renders of the gallery', async () => {
    // A library scroll remounts tiles constantly (filter flips, page loads, route changes); each
    // decode is ~1 ms of main thread, so it must happen once per photo, not once per render.
    const uid = 'blur-once'
    const first = mount(JustifiedGallery, { props: { items: [item(uid)], targetHeight: 200 } })
    await flushPromises()
    expect(first.html()).toContain('data:image/png;base64,Zm9v')
    first.unmount()

    const second = mount(JustifiedGallery, { props: { items: [item(uid)], targetHeight: 200 } })
    await flushPromises()
    expect(second.html()).toContain('data:image/png;base64,Zm9v')

    expect(vi.mocked(decode)).toHaveBeenCalledTimes(1)
    expect(vi.mocked(decode)).toHaveBeenCalledWith(HASH, 32, 32)
  })
})
