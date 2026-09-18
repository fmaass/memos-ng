// Which of the map's four states the markers query is actually in.
//
// "No photo here knows where it was taken yet" is a statement of FACT about the library, and it
// was being rendered from the absence of markers alone — so it also claimed it while the request
// was still in flight (1.1 MB on production, seconds on a throttled link) and after the request
// had failed. Three different situations were being told to the viewer as one, and the one thing
// they have in common is the only thing the view could see.
//
// It is a pure function rather than a chain of `v-if`s because that chain is exactly what went
// wrong: each state is named here, once, and tested directly.

export type PlacesStatus = 'pending' | 'error' | 'empty' | 'ready'

export interface MarkersQueryState {
  /** No answer yet — the query has never resolved. */
  isPending: boolean
  /** The last attempt failed. */
  isError: boolean
  /** How many located photos the last successful answer carried. */
  markerCount: number
}

/**
 * Markers already in hand win over every other signal: a refetch in the background, and a
 * refetch that FAILED, both leave the map showing the pins it has rather than replacing them
 * with a spinner or an apology. Beyond that the order is the order of what the viewer is owed —
 * we are still asking, we could not ask, there is genuinely nothing to show.
 */
export function placesStatus({ isPending, isError, markerCount }: MarkersQueryState): PlacesStatus {
  if (markerCount > 0) return 'ready'
  if (isPending) return 'pending'
  if (isError) return 'error'
  return 'empty'
}
