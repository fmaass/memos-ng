import { dayKey, LIBRARY_TIME_ZONE } from '@/lib/librarySections'

// The jump from a Months day tile into All (plan decision 3, load-until-present): walk the All
// view's OWN paged list forward until an item of that Zurich day is loaded, and name its first tile.
// Pure over a pager, so the stopping rule is tested without a query, a DOM or a scroll; the view
// scrolls to the uid this resolves.

export interface PageSnapshot<T> {
  /** Every item loaded so far, in list order. */
  items: readonly T[]
  hasNext: boolean
}

export interface DayPager<T> {
  /** What is loaded now; async while a first page is still on its way. */
  current(): PageSnapshot<T> | Promise<PageSnapshot<T>>
  /** Lands the next page and reports the list after it; null when that page failed. */
  fetchNext(): Promise<PageSnapshot<T> | null>
}

// What is already loaded is searched first, and each fetched page is searched before another is
// asked for, so the walk ends on the first page holding the day and never requests the page after
// it. The end of the list or a failed page ends it with null: the caller stays where it is.
// `shouldContinue` is asked after every wait and before every page: a walk a newer jump superseded
// ends there with null, and never puts another request on the wire.
export async function scrollToDay<T extends { uid: string; takenAt: string }>(
  key: string,
  pager: DayPager<T>,
  tz: string = LIBRARY_TIME_ZONE,
  shouldContinue: () => boolean = () => true,
): Promise<string | null> {
  let snapshot: PageSnapshot<T> | null = await pager.current()
  while (snapshot) {
    if (!shouldContinue()) return null
    // The whole list each time: a page joined in flight may have been a refetch that rewrote rows
    // already searched.
    const hit = snapshot.items.find((item) => dayKey(item.takenAt, tz) === key)
    if (hit) return hit.uid
    if (!snapshot.hasNext || !shouldContinue()) return null
    snapshot = await pager.fetchNext()
  }
  return null
}
