import { describe, expect, it } from 'vitest'

import { keyToIntent, stepIndex } from '@/lib/lightbox'

describe('stepIndex', () => {
  it('moves within bounds and clamps at the ends', () => {
    expect(stepIndex(0, 5, 1)).toBe(1)
    expect(stepIndex(4, 5, 1)).toBe(4) // clamp at last
    expect(stepIndex(0, 5, -1)).toBe(0) // clamp at first
    expect(stepIndex(2, 5, -1)).toBe(1)
  })
  it('is safe for empty', () => {
    expect(stepIndex(0, 0, 1)).toBe(0)
  })
})

describe('keyToIntent', () => {
  it('maps arrows and escape, ignores others', () => {
    expect(keyToIntent('ArrowLeft')).toBe('prev')
    expect(keyToIntent('ArrowRight')).toBe('next')
    expect(keyToIntent('Escape')).toBe('close')
    expect(keyToIntent('a')).toBeNull()
  })
})
