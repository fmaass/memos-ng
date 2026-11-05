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

  it('announces a live memo by its owner and deep-links to the memo (MEMNG-967)', () => {
    const n = notificationFor({
      kind: 'memo_live',
      uid: 'm1',
      owner: 'Olga',
      title: 'Beach day',
      attachments: 30,
    })
    expect(n).toEqual({ title: 'Olga posted', body: 'Beach day · 30 files', url: '/m/m1' })
  })

  it('words a live memo without a title or with one file', () => {
    expect(notificationFor({ kind: 'memo_live', uid: 'm2', owner: 'Mia', title: '', attachments: 1 }))
      .toEqual({ title: 'Mia posted', body: '1 file', url: '/m/m2' })
    expect(notificationFor({ kind: 'memo_live', uid: 'm 3', owner: 'Mia', title: 'Hi', attachments: 0 }))
      .toEqual({ title: 'Mia posted', body: 'Hi', url: '/m/m%203' })
  })

  it('reads the kind field only, never a file that happens to be named memo_live', () => {
    const n = notificationFor({ uid: 'a1', status: 'ready', title: 'memo_live' })
    expect(n).toEqual({ title: 'memo_live', body: 'Your media is ready', url: '/?attachment=a1' })
  })

  it('falls back to a generic title and home link when fields are missing', () => {
    const n = notificationFor({})
    expect(n.title).toBe('memos-ng')
    expect(n.body).toBe('Update available')
    expect(n.url).toBe('/')
  })
})
