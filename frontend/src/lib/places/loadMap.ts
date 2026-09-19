/** Stylesheet links whose fetch failed in this document — see `recordStylesheetFailures`. */
const failedStylesheets = new Set<HTMLLinkElement>()

function isStylesheetLink(target: EventTarget | null): target is HTMLLinkElement {
  return target instanceof HTMLLinkElement && target.rel === 'stylesheet'
}

/**
 * Vite's preload helper marks a CSS dependency seen BEFORE its `<link>` has loaded and returns
 * early for a seen one ever after, so a `map-*.css` that failed once — on the idle warm-up, or on
 * a first open of Places — stays failed for the life of the document: the next import brings the
 * chunk over, skips the stylesheet, and MapLibre mounts with its markers and controls
 * unpositioned and nothing on screen to say why. Only the element knows it failed, so the failure
 * has to be caught as it happens.
 */
export function recordStylesheetFailures(doc: Document): void {
  // A resource error is fired at the element and does not bubble; the capture phase is the only
  // place a document-level listener sees it at all.
  doc.addEventListener(
    'error',
    (event) => {
      if (isStylesheetLink(event.target)) failedStylesheets.add(event.target)
    },
    true,
  )
}

/** Only a NEW element asks again: the browser will not re-fetch the link that failed. */
function refetchStylesheet(link: HTMLLinkElement): Promise<void> {
  return new Promise((resolve, reject) => {
    const fresh = link.cloneNode() as HTMLLinkElement
    fresh.addEventListener('load', () => resolve(), { once: true })
    fresh.addEventListener(
      'error',
      () => reject(new Error(`stylesheet failed again: ${fresh.href}`)),
      { once: true },
    )
    if (link.isConnected) link.replaceWith(fresh)
    else (link.ownerDocument ?? document).head.append(fresh)
  })
}

/**
 * Put every stylesheet that failed in this document back. A replacement that fails again is
 * recorded by the same listener, and the rejection is what sends the pane to its reload rung.
 */
export function repairFailedStylesheets(): Promise<void> {
  if (failedStylesheets.size === 0) return Promise.resolve()
  const broken = [...failedStylesheets]
  failedStylesheets.clear()
  return Promise.all(broken.map(refetchStylesheet)).then(() => undefined)
}

/**
 * The one loader the map pane and the idle warm-up both go through, so what was warmed is exactly
 * the chunk the pane later asks for — and so neither of them can mount a map whose stylesheet an
 * earlier attempt has already burned.
 */
export const loadPlacesMap = async () => {
  await repairFailedStylesheets()
  return import('@/components/places/PlacesMap.vue')
}
