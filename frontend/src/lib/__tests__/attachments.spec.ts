import { describe, expect, it } from 'vitest'

import { attachmentUidsToClear } from '@/lib/attachments'

describe('attachmentUidsToClear', () => {
  it('returns every attachment uid in order', () => {
    const memo = { attachments: [{ uid: 'a' }, { uid: 'b' }, { uid: 'c' }] }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect(attachmentUidsToClear(memo as any)).toEqual(['a', 'b', 'c'])
  })

  it('returns [] when the memo has no attachments', () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect(attachmentUidsToClear({ attachments: [] } as any)).toEqual([])
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect(attachmentUidsToClear({} as any)).toEqual([])
  })
})
