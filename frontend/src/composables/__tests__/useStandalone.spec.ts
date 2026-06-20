import { afterEach, describe, expect, it, vi } from 'vitest'

import { useStandalone } from '@/composables/useStandalone'

afterEach(() => vi.restoreAllMocks())

describe('useStandalone', () => {
  it('is true when running in standalone display-mode', () => {
    vi.spyOn(window, 'matchMedia').mockReturnValue({ matches: true } as MediaQueryList)
    expect(useStandalone().isStandalone.value).toBe(true)
  })

  it('is false in a normal browser tab', () => {
    vi.spyOn(window, 'matchMedia').mockReturnValue({ matches: false } as MediaQueryList)
    expect(useStandalone().isStandalone.value).toBe(false)
  })
})
