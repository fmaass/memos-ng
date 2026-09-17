import { afterEach, describe, expect, it, vi } from 'vitest'

import type { LightboxItem } from '@/lib/lightboxSlides'

import { useMediaLightbox } from '@/composables/useMediaLightbox'

type Lightbox = ReturnType<typeof useMediaLightbox>
type Handlers = Parameters<Lightbox['open']>[2]

// PhotoSwipe itself is the real dependency here (never mocked): the contract under test is which
// buttons THIS composable registers on it and what they call. Only the viewer chrome is asserted —
// the slide maths lives in lightboxSlides and has its own spec.
function item(over: Partial<LightboxItem> = {}): LightboxItem {
  return {
    uid: 'att-1',
    mime: 'image/jpeg',
    mine: true,
    width: 1600,
    height: 1200,
    thumbnailUrl: '/api/assets/att-1/thumb',
    previewUrl: '/api/assets/att-1/preview',
    webUrl: '/api/assets/att-1/web',
    posterUrl: '/api/assets/att-1/poster',
    downloadUrl: '/api/attachments/att-1/original',
    ...over,
  }
}

const lightboxes: Lightbox[] = []

// PhotoSwipe resolves its module and builds its chrome off a microtask, so the viewer's DOM is
// one tick away from open(); waiting for the built-in close button is waiting for "it is open".
async function openViewer(items: LightboxItem[], handlers: Handlers) {
  const lightbox = useMediaLightbox()
  lightboxes.push(lightbox)
  lightbox.open(items, 0, handlers)
  for (let tick = 0; tick < 50 && !document.querySelector('.pswp__button--close'); tick++) {
    await new Promise((resolve) => setTimeout(resolve, 0))
  }
  expect(document.querySelector('.pswp__button--close')).not.toBeNull()
  return lightbox
}

afterEach(() => {
  while (lightboxes.length) lightboxes.pop()?.destroy()
  for (const node of document.querySelectorAll('.pswp')) node.remove()
  // PhotoSwipe refuses to open while `window.pswp` is set, and it is cleared by the animated close
  // — which never completes in happy-dom (no transition ever fires). Without this, every viewer
  // after the first would silently not mount and the next test would assert against nothing.
  delete (window as unknown as { pswp?: unknown }).pswp
})

function showInMemoButton(): HTMLElement | null {
  return document.querySelector('[data-test="pswp-show-in-memo"]')
}

describe('useMediaLightbox — Show in memo', () => {
  it('offers the button and hands the handler the memo the photo hangs on', async () => {
    const onShowInMemo = vi.fn()
    await openViewer([item({ memoUid: 'memo-42' })], { onShowInMemo })

    const button = showInMemoButton()
    expect(button).not.toBeNull()
    button?.click()

    expect(onShowInMemo).toHaveBeenCalledWith('memo-42')
  })

  it('has no button when the caller offers no handler', async () => {
    // Resources and the memo feed open the same viewer with no memo to jump to — a dead button
    // there would be a lie.
    await openViewer([item({ memoUid: 'memo-42' })], {})

    expect(showInMemoButton()).toBeNull()
  })

  it('hides the button on a slide that knows no memo', async () => {
    await openViewer([item({ memoUid: undefined })], { onShowInMemo: vi.fn() })

    expect(showInMemoButton()?.style.display).toBe('none')
  })
})
