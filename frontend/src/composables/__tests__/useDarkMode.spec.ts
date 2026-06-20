import { nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// useDarkMode keeps module-level singleton state, so each test re-imports it fresh
// (vi.resetModules) against a clean localStorage + <html> to assert init behaviour.
describe('useDarkMode', () => {
  beforeEach(() => {
    window.localStorage.clear()
    document.documentElement.classList.remove('app-dark')
    vi.resetModules()
  })

  it('defaults to dark, and toggling flips the .app-dark class and persists', async () => {
    const { useDarkMode } = await import('@/composables/useDarkMode')
    const { isDark, toggle } = useDarkMode()

    expect(isDark.value).toBe(true)
    expect(document.documentElement.classList.contains('app-dark')).toBe(true)

    toggle()
    await nextTick()
    expect(isDark.value).toBe(false)
    expect(document.documentElement.classList.contains('app-dark')).toBe(false)
    expect(window.localStorage.getItem('memos-ng:dark')).toBe('0')
  })

  it('respects a persisted light-mode choice on init', async () => {
    window.localStorage.setItem('memos-ng:dark', '0')
    vi.resetModules()
    const { useDarkMode } = await import('@/composables/useDarkMode')
    expect(useDarkMode().isDark.value).toBe(false)
    expect(document.documentElement.classList.contains('app-dark')).toBe(false)
  })
})
