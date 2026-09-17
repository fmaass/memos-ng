// What order the photos are in — and therefore which slide a tap opens. The grid shows newest
// first while the map hands out whatever order the API returned, so the tapped photo is carried
// by UID and the viewer is opened on the very list the viewer's user just looked at. A position
// in the unsorted set would open a different photo, and "Show in memo" would then navigate to
// that other photo's memo.

import type { MapMarker } from '@/lib/places/geo'

export function newestFirst(markers: readonly MapMarker[]): MapMarker[] {
  return [...markers].sort((a, b) => Date.parse(b.taken_at) - Date.parse(a.taken_at))
}

/** The list the viewer opens on, and the slide the tapped photo is in it. */
export function viewerTarget(
  markers: readonly MapMarker[],
  uid: string,
): { list: MapMarker[]; index: number } {
  const list = newestFirst(markers)
  const index = list.findIndex((m) => m.uid === uid)
  return { list, index: index < 0 ? 0 : index }
}
