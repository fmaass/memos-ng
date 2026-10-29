import { GuestHttpError, type GuestApi, type Limits, type PassInfo } from './api'
import type { Credential, CredentialStore } from './credential'

// Redeem and boot. The pass secret is read from the path once, kept in memory only, and the path
// is replaced by `/p/joined` only AFTER the device credential is stored. `/p/joined` boots from
// the stored credential and never redeems the literal "joined".

export type Phase = 'boot' | 'landing' | 'name' | 'ready' | 'ended' | 'missing' | 'error'

export interface Navigation {
  path(): string
  replace(path: string): void
}

export const JOINED_PATH = '/p/joined'

export function secretFromPath(path: string): string | null {
  const match = /^\/p\/([^/]+)$/.exec(path)
  if (!match || match[1] === 'joined') return null
  try {
    return decodeURIComponent(match[1])
  } catch {
    return null
  }
}

export class GuestSession {
  phase: Phase = 'boot'
  pass: PassInfo | null = null
  limits: Limits | null = null
  displayName = ''
  deviceId: string | null = null
  message: string | null = null
  /** A pass link opened on a device that already holds a credential: offered, never auto-redeemed. */
  otherSecret: string | null = null
  /** False once a credential could not be stored: the pass link stays in the address bar, and a
   * closed tab cannot resume. */
  durable = true
  private secret: string | null
  private redeeming: Promise<void> | null = null

  constructor(
    private readonly api: GuestApi,
    private readonly credentials: CredentialStore,
    private readonly nav: Navigation,
    private readonly ios: boolean,
    private readonly onChange: () => void = () => {},
  ) {
    this.secret = secretFromPath(nav.path())
  }

  async boot(): Promise<void> {
    const credential = this.credentials.get()
    if (credential) {
      await this.loadMe(credential)
      if (this.phase === 'ready' && this.secret) {
        this.otherSecret = this.secret
        this.secret = null
        this.nav.replace(JOINED_PATH)
      }
      if (this.phase !== 'ready' && this.phase !== 'error' && this.secret) this.phase = this.ios ? 'landing' : 'name'
    } else if (this.secret) {
      this.phase = this.ios ? 'landing' : 'name'
    } else {
      this.phase = 'missing'
    }
    this.onChange()
  }

  chooseBrowser(): void {
    if (this.phase === 'landing') this.phase = 'name'
    this.onChange()
  }

  joinOther(): void {
    if (!this.otherSecret) return
    this.secret = this.otherSecret
    this.otherSecret = null
    this.phase = 'name'
    this.onChange()
  }

  private async loadMe(credential: Credential): Promise<void> {
    try {
      const me = await this.api.me(credential.token)
      if (!this.credentials.isCurrent(credential)) return
      this.pass = me.pass
      this.limits = me.limits
      this.displayName = me.display_name
      this.deviceId = me.device_id
      this.phase = 'ready'
    } catch (error) {
      if (!this.credentials.isCurrent(credential)) return
      if (error instanceof GuestHttpError && (error.status === 401 || error.status === 410)) {
        this.refused(credential, error.status)
        return
      }
      this.phase = 'error'
      this.message = 'The connection failed. Reload to try again.'
    }
  }

  /** Ends the session only if `credential` (the one that earned the refusal) is still current. */
  refused(credential: Credential, status: number): void {
    if (!this.credentials.clearIfCurrent(credential)) return
    this.pass = null
    this.limits = null
    this.deviceId = null
    this.phase = 'ended'
    this.message = status === 410 ? 'This pass is no longer open.' : 'This device is no longer part of the pass.'
    this.onChange()
  }

  /** One redeem in flight: a second call JOINS it. */
  redeem(displayName: string): Promise<void> {
    if (this.redeeming) return this.redeeming
    const running = this.doRedeem(displayName.trim()).finally(() => {
      this.redeeming = null
      this.onChange()
    })
    this.redeeming = running
    return running
  }

  private async doRedeem(displayName: string): Promise<void> {
    const secret = this.secret
    if (!secret || !displayName) return
    try {
      const out = await this.api.redeem(secret, displayName)
      const credential = this.credentials.set(out.device_token, out.device_id) // durable FIRST
      this.secret = null
      this.durable = credential.durable
      // Only a credential a reopened tab will find lets the secret leave the address bar;
      // otherwise the pass link is the guest's only way back in.
      if (credential.durable) this.nav.replace(JOINED_PATH)
      this.pass = out.pass
      this.limits = out.limits
      this.deviceId = out.device_id
      this.displayName = displayName
      this.message = null
      this.phase = 'ready'
    } catch (error) {
      if (error instanceof GuestHttpError) {
        if (error.status === 404) this.message = 'This link is not valid.'
        else if (error.status === 410) this.message = 'This pass is no longer open.'
        else if (error.status === 429) this.message = typeof error.detail === 'string' ? error.detail : 'Too many attempts; retry later.'
        else this.message = 'Joining failed. Try again.'
        if (error.status === 404 || error.status === 410) this.phase = 'ended'
      } else {
        this.message = 'The connection failed. Try again.'
      }
    }
  }
}
