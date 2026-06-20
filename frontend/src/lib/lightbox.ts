// Pure lightbox navigation — clamped (no wrap) so the first/last item is a hard stop.
// DOM-free and unit-tested; the component drives keyboard/swipe through stepIndex.

export function stepIndex(current: number, len: number, dir: -1 | 1): number {
  if (len <= 0) return 0
  return Math.max(0, Math.min(len - 1, current + dir))
}

/** Map a keyboard event key to a navigation intent (or null to ignore). */
export function keyToIntent(key: string): 'prev' | 'next' | 'close' | null {
  if (key === 'ArrowLeft') return 'prev'
  if (key === 'ArrowRight') return 'next'
  if (key === 'Escape') return 'close'
  return null
}
