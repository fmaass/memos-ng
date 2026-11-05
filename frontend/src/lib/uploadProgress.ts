// The `upload.progress` frame of /api/events (MEMNG-965): how far the server has committed an
// upload's bytes, at most once a second per upload. Pure, so the mapping onto the composer tile's
// 'uploading N %' state is unit-tested without an EventSource (happy-dom has none).

export interface UploadProgressEvent {
  upload_uid: string
  bytes_received: number
  declared_size: number
}

// What the frame needs from a tile: the same three fields the composer's UploadItem carries.
export interface ProgressTile {
  status: string
  progress: number
  uploadUid?: string
}

const isCount = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v) && v >= 0

// The frame's data, or null for anything that is not a well-formed progress frame.
export function parseUploadProgress(data: string): UploadProgressEvent | null {
  let body: unknown
  try {
    body = JSON.parse(data)
  } catch {
    return null
  }
  if (typeof body !== 'object' || body === null) return null
  const { upload_uid, bytes_received, declared_size } = body as Record<string, unknown>
  if (typeof upload_uid !== 'string' || !isCount(bytes_received) || !isCount(declared_size)) {
    return null
  }
  return { upload_uid, bytes_received, declared_size }
}

// Rounded as the tile's own PATCH loop rounds it (useResumableUpload.uploadFile), clamped to 0–100;
// an empty file is complete as soon as it is announced.
export function uploadPercent(ev: UploadProgressEvent): number {
  if (ev.declared_size <= 0) return 100
  return Math.min(100, Math.max(0, Math.round((ev.bytes_received / ev.declared_size) * 100)))
}

// The percentage the tile should show after this frame, or null when the frame changes nothing:
// another upload's frame, a tile that is not uploading (queued, done or failed), or a frame behind
// what the tile already shows — the frames are coalesced and can trail the tile's own PATCH answers,
// and a progress bar must never run backwards.
export function applyUploadProgress(tile: ProgressTile, ev: UploadProgressEvent): number | null {
  if (tile.status !== 'uploading' || tile.uploadUid !== ev.upload_uid) return null
  const pct = uploadPercent(ev)
  return pct > tile.progress ? pct : null
}
