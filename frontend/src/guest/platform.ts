// What the page needs to know about where it runs, and the two values the door fills into the
// document (app/ingest/guest.py render_guest_document).

export function isIOS(userAgent: string, maxTouchPoints: number): boolean {
  if (/iPhone|iPad|iPod/.test(userAgent)) return true
  // iPadOS reports a desktop Safari UA; a touch screen gives it away.
  return /Macintosh/.test(userAgent) && maxTouchPoints > 1
}

function meta(doc: Document, name: string): string {
  return doc.querySelector(`meta[name="${name}"]`)?.getAttribute('content') ?? ''
}

/** The install link, or null when the door has none (or the page runs unfilled, in dev). */
export function appUrl(doc: Document): string | null {
  const value = meta(doc, 'memos-app-url')
  return value.startsWith('https://') ? value : null
}

/** The picker's accept list: the server's extensions, or none (the server still refuses). */
export function acceptList(doc: Document): string {
  const value = meta(doc, 'memos-accept')
  return /^(\.[a-z0-9]+)(,\.[a-z0-9]+)*$/.test(value) ? value : ''
}

export function extensionAccepted(name: string, accept: string): boolean {
  if (!accept) return true
  const dot = name.lastIndexOf('.')
  return dot > 0 && accept.split(',').includes(name.slice(dot).toLowerCase())
}
