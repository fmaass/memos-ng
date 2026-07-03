import { describe, expect, it } from 'vitest'

import { frameTestMessage } from '@/composables/useSettings'

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
