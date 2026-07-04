import { describe, expect, it } from 'vitest'

import { frameErrorText, frameIsDirty, frameTestMessage } from '@/composables/useSettings'

describe('frameTestMessage', () => {
  it('maps an accepted send to a success banner relaying the server wording', () => {
    const r = frameTestMessage({ ok: true, detail: 'Accepted by the mail server — check your frame.' })
    expect(r.severity).toBe('success')
    expect(r.text).toContain('check your frame')
    // never fabricates a "delivered to the frame" claim — relays the server text verbatim
    expect(r.text.toLowerCase()).not.toContain('delivered')
  })

  it('maps a failure to an error banner carrying the SMTP detail', () => {
    const r = frameTestMessage({ ok: false, detail: '535 bad credentials' })
    expect(r.severity).toBe('error')
    expect(r.text).toBe('535 bad credentials')
  })
})

describe('frameIsDirty', () => {
  it('is clean when buffers equal the persisted values', () => {
    expect(
      frameIsDirty(
        { to: 'f@send.auraframes.com', from: 'me@me.com' },
        { to: 'f@send.auraframes.com', from: 'me@me.com' },
      ),
    ).toBe(false)
  })

  it('treats empty buffers and null persisted as clean (nothing set on either side)', () => {
    expect(frameIsDirty({ to: '', from: '' }, { to: null, from: null })).toBe(false)
    expect(frameIsDirty({ to: '', from: '' }, {})).toBe(false)
  })

  it('is dirty when the send-to buffer differs from persisted', () => {
    expect(
      frameIsDirty({ to: 'new@send.auraframes.com', from: 'me@me.com' }, { to: null, from: 'me@me.com' }),
    ).toBe(true)
  })

  it('is dirty when the send-from buffer differs from persisted', () => {
    expect(
      frameIsDirty(
        { to: 'f@send.auraframes.com', from: 'changed@me.com' },
        { to: 'f@send.auraframes.com', from: 'me@me.com' },
      ),
    ).toBe(true)
  })
})

describe('frameErrorText', () => {
  it('extracts a FastAPI 422 validation message from the detail array', () => {
    const e = { detail: [{ msg: 'must be an @send.auraframes.com address', type: 'value_error' }] }
    expect(frameErrorText(e)).toBe('must be an @send.auraframes.com address')
  })

  it('relays a string detail (a 400 body)', () => {
    expect(frameErrorText({ detail: 'Frame sharing is not configured on the server' })).toBe(
      'Frame sharing is not configured on the server',
    )
  })

  it('falls back to an Error message, then a generic message', () => {
    expect(frameErrorText(new Error('network down'))).toBe('network down')
    expect(frameErrorText(null)).toBe('Could not save the frame settings.')
  })
})
