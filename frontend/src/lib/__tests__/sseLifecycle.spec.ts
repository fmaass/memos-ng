import { describe, expect, it } from 'vitest'

import type { components } from '@/api/schema'
import { hasInFlight, nextAction, type TimelineData } from '@/lib/sseLifecycle'

type AttachmentStatus = components['schemas']['AttachmentStatus']
type MemoOut = components['schemas']['MemoOut']

function timeline(statuses: AttachmentStatus[]): TimelineData {
  return {
    pages: [
      {
        items: statuses.map(
          (status, i) =>
            ({
              uid: `m${i}`,
              attachments: [{ uid: `a${i}`, status }],
            }) as MemoOut,
        ),
        nextCursor: null,
        totalCount: statuses.length,
      },
    ],
    pageParams: [undefined],
  }
}

describe('hasInFlight', () => {
  it.each(['uploading', 'pending', 'processing'] satisfies AttachmentStatus[])(
    'returns true for %s attachments',
    (status) => {
      expect(hasInFlight(timeline([status]))).toBe(true)
    },
  )

  it.each(['ready', 'failed'] satisfies AttachmentStatus[])(
    'returns false for terminal %s attachments',
    (status) => {
      expect(hasInFlight(timeline([status]))).toBe(false)
    },
  )

  it('returns false for empty and undefined timeline data', () => {
    expect(hasInFlight(timeline([]))).toBe(false)
    expect(hasInFlight(undefined)).toBe(false)
  })
})

describe('nextAction', () => {
  it.each([
    { connected: false, inFlight: false, everConnected: false, expected: 'none' },
    { connected: false, inFlight: false, everConnected: true, expected: 'none' },
    { connected: false, inFlight: true, everConnected: false, expected: 'open' },
    { connected: false, inFlight: true, everConnected: true, expected: 'open' },
    { connected: true, inFlight: false, everConnected: false, expected: 'close' },
    { connected: true, inFlight: false, everConnected: true, expected: 'close' },
    { connected: true, inFlight: true, everConnected: false, expected: 'none' },
    { connected: true, inFlight: true, everConnected: true, expected: 'none' },
  ] as const)('returns $expected for %j', ({ expected, ...state }) => {
    expect(nextAction(state)).toBe(expected)
  })
})
