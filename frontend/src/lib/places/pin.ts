// The DOM a map pin is made of, and where its landing animation may live.
//
// MapLibre positions a Marker by writing `transform: translate(...)` on the element it was handed.
// A CSS animation that also animates `transform` overrides that inline style for as long as it
// runs, so animating the marker ROOT would play the landing at the map container's origin and snap
// the pin to its coordinate at the end. The animation therefore belongs to an inner wrapper that
// MapLibre never touches — and this module is where that rule is stated and tested.

import './pin.css'

export const PIN_CLASS = 'places-pin'
export const PIN_INNER_CLASS = 'places-pin__inner'
export const LANDING_CLASS = 'places-pin--landing'

/** The pin's DOM: a button MapLibre positions, wrapping the parts that may animate. */
export function createPinElement(): HTMLElement {
  const element = document.createElement('button')
  element.type = 'button'
  element.className = PIN_CLASS
  element.dataset.test = 'places-pin'
  const inner = document.createElement('span')
  inner.className = PIN_INNER_CLASS
  const image = document.createElement('img')
  image.className = 'places-pin__thumb'
  image.alt = ''
  const badge = document.createElement('span')
  badge.className = 'places-pin__badge'
  inner.append(image, badge)
  element.append(inner)
  return element
}

/** Put the landing animation on (or take it off) the wrapper — never the positioned root. */
export function setLanding(element: HTMLElement, landing: boolean): void {
  const inner = element.querySelector(`.${PIN_INNER_CLASS}`)
  if (!inner) return
  inner.classList.remove(LANDING_CLASS)
  if (!landing) return
  // Re-adding a class the element already carries does not replay a CSS animation; a reflow
  // between the remove and the add does.
  void (inner as HTMLElement).offsetWidth
  inner.classList.add(LANDING_CLASS)
}
