import { describe, expect, it } from 'vitest'

import { notificationFor } from '@/lib/pushNotification'

describe('notificationFor', () => {
  it('announces ready media and deep-links to the attachment', () => {
    const n = notificationFor({ uid: 'abc', status: 'ready', title: 'Beach.mov' })
    expect(n.title).toBe('Beach.mov')
    expect(n.body).toBe('Your media is ready')
    expect(n.url).toBe('/?attachment=abc')
  })

  it('announces a processing failure', () => {
    expect(notificationFor({ uid: 'x', status: 'failed' }).body).toBe('Media processing failed')
  })

  it('falls back to a generic title and home link when fields are missing', () => {
    const n = notificationFor({})
    expect(n.title).toBe('memos-ng')
    expect(n.body).toBe('Update available')
    expect(n.url).toBe('/')
  })
})
