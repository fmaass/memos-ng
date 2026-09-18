import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import { createPinElement, PIN_CLASS, PIN_INNER_CLASS } from '@/lib/places/pin'

/** The shipped stylesheet, read as text: the rule that must NOT be there cannot be observed in
 *  happy-dom, which loads no CSS, and it is the rule that decides where every pin is drawn. */
const css = readFileSync('src/lib/places/pin.css', 'utf8')
const block = (selector: string) => {
  const start = css.indexOf(`${selector} {`)
  return start === -1 ? '' : css.slice(start, css.indexOf('}', start))
}

describe('the pin stylesheet', () => {
  it('leaves the marker root to MapLibre, which positions it absolutely', () => {
    // MapLibre sets `position: absolute` + an inline transform on the element it was handed. A
    // `position: relative` here wins on specificity, puts every pin back into normal flow, and the
    // projected translation is then applied on top of a cumulative layout offset.
    expect(block('.places-pin')).not.toMatch(/position\s*:/)
    // The positioning context the badge needs lives on the wrapper instead (positive control).
    expect(block('.places-pin__inner')).toMatch(/position\s*:\s*relative/)
  })

  it('builds a root with no inline positioning of its own', () => {
    expect(createPinElement().style.position).toBe('')
  })
})

describe('the pin element', () => {
  it('keeps the thumbnail and badge inside a wrapper the map does not position', () => {
    const element = createPinElement()
    expect(element.classList.contains(PIN_CLASS)).toBe(true)
    const inner = element.querySelector(`.${PIN_INNER_CLASS}`)
    expect(inner).not.toBeNull()
    // `paint()` finds these by tag: they must stay reachable through the wrapper.
    expect(inner?.querySelector('img')).not.toBeNull()
    expect(inner?.querySelector('span')).not.toBeNull()
  })
})
