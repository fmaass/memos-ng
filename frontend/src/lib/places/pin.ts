// The DOM a map pin is made of.
//
// MapLibre positions a Marker by writing `transform: translate(...)` on the element it was handed,
// and a rule of ours on that same element would either re-flow the pin or override its projection.
// Everything that needs a positioning context therefore belongs to an inner wrapper MapLibre never
// touches — and this module is where that rule is stated and tested.

import './pin.css'

export const PIN_CLASS = 'places-pin'
export const PIN_INNER_CLASS = 'places-pin__inner'

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
