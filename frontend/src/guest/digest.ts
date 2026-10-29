import { sha256 } from '@noble/hashes/sha2.js'
import { bytesToHex } from '@noble/hashes/utils.js'

// Full-file SHA-256 read in slices (crypto.subtle cannot stream), plus the digest of the first
// MiB, which only ever NAMES a candidate binding — never decides one.
export const HEAD_BYTES = 1024 * 1024
export const SLICE_BYTES = 4 * 1024 * 1024

export interface Digest {
  sha256: string
  head: string
}

export async function digestFile(
  file: Blob,
  onProgress?: (done: number) => void,
  sliceBytes = SLICE_BYTES,
): Promise<Digest> {
  const hasher = sha256.create()
  for (let offset = 0; offset < file.size; offset += sliceBytes) {
    const chunk = new Uint8Array(await file.slice(offset, offset + sliceBytes).arrayBuffer())
    hasher.update(chunk)
    onProgress?.(Math.min(file.size, offset + sliceBytes))
  }
  const head = sha256(new Uint8Array(await file.slice(0, HEAD_BYTES).arrayBuffer()))
  return { sha256: bytesToHex(hasher.digest()), head: bytesToHex(head) }
}
