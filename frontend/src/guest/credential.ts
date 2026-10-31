// The device credential, kept durably per origin (localStorage) and judged by EPOCH: every
// request remembers the credential that issued it, and a refusal clears the credential only while
// that same credential is still the current one — a stale 401 never ends a newer credential.
//
// When the browser refuses the durable write, the credential is kept for THIS TAB instead
// (sessionStorage), tagged with a digest of the pass link it was redeemed from: a reload of that
// kept link resumes the same device rather than redeeming a new one against the device cap.

import { sha256 } from '@noble/hashes/sha2.js'
import { bytesToHex, utf8ToBytes } from '@noble/hashes/utils.js'

export interface Credential {
  token: string
  deviceId: string
  epoch: number
  /** Whether a reload or a reopened tab will find it again. */
  durable: boolean
  /** For a credential kept only for this tab: the digest of the pass link it was redeemed from. */
  link: string | null
}

export const CREDENTIAL_KEY = 'memos-guest-device'

/** The digest that names a pass link at rest; the secret itself is never stored. */
export function linkDigest(secret: string): string {
  return bytesToHex(sha256(utf8ToBytes(secret)))
}

interface Stored {
  token: string
  deviceId: string
  link: string | null
}

function read(storage: Storage | null): Stored | null {
  const raw = storage?.getItem(CREDENTIAL_KEY)
  const parsed = raw ? (JSON.parse(raw) as { token?: unknown; deviceId?: unknown; link?: unknown }) : null
  if (!parsed || typeof parsed.token !== 'string' || typeof parsed.deviceId !== 'string') return null
  return { token: parsed.token, deviceId: parsed.deviceId, link: typeof parsed.link === 'string' ? parsed.link : null }
}

/** Removes the stored credential only if it is `token` — another tab may have stored a newer one. */
function removeIf(storage: Storage | null, token: string | null): void {
  try {
    const stored = read(storage)
    if (stored && (token === null || stored.token === token)) storage?.removeItem(CREDENTIAL_KEY)
  } catch {
    // nothing to remove
  }
}

export class CredentialStore {
  private current: Credential | null = null
  private epoch = 0

  constructor(
    private readonly storage: Storage | null,
    private readonly tabStorage: Storage | null = null,
  ) {
    // This tab's own credential first: it is newer than a durable one it could not replace.
    try {
      const tab = read(tabStorage)
      if (tab) this.current = { ...tab, epoch: ++this.epoch, durable: false }
    } catch {
      this.current = null
    }
    if (this.current) return
    try {
      const stored = read(storage)
      if (stored) this.current = { token: stored.token, deviceId: stored.deviceId, epoch: ++this.epoch, durable: true, link: null }
    } catch {
      this.current = null // unreadable storage: start without a credential
    }
  }

  get(): Credential | null {
    return this.current
  }

  /** Stores the credential and reports whether it is durable: storage that is missing, refuses
   * the write or does not read it back keeps it for this page only, and the caller must not act
   * as if a reopened tab would find it. Such a credential is kept for this tab, under the digest
   * of `secret` (the pass link it was redeemed from), so a reload of that link finds it; the
   * durable credential it could not replace is dropped, so nothing resumes the older pass. */
  set(token: string, deviceId: string, secret: string | null = null): Credential {
    const replacing = this.current
    const value = JSON.stringify({ token, deviceId })
    let durable = false
    try {
      if (this.storage) {
        this.storage.setItem(CREDENTIAL_KEY, value)
        durable = this.storage.getItem(CREDENTIAL_KEY) === value
      }
    } catch {
      durable = false
    }
    const link = durable || secret === null ? null : linkDigest(secret)
    if (durable) {
      removeIf(this.tabStorage, null)
    } else {
      if (replacing?.durable) removeIf(this.storage, replacing.token)
      try {
        this.tabStorage?.setItem(CREDENTIAL_KEY, JSON.stringify({ token, deviceId, link }))
      } catch {
        // this page only: a reload will not find it
      }
    }
    this.current = { token, deviceId, epoch: ++this.epoch, durable, link }
    return this.current
  }

  isCurrent(issued: Credential): boolean {
    return this.current !== null && this.current.epoch === issued.epoch
  }

  /** Clears the credential only if `issued` is still current — in this page and in storage (a
   * second tab may have stored a newer one). Answers whether it cleared. */
  clearIfCurrent(issued: Credential): boolean {
    if (!this.isCurrent(issued)) return false
    this.current = null
    this.epoch++
    removeIf(this.storage, issued.token)
    removeIf(this.tabStorage, issued.token)
    return true
  }
}
