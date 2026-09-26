// One built MapMarker per test case. The shape is the generated `MapMarker` schema type, so a
// field the backend renames breaks these specs rather than letting them assert a stale contract.
import type { MapMarker } from '@/lib/places/geo'

export function marker(over: Partial<MapMarker> = {}): MapMarker {
  const uid = over.uid ?? 'att-1'
  return {
    uid,
    memo_uid: 'memo-1',
    mime: 'image/jpeg',
    status: 'ready',
    blurhash: null,
    width: 4000,
    height: 3000,
    duration: null,
    position: 0,
    sha256: null,
    mine: true,
    hasPrint: false,
    hasLegacyPrint: false,
    renditions: [],
    capture_civil: null,
    capture_instant: null,
    capture_source: null,
    taken_at: '2026-01-01T00:00:00Z',
    latitude: 47.2,
    longitude: 8.4,
    thumbnailUrl: `/api/assets/${uid}/thumb?sig=t`,
    previewUrl: `/api/assets/${uid}/preview?sig=p`,
    webUrl: `/api/assets/${uid}/web?sig=w`,
    webReady: false,
    posterUrl: `/api/assets/${uid}/poster?sig=o`,
    printUrl: `/api/assets/${uid}/print?sig=r`,
    downloadUrl: `/api/attachments/${uid}/original`,
    ...over,
  }
}
