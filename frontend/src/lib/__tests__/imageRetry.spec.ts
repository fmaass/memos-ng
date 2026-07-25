import { describe, expect, it } from 'vitest'

import {
  MAX_THUMB_RETRIES,
  nextRetrySrc,
  REMINT_COOLDOWN_MS,
  retryDelayMs,
  shouldRemint,
  withSameBust,
} from '@/lib/imageRetry'

describe('withSameBust', () => {
  it('leaves the other tier alone while no retry is in flight', () => {
    const signed = '/api/attachments/a1/mobile?exp=123&sig=abc'
    expect(withSameBust(signed, '/api/attachments/a1/thumb?exp=123&sig=abc')).toBe(signed)
  })

  it('carries the bust the live src holds, so the srcset candidate re-requests too', () => {
    expect(withSameBust('/api/attachments/a1/mobile?exp=123', '/api/attachments/a1/thumb?exp=123&_r=2')).toBe(
      '/api/attachments/a1/mobile?exp=123&_r=2',
    )
  })

  it('opens a query string when the other tier has none', () => {
    expect(withSameBust('/api/attachments/a1/mobile', '/api/attachments/a1/thumb?_r=1')).toBe(
      '/api/attachments/a1/mobile?_r=1',
    )
  })
})

describe('nextRetrySrc', () => {
  it('appends a cache-bust param to a bare URL on the first failure', () => {
    expect(nextRetrySrc('/api/attachments/a1/thumb', 0)).toBe('/api/attachments/a1/thumb?_r=1')
  })

  it('uses & when the URL already has a query string (signed URLs carry ?exp&scope&sig)', () => {
    const signed = '/api/attachments/a1/thumb?exp=123&scope=7&sig=abc'
    expect(nextRetrySrc(signed, 0)).toBe(`${signed}&_r=1`)
  })

  it('bumps the cache-bust value with the attempt count so each retry is a distinct URL', () => {
    expect(nextRetrySrc('/u', 1)).toBe('/u?_r=2')
  })

  it('returns null once attempts are exhausted (the signal to re-mint, not cache-bust)', () => {
    expect(nextRetrySrc('/u', MAX_THUMB_RETRIES)).toBeNull()
    expect(nextRetrySrc('/u', MAX_THUMB_RETRIES + 1)).toBeNull()
  })
})

describe('retryDelayMs', () => {
  it('backs off the first two thumbnail retries', () => {
    expect(retryDelayMs(0)).toBe(1_000)
    expect(retryDelayMs(1)).toBe(4_000)
  })

  it('returns null once thumbnail retries are exhausted', () => {
    expect(retryDelayMs(MAX_THUMB_RETRIES)).toBeNull()
    expect(retryDelayMs(MAX_THUMB_RETRIES + 1)).toBeNull()
  })
})

describe('shouldRemint', () => {
  it('fires when idle and the cooldown has elapsed', () => {
    expect(shouldRemint(REMINT_COOLDOWN_MS, 0, false)).toBe(true)
  })

  it('is blocked while a refetch is already in flight (coalesce concurrent failures)', () => {
    expect(shouldRemint(REMINT_COOLDOWN_MS * 10, 0, true)).toBe(false)
  })

  it('is blocked inside the cooldown window (no refetch loop during an outage)', () => {
    expect(shouldRemint(1_000, 0, false)).toBe(false)
    expect(shouldRemint(REMINT_COOLDOWN_MS - 1, 0, false)).toBe(false)
  })
})
