import { isTagOnlyLine, parseMemoMarkdown } from '@/lib/markdown'

// Which source lines become clickable checkboxes is decided by the renderer (markdown-it +
// markdown-it-task-lists), not by a line-shaped regex: `> - [ ] q` and `1. [ ] o` DO render a
// checkbox, while a literal `- [ ] x` inside a fence or an indented code block does NOT, and
// `- [ ]x` (no space after the box) does not either. A regex that disagrees on any of those
// makes the DOM index (querySelectorAll order) point at the wrong source line. So the
// enumeration below IS the renderer's own parse — one source of truth for both sides.

/** Class the task-lists plugin puts on every rendered checkbox; MemoCard indexes these in the DOM. */
export const TASK_CHECKBOX_CLASS = 'task-list-item-checkbox'

// On a line the renderer certified as a task, everything before the checkbox is indentation,
// blockquote markers and the list marker — none of which can contain `[` — so the first
// `[ ]`/`[x]` after a list marker is the checkbox. Replacing only the mark keeps the `> `
// prefix, the indentation and the bullet/number style intact.
const CHECKBOX_MARK_RE = /^([^[\n]*?(?:[-*+]|\d{1,9}[.)])[ \t]+\[)([ xX])(\])/

/**
 * Source line indices (0-based, into `content.split('\n')`) of the tasks the renderer turns into
 * `.task-list-item-checkbox` inputs, in rendered document order — so element N in the DOM is
 * `taskCheckboxLines(content)[N]` in the source.
 */
export function taskCheckboxLines(content: string): number[] {
  // renderMemoMarkdown parses the tag-stripped source, so token line numbers are indices into
  // the stripped document; keep the map back to the caller's (unstripped) line numbers.
  const sourceLineOf: number[] = []
  const kept: string[] = []
  content.split('\n').forEach((line, index) => {
    if (isTagOnlyLine(line)) return
    sourceLineOf.push(index)
    kept.push(line)
  })

  const lines: number[] = []
  for (const token of parseMemoMarkdown(kept.join('\n'))) {
    if (token.type !== 'inline' || !token.map) continue
    // The plugin unshifts exactly this html_inline checkbox into a task item's inline token;
    // with markdown-it `html: false`, user markdown can never produce an html_inline itself.
    const checkbox = token.children?.[0]
    if (checkbox?.type !== 'html_inline' || !checkbox.content.includes(TASK_CHECKBOX_CLASS)) continue
    const line = sourceLineOf[token.map[0]]
    if (line !== undefined) lines.push(line)
  }
  return lines
}

/**
 * Toggle the Nth rendered task checkbox (`- [ ]` ↔ `- [x]`), N being its index among the
 * `.task-list-item-checkbox` inputs in the rendered HTML. Pure so the toggle is unit-tested;
 * MemoCard PATCHes the result.
 */
export function toggleTask(content: string, index: number): string {
  const target = taskCheckboxLines(content)[index]
  if (target === undefined) return content
  const lines = content.split('\n')
  const line = lines[target]
  if (line === undefined) return content
  lines[target] = line.replace(CHECKBOX_MARK_RE, (_m, pre: string, mark: string, post: string) => {
    const flipped = mark === ' ' ? 'x' : ' '
    return `${pre}${flipped}${post}`
  })
  return lines.join('\n')
}
