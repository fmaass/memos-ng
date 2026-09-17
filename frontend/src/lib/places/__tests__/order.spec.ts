import { describe, expect, it } from 'vitest'

import { newestFirst, viewerTarget } from '@/lib/places/order'

import { marker } from './fixtures'

const old = marker({ uid: 'old', taken_at: '2024-03-01T10:00:00Z' })
const mid = marker({ uid: 'mid', taken_at: '2025-06-01T10:00:00Z' })
const recent = marker({ uid: 'new', taken_at: '2026-09-01T10:00:00Z' })

describe('newestFirst', () => {
  it('orders by taken_at, newest first, without touching the input', () => {
    const input = [old, recent, mid]
    expect(newestFirst(input).map((m) => m.uid)).toEqual(['new', 'mid', 'old'])
    expect(input.map((m) => m.uid)).toEqual(['old', 'new', 'mid'])
  })
})

describe('viewerTarget', () => {
  it('opens the displayed order on the photo that was tapped, not on its position in the input', () => {
    // The grid shows newest first; the viewer must agree, or a tap opens someone else's photo
    // and "Show in memo" then jumps to that other photo's memo.
    const target = viewerTarget([old, recent, mid], 'old')
    expect(target.list.map((m) => m.uid)).toEqual(['new', 'mid', 'old'])
    expect(target.index).toBe(2)
    expect(target.list[target.index]?.uid).toBe('old')
  })

  it('opens the newest photo when that is the one tapped', () => {
    const target = viewerTarget([old, recent], 'new')
    expect(target.index).toBe(0)
    expect(target.list[target.index]?.uid).toBe('new')
  })

  it('falls back to the first slide for a uid that is no longer in the set', () => {
    const target = viewerTarget([old, recent], 'gone')
    expect(target.index).toBe(0)
  })
})
