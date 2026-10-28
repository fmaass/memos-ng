// @vitest-environment jsdom
// jsdom for sanitizeQrSvg: happy-dom's parser makes DOMPurify drop the root <svg>, a real browser
// does not.
import { describe, expect, it } from 'vitest'

import {
  buildPassCreate,
  detailOf,
  type PassForm,
  passErrorMessage,
  sanitizeQrSvg,
} from '@/lib/tripPasses'

const NOW = new Date(2026, 9, 28, 12, 0, 0)
const form = (over: Partial<PassForm> = {}): PassForm => ({
  label: 'Grandparents',
  scope: 'contribute',
  expiresOn: null,
  maxDevices: 30,
  ...over,
})

describe('buildPassCreate', () => {
  it('trims the label and leaves expiry to the server when no day is picked', () => {
    expect(buildPassCreate(form({ label: '  Oma  ' }), NOW)).toEqual({
      ok: true,
      body: { label: 'Oma', scope: 'contribute', maxDevices: 30 },
    })
  })

  it('refuses an empty or blank label', () => {
    expect(buildPassCreate(form({ label: '' }), NOW).ok).toBe(false)
    expect(buildPassCreate(form({ label: '   ' }), NOW).ok).toBe(false)
  })

  it('takes a 255-character label and refuses 256', () => {
    expect(buildPassCreate(form({ label: 'x'.repeat(255) }), NOW).ok).toBe(true)
    expect(buildPassCreate(form({ label: 'x'.repeat(256) }), NOW).ok).toBe(false)
  })

  it('bounds the device cap to 1..200 integers, defaulting a cleared field to 30', () => {
    expect(buildPassCreate(form({ maxDevices: 0 }), NOW).ok).toBe(false)
    expect(buildPassCreate(form({ maxDevices: 1 }), NOW).ok).toBe(true)
    expect(buildPassCreate(form({ maxDevices: 200 }), NOW).ok).toBe(true)
    expect(buildPassCreate(form({ maxDevices: 201 }), NOW).ok).toBe(false)
    expect(buildPassCreate(form({ maxDevices: 2.5 }), NOW).ok).toBe(false)
    const cleared = buildPassCreate(form({ maxDevices: null }), NOW)
    expect(cleared.ok && cleared.body.maxDevices).toBe(30)
  })

  it('passes the scope through', () => {
    const r = buildPassCreate(form({ scope: 'contribute_view' }), NOW)
    expect(r.ok && r.body.scope).toBe('contribute_view')
  })

  it('sends a picked day as the END of that local day, with its offset', () => {
    const r = buildPassCreate(form({ expiresOn: new Date(2026, 10, 20) }), NOW)
    expect(r.ok).toBe(true)
    if (!r.ok) return
    const at = new Date(r.body.expiresAt!)
    expect([at.getFullYear(), at.getMonth(), at.getDate(), at.getHours(), at.getMinutes()]).toEqual([
      2026, 10, 20, 23, 59,
    ])
    expect(r.body.expiresAt).toMatch(/Z$/)
  })

  it('accepts today (it ends tonight) and refuses yesterday', () => {
    expect(buildPassCreate(form({ expiresOn: new Date(2026, 9, 28) }), NOW).ok).toBe(true)
    expect(buildPassCreate(form({ expiresOn: new Date(2026, 9, 27) }), NOW).ok).toBe(false)
  })
})

describe('passErrorMessage', () => {
  it.each([
    [409, 'trip_has_no_tag', 'no tag'],
    [410, 'device_revoked', 'device is already revoked'],
    [410, 'pass_revoked', 'pass is already revoked'],
    [404, 'Pass not found', 'Not found'],
    [503, 'TRIP_PASS_BASE_URL is not an absolute https origin; no pass was issued', 'not set up'],
    [422, null, 'refused these values'],
    [500, null, 'HTTP 500'],
  ])('%i %s → %s', (status, detail, expected) => {
    expect(passErrorMessage(status, detail)).toContain(expected)
  })
})

describe('detailOf', () => {
  it('reads a string detail and nothing else', () => {
    expect(detailOf({ detail: 'trip_has_no_tag' })).toBe('trip_has_no_tag')
    expect(detailOf({ detail: [{ msg: 'x' }] })).toBeNull()
    expect(detailOf(undefined)).toBeNull()
    expect(detailOf('detail')).toBeNull()
  })
})

describe('sanitizeQrSvg', () => {
  it('keeps the server QR intact', () => {
    const svg =
      '<svg xmlns="http://www.w3.org/2000/svg" width="148" height="148">' +
      '<path transform="scale(4)" stroke="#000" d="M4 4.5h7m1 0h1"></path></svg>'
    expect(sanitizeQrSvg(svg)).toBe(svg)
  })

  it('strips script, handlers and foreign content', () => {
    const out = sanitizeQrSvg(
      '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>alert(2)</script>' +
        '<foreignObject><img src=x onerror="alert(3)"></foreignObject><path d="M0 0h1"/></svg>',
    )
    expect(out).not.toMatch(/onload|onerror|<script|foreignObject|alert/)
    expect(out).toContain('<path d="M0 0h1"></path>')
  })
})
