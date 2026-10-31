import DOMPurify from 'dompurify'

import type { components } from '@/api/schema'

export type PassOut = components['schemas']['PassOut']
export type PassIssued = components['schemas']['PassIssued']
export type PassCreate = components['schemas']['PassCreate']
export type PassDevice = components['schemas']['PassDeviceOut']
export type PassScope = PassCreate['scope']

/** The bounds `PassCreate` declares (app/routers/passes.py): checked here so the sheet answers at
 * once instead of round-tripping a 422 the guest-facing copy cannot explain. */
export const LABEL_MAX = 255
export const MAX_DEVICES_MIN = 1
export const MAX_DEVICES_MAX = 200
export const DEFAULT_MAX_DEVICES = 30

export interface PassForm {
  label: string
  scope: PassScope
  /** A calendar day; the pass lasts to the END of it, local time. Null → the server's default
   * (the later of the trip's span end and now, + 14 days; else 30 days from now). */
  expiresOn: Date | null
  maxDevices: number | null
}

export type BuildResult = { ok: true; body: PassCreate } | { ok: false; error: string }

/** The issue request a filled-in form sends, or why none can be sent. */
export function buildPassCreate(form: PassForm, now: Date = new Date()): BuildResult {
  const label = form.label.trim()
  if (!label) return { ok: false, error: 'Give the pass a label.' }
  if (label.length > LABEL_MAX) {
    return { ok: false, error: `The label is longer than ${LABEL_MAX} characters.` }
  }
  const max = form.maxDevices ?? DEFAULT_MAX_DEVICES
  if (!Number.isInteger(max) || max < MAX_DEVICES_MIN || max > MAX_DEVICES_MAX) {
    return {
      ok: false,
      error: `Allow between ${MAX_DEVICES_MIN} and ${MAX_DEVICES_MAX} devices.`,
    }
  }
  const body: PassCreate = { label, scope: form.scope, maxDevices: max }
  if (form.expiresOn) {
    const d = form.expiresOn
    const end = new Date(d.getFullYear(), d.getMonth(), d.getDate(), 23, 59, 59)
    if (end.getTime() <= now.getTime()) {
      return { ok: false, error: 'The expiry day has already passed.' }
    }
    body.expiresAt = end.toISOString()
  }
  return { ok: true, body }
}

/** A refused pass call, carrying what the server said. */
export class PassError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string | null,
  ) {
    super(passErrorMessage(status, detail))
    this.name = 'PassError'
  }
}

export function detailOf(body: unknown): string | null {
  if (body && typeof body === 'object' && 'detail' in body) {
    const d = (body as { detail: unknown }).detail
    if (typeof d === 'string') return d
  }
  return null
}

/** What the sheet says for each refusal the pass routes document. */
export function passErrorMessage(status: number, detail: string | null): string {
  if (status === 409 && detail === 'trip_has_no_tag') {
    return 'This trip has no tag yet. Give it a tag before inviting guests.'
  }
  if (status === 410 && detail === 'device_revoked') return 'That device is already revoked.'
  if (status === 410) return 'This pass is already revoked.'
  if (status === 404) return 'Not found. It may have been deleted meanwhile.'
  if (status === 503) return 'Guest passes are not set up on this server.'
  if (status === 422) return 'The server refused these values.'
  return `Something went wrong (HTTP ${status}).`
}

/** The server's QR SVG, reduced to SVG markup before it reaches `v-html`. It is our own server's
 * output, but a guest-link sheet is the last place to trust markup blindly. */
export function sanitizeQrSvg(svg: string): string {
  return DOMPurify.sanitize(svg, { USE_PROFILES: { svg: true } })
}

export const SCOPE_LABELS: Record<PassScope, string> = {
  contribute: 'Add pictures',
  contribute_view: 'Add and view pictures',
}

/** The scopes the issue form offers: only those a guest route serves today. `contribute_view` is
 * accepted by the server but no route lets a guest view the trip yet; add it here once one does. */
export const OFFERED_SCOPES: readonly PassScope[] = ['contribute']
