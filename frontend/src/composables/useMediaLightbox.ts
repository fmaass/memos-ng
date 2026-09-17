import PhotoSwipeLightbox from 'photoswipe/lightbox'
import { ref } from 'vue'
import PhotoSwipe from 'photoswipe'
import 'photoswipe/style.css'
import './mediaLightbox.css'

import { buildSlides, type LightboxItem, slideForFace, type Slide } from '@/lib/lightboxSlides'
import {
  DEFAULT_PRINT_VARIANT_KEY,
  FACE_LABELS,
  type PhotoFace,
  type PrintMasterInfo,
  PRINT_ROW_LABELS,
  printRowState,
} from '@/lib/printMaster'

export interface AttachPrintPayload {
  uid: string
  file: File
  variantKey: string
  replacing: boolean // the caller confirms a replacement; a first attach needs no confirmation
}

export interface LightboxHandlers {
  onDelete?: (uid: string) => void
  /** Attach or replace the photo's print master (the caller owns any confirmation + upload). */
  onAttachPrint?: (payload: AttachPrintPayload) => void
  onRemovePrint?: (payload: { uid: string; variantKey: string }) => void
  /** Leave the viewer for the memo the current photo hangs on (the Places map opens it this way). */
  onShowInMemo?: (memoUid: string) => void
  /** Resolve a photo's print master (with measured dimensions), or null when it has none. */
  loadPrintMaster?: (uid: string) => Promise<PrintMasterInfo | null>
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
 * inline via a custom content type; the owner gets delete plus the full print-master row (View,
 * Download, Replace, Remove) and a Screen|Print toggle that loads the master itself.
 * Caller invokes `destroy()` on unmount.
 */
export function useMediaLightbox() {
  let lightbox: PhotoSwipeLightbox | null = null
  // Whether the viewer currently owns the screen. Callers with their own keyboard shortcuts (the
  // Places timeline) have to stand back while it is open: PhotoSwipe handles the arrows at
  // document level and the event still reaches a window listener afterwards.
  const isOpen = ref(false)
  let addPrintInput: HTMLInputElement | null = null
  // Re-render the print row from outside (the view refetched and the master may have changed).
  let refreshPrint: (() => void) | null = null

  function open(items: readonly LightboxItem[], index: number, handlers: LightboxHandlers = {}) {
    lightbox?.destroy()
    const screenSlides = buildSlides(items)
    // The dataSource PhotoSwipe reads: a per-slide copy the face toggle swaps in place, with
    // `screenSlides` kept as the untouched original to flip back to.
    const slides = screenSlides.map((s) => ({ ...s }))
    // Per-photo viewer state, scoped to this open() call: which face is showing, and the master
    // itself once resolved (a `null` VALUE means "asked, has none" — distinct from "not asked").
    const faces = new Map<string, PhotoFace>()
    const masters = new Map<string, PrintMasterInfo | null>()
    const inFlight = new Set<string>()
    let syncPrintUi: () => void = () => {}

    const currentItem = () => (lightbox?.pswp ? items[lightbox.pswp.currIndex] : undefined)
    const isPhoto = (item?: LightboxItem) => !!item && item.mime.startsWith('image/')
    const faceOf = (uid: string): PhotoFace => faces.get(uid) ?? 'screen'

    /** Load (once) the master of a photo, then repaint the row. Failures read as "no master". */
    function ensureMaster(uid: string, force = false) {
      if (!handlers.loadPrintMaster) return
      if (inFlight.has(uid) || (masters.has(uid) && !force)) return
      inFlight.add(uid)
      void handlers
        .loadPrintMaster(uid)
        .then((master) => masters.set(uid, master))
        .catch(() => masters.set(uid, null))
        .finally(() => {
          inFlight.delete(uid)
          // A master that is gone (removed, or a reload that says so) must not leave the viewer
          // showing a face that no longer exists — fall back to the screen rendition.
          if (faceOf(uid) === 'print' && masters.get(uid) === null) setFace('screen')
          else syncPrintUi()
        })
    }

    /** Show one face of the current photo — swapping the slide's own data, so zoom follows. */
    function setFace(face: PhotoFace) {
      const pswp = lightbox?.pswp
      const item = currentItem()
      if (!pswp || !item) return
      const master = masters.get(item.uid) ?? null
      if (face === 'print' && master === null) return // nothing to show — the control is disabled
      const i = pswp.currIndex
      faces.set(item.uid, face)
      const base = screenSlides[i]
      if (base) slides[i] = slideForFace(base, face, master)
      pswp.refreshSlideContent(i)
      syncPrintUi()
    }

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

    // --- videos: a clean native <video controls> (Safari/iOS render their own liquid-glass controls) ---
    lightbox.on('contentLoad', (e) => {
      const data = e.content.data as unknown as Slide
      if (data.type !== 'video') return
      e.preventDefault()
      const wrap = document.createElement('div')
      wrap.className = 'pswp-video-wrap' // full-slide flex-center (mediaLightbox.css)
      const video = document.createElement('video')
      video.src = data.src
      if (data.poster) video.poster = data.poster
      video.controls = true
      video.playsInline = true
      video.preload = 'metadata'
      video.className = 'pswp-video'
      wrap.appendChild(video)
      e.content.element = wrap
    })
    // a video isn't pinch-zoomable (swipe still navigates around it) and autoplays on its slide
    lightbox.addFilter('isContentZoomable', (zoomable, content) =>
      (content.data as unknown as Slide).type === 'video' ? false : zoomable,
    )
    lightbox.on('contentActivate', ({ content }) => {
      content.element?.querySelector('video')?.play().catch(() => {})
    })
    lightbox.on('contentDeactivate', ({ content }) => {
      content.element?.querySelector('video')?.pause()
    })

    // --- owner buttons (delete) + download + print-master chrome; all track the current slide ---
    lightbox.on('uiRegister', () => {
      const pswp = lightbox?.pswp
      if (!pswp) return

      pswp.ui?.registerElement({
        name: 'download',
        order: 15, // between zoom (10) and close (20), in the top bar
        isButton: true,
        appendTo: 'bar',
        tagName: 'a',
        // Native PhotoSwipe icon (isCustomSVG → .pswp__icn) so it centers/sizes/shadows exactly
        // like the built-in close/zoom buttons — not a foreign PrimeIcons glyph that floats.
        html: {
          isCustomSVG: true,
          // 24-unit Material path centered (translate +4) in PhotoSwipe's native 32 viewBox, so it
          // renders at the SAME size as the built-in close/zoom icons (size:24 made them ~1.3x big).
          inner:
            '<path transform="translate(4 4)" d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z" id="pswp__icn-download"/>',
          outlineID: 'pswp__icn-download',
        },
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
          order: 16,
          isButton: true,
          appendTo: 'bar',
          html: {
            isCustomSVG: true,
            inner:
              '<path transform="translate(4 4)" d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z" id="pswp__icn-delete"/>',
            outlineID: 'pswp__icn-delete',
          },
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

      if (handlers.onShowInMemo) {
        pswp.ui?.registerElement({
          name: 'show-in-memo',
          order: 17, // after delete (16), still left of close (20)
          isButton: true,
          appendTo: 'bar',
          html: {
            isCustomSVG: true,
            inner:
              '<path transform="translate(4 4)" d="M4 6h16v2H4zm0 5h16v2H4zm0 5h10v2H4z" id="pswp__icn-show-in-memo"/>',
            outlineID: 'pswp__icn-show-in-memo',
          },
          onInit: (el) => {
            el.setAttribute('aria-label', 'Show in memo')
            el.dataset.test = 'pswp-show-in-memo'
            // A photo reached from Resources carries no memo: the button would navigate nowhere.
            const sync = () => {
              el.style.display = items[pswp.currIndex]?.memoUid ? 'block' : 'none'
            }
            pswp.on('change', sync)
            sync()
          },
          onClick: () => {
            const memoUid = items[pswp.currIndex]?.memoUid
            if (!memoUid) return
            handlers.onShowInMemo?.(memoUid)
            pswp.close()
          },
        })
      }

      // --- print master: a Screen|Print toggle in the bar + the master's own row below it ---
      if (handlers.loadPrintMaster) {
        let faceToggleEl: HTMLElement | null = null
        let faceButtons: HTMLButtonElement[] = []
        let rowEl: HTMLElement | null = null
        let titleEl: HTMLElement | null = null
        let metaEl: HTMLElement | null = null
        let actionsEl: HTMLElement | null = null

        pswp.ui?.registerElement({
          name: 'face-toggle',
          className: 'pswp-face-toggle',
          order: 14, // ahead of download (15), so the two faces read as one control with the bar
          isButton: false,
          tagName: 'div',
          appendTo: 'bar',
          onInit: (el) => {
            el.setAttribute('role', 'group')
            el.setAttribute('aria-label', 'Which face of this photo to show')
            faceButtons = (['screen', 'print'] as const).map((face) => {
              const button = document.createElement('button')
              button.type = 'button'
              button.className = 'pswp-face-toggle__option'
              button.dataset.face = face
              button.textContent = FACE_LABELS[face]
              button.addEventListener('click', () => setFace(face))
              el.appendChild(button)
              return button
            })
            faceToggleEl = el
          },
        })

        pswp.ui?.registerElement({
          name: 'print-row',
          className: 'pswp-print-row',
          isButton: false,
          tagName: 'div',
          appendTo: 'root', // a strip of its own: four verbs and a metadata line don't fit the bar
          onInit: (el) => {
            titleEl = document.createElement('span')
            titleEl.className = 'pswp-print-row__title'
            metaEl = document.createElement('span')
            metaEl.className = 'pswp-print-row__meta'
            actionsEl = document.createElement('div')
            actionsEl.className = 'pswp-print-row__actions'
            el.append(titleEl, metaEl, actionsEl)
            rowEl = el
          },
        })

        syncPrintUi = () => {
          const item = currentItem()
          const uid = item?.uid
          const master = uid ? (masters.get(uid) ?? null) : null
          const state = printRowState({
            isPhoto: isPhoto(item),
            resolved: uid ? masters.has(uid) : false,
            mine: item?.mine ?? false,
            master,
            canAttach: !!handlers.onAttachPrint,
            canRemove: !!handlers.onRemovePrint,
          })
          const face = uid ? faceOf(uid) : 'screen'

          if (faceToggleEl) {
            faceToggleEl.style.display = state.faceToggle ? '' : 'none'
            for (const button of faceButtons) {
              const on = button.dataset.face === face
              button.classList.toggle('is-on', on)
              button.setAttribute('aria-pressed', String(on))
            }
          }
          if (!rowEl || !titleEl || !metaEl || !actionsEl) return
          if (!state.visible || !uid) {
            rowEl.style.display = 'none'
            return
          }
          rowEl.style.display = ''
          titleEl.textContent = state.title
          metaEl.textContent = state.meta
          actionsEl.replaceChildren()
          for (const action of state.actions) {
            const label = PRINT_ROW_LABELS[action]
            if (action === 'download' && master) {
              const download = document.createElement('a')
              download.className = 'pswp-print-row__btn'
              download.href = master.downloadUrl
              download.setAttribute('download', '')
              download.textContent = label
              actionsEl.appendChild(download)
              continue
            }
            const button = document.createElement('button')
            button.type = 'button'
            // Replace/Attach carry the accent: they are the verb the row exists for.
            const primary = action === 'replace' || action === 'attach'
            button.className = `pswp-print-row__btn${primary ? ' is-primary' : ''}`
            button.textContent = label
            button.addEventListener('click', () => {
              if (action === 'view') setFace('print')
              // Replacing keeps the master's OWN key, so a `print:glossy` photo does not silently
              // gain a second, competing master under the bare `print` key.
              else if (action === 'replace' && master) pickMaster(uid, master.variantKey, true)
              else if (action === 'attach') pickMaster(uid, DEFAULT_PRINT_VARIANT_KEY, false)
              else if (action === 'remove' && master)
                handlers.onRemovePrint?.({ uid, variantKey: master.variantKey })
            })
            actionsEl.appendChild(button)
          }
        }

        pswp.on('change', () => {
          const item = currentItem()
          if (isPhoto(item) && item) ensureMaster(item.uid)
          syncPrintUi()
        })
        const first = items[index]
        if (isPhoto(first) && first) ensureMaster(first.uid)
        syncPrintUi()
      }
    })

    function pickMaster(uid: string, variantKey: string, replacing: boolean) {
      openPrintPicker(uid, (pickedUid, file) =>
        handlers.onAttachPrint?.({ uid: pickedUid, file, variantKey, replacing }),
      )
    }

    // The view refetched (an upload landed, a master was removed): re-ask for the current photo's
    // master and repaint. Forced, because the answer this viewer cached is exactly what changed.
    refreshPrint = () => {
      const item = currentItem()
      if (isPhoto(item) && item) ensureMaster(item.uid, true)
      syncPrintUi()
    }

    lightbox.on('close', () => {
      isOpen.value = false
    })
    lightbox.init()
    lightbox.loadAndOpen(index)
    isOpen.value = true
  }

  // A reusable hidden file input for picking a print master.
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
    isOpen.value = false
    refreshPrint = null
    addPrintInput?.remove()
    addPrintInput = null
  }

  /** Re-read the open photo's print master (after the view's own data changed). No-op when closed. */
  function refreshPrintMaster() {
    refreshPrint?.()
  }

  return { open, destroy, refreshPrintMaster, isOpen }
}
