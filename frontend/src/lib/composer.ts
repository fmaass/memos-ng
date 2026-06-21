// Pure composer helpers — DOM-free so they're unit-tested directly.
import type { MarkdownAction } from '@/lib/markdownToolbar'

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const pad2 = (n: number) => String(n).padStart(2, '0')

/** A backdated memo's display date, e.g. "Jun 21, 2026 14:30" (full, no truncation). */
export function formatMemoDate(d: Date): string {
  return `${MONTHS[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`
}

/** Map a composer keydown to an intent: ⌘/Ctrl+Enter = post; ⌘/Ctrl+B/I/K = a markdown action. */
export function composerKeyAction(
  e: { key: string; metaKey: boolean; ctrlKey: boolean },
): 'save' | MarkdownAction | null {
  if (!(e.metaKey || e.ctrlKey)) return null
  if (e.key === 'Enter') return 'save'
  const map: Record<string, MarkdownAction> = { b: 'bold', i: 'italic', k: 'link' }
  return map[e.key.toLowerCase()] ?? null
}
