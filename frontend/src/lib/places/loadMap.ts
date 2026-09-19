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
function refetchStylesheet(link: HTMLLinkElement): {
  fresh: HTMLLinkElement
  loaded: Promise<void>
} {
  const fresh = link.cloneNode() as HTMLLinkElement
  const loaded = new Promise<void>((resolve, reject) => {
    fresh.addEventListener('load', () => resolve(), { once: true })
    fresh.addEventListener(
      'error',
      () => reject(new Error(`stylesheet failed again: ${fresh.href}`)),
      { once: true },
    )
  })
  if (link.isConnected) link.replaceWith(fresh)
  else (link.ownerDocument ?? document).head.append(fresh)
  return { fresh, loaded }
}

let repairInFlight: Promise<void> | null = null

/**
 * Put every stylesheet that failed in this document back — once. The pane and the idle warm-up
 * share this loader, so both can want the repair at the same moment, and a caller that walked
 * past an unfinished one would import the chunk with the stylesheet still missing, which Vite's
 * helper will never append again. So every caller waits on the repair that is running, a link
 * leaves the list only when its replacement has LOADED, and a replacement that failed takes its
 * place there: the next attempt tries again, and the rejection still reaches the reload rung.
 */
export function repairFailedStylesheets(): Promise<void> {
  if (repairInFlight) return repairInFlight
  if (failedStylesheets.size === 0) return Promise.resolve()

  const repair = Promise.all(
    [...failedStylesheets].map((link) => {
      const { fresh, loaded } = refetchStylesheet(link)
      return loaded.then(
        () => {
          failedStylesheets.delete(link)
        },
        (error: unknown) => {
          // The burned element is the new one now: the old one is out of the document, and
          // leaving it listed would append a second copy of it on the next attempt.
          failedStylesheets.delete(link)
          failedStylesheets.add(fresh)
          throw error
        },
      )
    }),
  ).then(() => undefined)

  repairInFlight = repair
  const settled = () => {
    if (repairInFlight === repair) repairInFlight = null
  }
  repair.then(settled, settled)
  return repair
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
