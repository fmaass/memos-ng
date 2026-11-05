import { describe, expect, it } from 'vitest'

import type { components } from '@/api/schema'
import {
  hasActiveUpload,
  hasInFlight,
  isTerminalStatus,
  nextAction,
  type TimelineData,
} from '@/lib/sseLifecycle'

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

describe('isTerminalStatus', () => {
  // The single classification both the connection lifecycle and the cache patch read: a status is
  // terminal exactly when it is not in flight.
  it.each(['ready', 'failed'] satisfies AttachmentStatus[])('%s is terminal', (status) => {
    expect(isTerminalStatus(status)).toBe(true)
  })

  it.each(['uploading', 'pending', 'processing'] satisfies AttachmentStatus[])(
    '%s is not terminal',
    (status) => {
      expect(isTerminalStatus(status)).toBe(false)
    },
  )
})

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
    { connected: false, inFlight: false, uploading: false, everConnected: false, expected: 'none' },
    { connected: false, inFlight: false, uploading: false, everConnected: true, expected: 'none' },
    { connected: false, inFlight: true, uploading: false, everConnected: false, expected: 'open' },
    { connected: false, inFlight: true, uploading: false, everConnected: true, expected: 'open' },
    { connected: true, inFlight: false, uploading: false, everConnected: false, expected: 'close' },
    { connected: true, inFlight: false, uploading: false, everConnected: true, expected: 'close' },
    { connected: true, inFlight: true, uploading: false, everConnected: false, expected: 'none' },
    { connected: true, inFlight: true, uploading: false, everConnected: true, expected: 'none' },
    { connected: false, inFlight: false, uploading: true, everConnected: false, expected: 'open' },
    { connected: false, inFlight: false, uploading: true, everConnected: true, expected: 'open' },
    { connected: true, inFlight: false, uploading: true, everConnected: true, expected: 'none' },
    { connected: true, inFlight: true, uploading: true, everConnected: true, expected: 'none' },
  ] as const)('returns $expected for %j', ({ expected, ...state }) => {
    expect(nextAction(state)).toBe(expected)
  })
})

describe('an active upload keeps the stream open (MEMNG-1181)', () => {
  it.each(['queued', 'uploading'] as const)('a %s queue item is an active upload', (status) => {
    expect(hasActiveUpload([status])).toBe(true)
    expect(hasActiveUpload(['done', status, 'error'])).toBe(true)
  })

  it.each(['done', 'error'] as const)('a %s queue item is not', (status) => {
    expect(hasActiveUpload([status])).toBe(false)
  })

  it('an empty queue is not', () => {
    expect(hasActiveUpload([])).toBe(false)
  })

  // The bug: the timeline holds only finished media, so the cache alone never asks for the stream,
  // and the upload's own progress frames are sent into a connection nobody opened.
  const idle = timeline(['ready', 'ready'])

  it('opens the stream for an upload started from an idle timeline', () => {
    const state = {
      connected: false,
      everConnected: false,
      inFlight: hasInFlight(idle),
      uploading: hasActiveUpload(['uploading']),
    }
    expect(state.inFlight).toBe(false)
    expect(nextAction(state)).toBe('open')
  })

  it('holds the stream open while the upload runs, and lets it close once it is over', () => {
    const inFlight = hasInFlight(idle)
    const running = { connected: true, everConnected: true, inFlight }
    expect(nextAction({ ...running, uploading: hasActiveUpload(['queued', 'done']) })).toBe('none')
    expect(nextAction({ ...running, uploading: hasActiveUpload(['done', 'error']) })).toBe('close')
  })
})
