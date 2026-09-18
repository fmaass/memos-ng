import { describe, expect, it } from 'vitest'

import { placesStatus } from '@/lib/places/state'

describe('placesStatus', () => {
  it('is pending while the request is still in flight', () => {
    // The defect this exists for: an unanswered request looks exactly like an empty library if
    // you only count markers, and production's markers answer is over a megabyte.
    expect(placesStatus({ isPending: true, isError: false, markerCount: 0 })).toBe('pending')
  })

  it('is an error of its own when the request failed', () => {
    // "Nothing was found" and "I could not look" are different things to tell someone.
    expect(placesStatus({ isPending: false, isError: true, markerCount: 0 })).toBe('error')
  })

  it('is empty only once the query has settled with nothing', () => {
    expect(placesStatus({ isPending: false, isError: false, markerCount: 0 })).toBe('empty')
  })

  it('is ready as soon as there are markers', () => {
    expect(placesStatus({ isPending: false, isError: false, markerCount: 3 })).toBe('ready')
  })

  it('keeps the pins on screen while a refetch is in flight', () => {
    // A background refetch must not blank the map it is refreshing.
    expect(placesStatus({ isPending: true, isError: false, markerCount: 3 })).toBe('ready')
  })

  it('keeps the pins on screen when a refetch fails', () => {
    // The pins already fetched are still true; a failed refresh is no reason to withdraw them.
    expect(placesStatus({ isPending: false, isError: true, markerCount: 3 })).toBe('ready')
  })
})
