import PhotoSwipeLightbox from 'photoswipe/lightbox'
import PhotoSwipe from 'photoswipe'
import 'photoswipe/style.css'

import { buildSlides, type LightboxItem, type Slide } from '@/lib/lightboxSlides'

export interface LightboxHandlers {
  onDelete?: (uid: string) => void
  onAddPrint?: (uid: string, file: File) => void
}

// Read an iOS safe-area inset (px) off the document so PhotoSwipe's chrome clears the status bar /
// home indicator in the standalone PWA. Falls back to 0 (env() resolves to 0 off-PWA / older browsers).
function safeInset(side: 'top' | 'bottom' | 'left' | 'right'): number {
  if (typeof getComputedStyle === 'undefined') return 0
  const probe = document.createElement('div')
  probe.style.cssText = `position:fixed;height:env(safe-area-inset-${side});`
  document.body.appendChild(probe)
  const px = parseFloat(getComputedStyle(probe).height) || 0
  probe.remove()
  return px
}

/**
 * Full-screen media viewer backed by PhotoSwipe v5 (pinch-zoom, swipe, momentum, a11y, safe-area).
 * Replaces the hand-rolled MediaLightbox. `open(items, index, handlers)` opens at `index`; videos play
 * inline via a custom content type; the owner gets delete + add-print buttons. Print masters are
 * API-only — no print-download chrome. Caller invokes `destroy()` on unmount.
 */
export function useMediaLightbox() {
  let lightbox: PhotoSwipeLightbox | null = null
  let addPrintInput: HTMLInputElement | null = null

  function open(items: readonly LightboxItem[], index: number, handlers: LightboxHandlers = {}) {
    lightbox?.destroy()
    const slides = buildSlides(items)

    lightbox = new PhotoSwipeLightbox({
      dataSource: slides as unknown as PhotoSwipeLightbox['options']['dataSource'],
      pswpModule: () => Promise.resolve({ default: PhotoSwipe }),
      bgOpacity: 0.92,
      // clear the iOS status bar / home indicator in the standalone PWA
      paddingFn: () => ({
        top: safeInset('top'),
        bottom: safeInset('bottom'),
        left: safeInset('left'),
        right: safeInset('right'),
      }),
    })

    // --- videos: render a <video> for slides marked type:'video' (images use PhotoSwipe's default) ---
    lightbox.on('contentLoad', (e) => {
      const data = e.content.data as unknown as Slide
      if (data.type !== 'video') return
      e.preventDefault()
      const wrap = document.createElement('div')
      wrap.className = 'pswp__video-wrap flex h-full w-full items-center justify-center'
      const video = document.createElement('video')
      video.src = data.src
      if (data.poster) video.poster = data.poster
      video.controls = true
      video.playsInline = true
      video.preload = 'metadata'
      video.className = 'max-h-full max-w-full'
      wrap.appendChild(video)
      e.content.element = wrap
    })
    // custom content isn't pinch-zoomable; let the swipe still work around the video
    lightbox.addFilter('isContentZoomable', (zoomable, content) => {
      return (content.data as unknown as Slide).type === 'video' ? false : zoomable
    })

    // --- owner buttons (delete, add-print) + download; visibility tracks the current slide ---
    lightbox.on('uiRegister', () => {
      const pswp = lightbox?.pswp
      if (!pswp) return

      pswp.ui?.registerElement({
        name: 'download',
        order: 8,
        isButton: true,
        tagName: 'a',
        html: '<i class="pi pi-download" aria-hidden="true"></i>',
        onInit: (el) => {
          const a = el as HTMLAnchorElement
          a.setAttribute('aria-label', 'Download original')
          a.setAttribute('download', '')
          const sync = () => {
            const cur = items[pswp.currIndex]
            if (cur) a.href = cur.downloadUrl
          }
          pswp.on('change', sync)
          sync()
        },
      })

      if (handlers.onDelete) {
        pswp.ui?.registerElement({
          name: 'delete',
          order: 9,
          isButton: true,
          html: '<i class="pi pi-trash" aria-hidden="true"></i>',
          onInit: (el) => {
            el.setAttribute('aria-label', 'Delete')
            const sync = () => {
              el.style.display = (items[pswp.currIndex]?.mine ?? false) ? 'block' : 'none'
            }
            pswp.on('change', sync)
            sync()
          },
          onClick: () => {
            const cur = items[pswp.currIndex]
            if (cur?.uid) {
              handlers.onDelete?.(cur.uid)
              pswp.close()
            }
          },
        })
      }

      if (handlers.onAddPrint) {
        pswp.ui?.registerElement({
          name: 'add-print',
          order: 10,
          isButton: true,
          html: '<i class="pi pi-print" aria-hidden="true"></i>',
          onInit: (el) => {
            el.setAttribute('aria-label', 'Attach a print-quality master')
            const sync = () => {
              const cur = items[pswp.currIndex]
              const ok = !!cur && (cur.mine ?? false) && cur.mime.startsWith('image/')
              el.style.display = ok ? 'block' : 'none'
            }
            pswp.on('change', sync)
            sync()
          },
          onClick: () => {
            const cur = items[pswp.currIndex]
            if (cur) openPrintPicker(cur.uid, handlers.onAddPrint!)
          },
        })
      }
    })

    lightbox.init()
    lightbox.loadAndOpen(index)
  }

  // A reusable hidden file input for the add-print button.
  function openPrintPicker(uid: string, cb: (uid: string, file: File) => void) {
    if (!addPrintInput) {
      addPrintInput = document.createElement('input')
      addPrintInput.type = 'file'
      addPrintInput.accept = 'image/*,.tif,.tiff'
      addPrintInput.style.display = 'none'
      document.body.appendChild(addPrintInput)
    }
    const input = addPrintInput
    input.onchange = () => {
      const file = input.files?.[0]
      if (file) cb(uid, file)
      input.value = ''
    }
    input.click()
  }

  function destroy() {
    lightbox?.destroy()
    lightbox = null
    addPrintInput?.remove()
    addPrintInput = null
  }

  return { open, destroy }
}
