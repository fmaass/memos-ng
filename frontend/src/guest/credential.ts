// The device credential, kept durably per origin (localStorage) and judged by EPOCH: every
// request remembers the credential that issued it, and a refusal clears the credential only while
// that same credential is still the current one — a stale 401 never ends a newer credential.

export interface Credential {
  token: string
  deviceId: string
  epoch: number
  /** Whether a reload or a reopened tab will find it again. */
  durable: boolean
}

export const CREDENTIAL_KEY = 'memos-guest-device'

export class CredentialStore {
  private current: Credential | null = null
  private epoch = 0

  constructor(private readonly storage: Storage | null) {
    try {
      const raw = storage?.getItem(CREDENTIAL_KEY)
      const parsed = raw ? (JSON.parse(raw) as { token?: unknown; deviceId?: unknown }) : null
      if (parsed && typeof parsed.token === 'string' && typeof parsed.deviceId === 'string') {
        this.current = { token: parsed.token, deviceId: parsed.deviceId, epoch: ++this.epoch, durable: true }
      }
    } catch {
      this.current = null // unreadable storage: start without a credential
    }
  }

  get(): Credential | null {
    return this.current
  }

  /** Stores the credential and reports whether it is durable: storage that is missing, refuses
   * the write or does not read it back keeps it for this page only, and the caller must not act
   * as if a reload would find it. */
  set(token: string, deviceId: string): Credential {
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
    this.current = { token, deviceId, epoch: ++this.epoch, durable }
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
    try {
      const raw = this.storage?.getItem(CREDENTIAL_KEY)
      const stored = raw ? (JSON.parse(raw) as { token?: unknown }) : null
      if (stored?.token === issued.token) this.storage?.removeItem(CREDENTIAL_KEY)
    } catch {
      // nothing durable to clear
    }
    return true
  }
}
