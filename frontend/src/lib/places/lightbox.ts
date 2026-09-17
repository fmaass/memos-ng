// A marker is an attachment plus where it was taken; the viewer wants the attachment and the memo
// it belongs to. Pure so the "Show in memo" contract is tested without a PhotoSwipe DOM.

import type { MapMarker } from '@/lib/places/geo'
import type { LightboxItem } from '@/lib/lightboxSlides'

export function toLightboxItem(marker: MapMarker): LightboxItem {
  return {
    uid: marker.uid,
    memoUid: marker.memo_uid,
    mime: marker.mime,
    mine: marker.mine,
    width: marker.width,
    height: marker.height,
    thumbnailUrl: marker.thumbnailUrl,
    previewUrl: marker.previewUrl,
    webUrl: marker.webUrl,
    posterUrl: marker.posterUrl,
    downloadUrl: marker.downloadUrl,
  }
}

export function toLightboxItems(markers: readonly MapMarker[]): LightboxItem[] {
  return markers.map(toLightboxItem)
}

/** The canonical permalink of the memo a photo hangs on — what "Show in memo" navigates to. */
export function memoPath(marker: Pick<MapMarker, 'memo_uid'>): string {
  return `/m/${marker.memo_uid}`
}
