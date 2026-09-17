import { describe, expect, it } from 'vitest'

import { memoPath, toLightboxItem, toLightboxItems } from '@/lib/places/lightbox'

import { marker } from './fixtures'

describe('toLightboxItem', () => {
  it('carries the attachment tiers and the memo the photo belongs to', () => {
    // The memo uid travels ON the slide: the viewer's "Show in memo" button has no other way to
    // know which memo the photo it is showing came from.
    const item = toLightboxItem(marker({ uid: 'att-9', memo_uid: 'memo-9' }))

    expect(item).toMatchObject({
      uid: 'att-9',
      memoUid: 'memo-9',
      mime: 'image/jpeg',
      thumbnailUrl: '/api/assets/att-9/thumb?sig=t',
      previewUrl: '/api/assets/att-9/preview?sig=p',
      webUrl: '/api/assets/att-9/web?sig=w',
      posterUrl: '/api/assets/att-9/poster?sig=o',
      downloadUrl: '/api/attachments/att-9/original',
      width: 4000,
      height: 3000,
      mine: true,
    })
  })

  it('maps a whole list in order', () => {
    const items = toLightboxItems([marker({ uid: 'a' }), marker({ uid: 'b' })])

    expect(items.map((i) => i.uid)).toEqual(['a', 'b'])
  })
})

describe('memoPath', () => {
  it('is the canonical permalink of the photo memo', () => {
    expect(memoPath(marker({ memo_uid: '01J8Z3K9QW7X2E5R6T7Y8U9I0P' }))).toBe(
      '/m/01J8Z3K9QW7X2E5R6T7Y8U9I0P',
    )
  })
})
