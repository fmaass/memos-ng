import { useToast } from 'primevue/usetoast'
import { ref } from 'vue'

export interface ShareableItem {
  uid: string
  filename: string
  /** The ORIGINAL (`…/original`), never a derived tier. */
  downloadUrl: string
}

const EXTENSIONS: Record<string, string> = {
  'image/jpeg': 'jpg',
  'image/png': 'png',
  'image/gif': 'gif',
  'image/webp': 'webp',
  'image/heic': 'heic',
  'image/heif': 'heif',
  'image/avif': 'avif',
  'video/mp4': 'mp4',
  'video/quicktime': 'mov',
  'video/webm': 'webm',
}

// Only for an item with no filename: the extension of what the server actually sent.
export function extensionFor(contentType: string): string {
  const type = contentType.split(';')[0]!.trim().toLowerCase()
  const known = EXTENSIONS[type]
  if (known) return known
  const subtype = type.split('/')[1]?.replace(/[^a-z0-9.-]/g, '')
  return subtype || 'bin'
}

// Every original, fetched before anything is shared. The explicit Accept matters: the file server
// negotiates a sibling WebP for a JPEG when the browser's own Accept names WebP, and the share must
// carry the original. One failure and there is no share — a partial set would look complete.
export async function buildShareFiles(items: readonly ShareableItem[]): Promise<File[] | null> {
  const blobs: { item: ShareableItem; blob: Blob }[] = []
  for (const item of items) {
    try {
      const res = await fetch(item.downloadUrl, { credentials: 'include', headers: { Accept: '*/*' } })
      if (!res.ok) return null
      blobs.push({ item, blob: await res.blob() })
    } catch {
      return null
    }
  }
  return blobs.map(
    ({ item, blob }) =>
      new File([blob], item.filename || `${item.uid}.${extensionFor(blob.type)}`, { type: blob.type }),
  )
}

let probe: File | null = null
function canShareFiles(): boolean {
  if (typeof navigator === 'undefined' || typeof navigator.canShare !== 'function') return false
  probe ??= new File([new Uint8Array(1)], 'probe.jpg', { type: 'image/jpeg' })
  try {
    return navigator.canShare({ files: [probe] }) === true
  } catch {
    return false
  }
}

// The share button exists only where the platform can share files (`supported`). A share while one
// is preparing is refused, so a double tap fetches the originals once.
export function useShareSelection() {
  const toast = useToast()
  const supported = canShareFiles()
  const pending = ref(false)

  async function share(items: readonly ShareableItem[]): Promise<void> {
    if (pending.value || items.length === 0) return
    pending.value = true
    try {
      const files = await buildShareFiles(items)
      if (!files) {
        toast.add({ severity: 'error', summary: 'Couldn’t load the originals to share', life: 4000 })
        return
      }
      await navigator.share({ files })
    } catch (e) {
      // Dismissing the share sheet rejects with AbortError; that is the user's answer, not a fault.
      if (!(e instanceof DOMException && e.name === 'AbortError')) {
        toast.add({ severity: 'error', summary: 'Share failed', life: 4000 })
      }
    } finally {
      pending.value = false
    }
  }

  return { supported, pending, share }
}
