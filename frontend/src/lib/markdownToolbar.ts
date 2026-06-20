export type MarkdownAction = 'bold' | 'italic' | 'code' | 'checkbox' | 'link'

export interface ToolbarResult {
  text: string
  selectionStart: number
  selectionEnd: number
}

// Apply a markdown toolbar action to `text` over the [start,end) selection, returning the
// new text and where the selection should sit. Pure so the toolbar behaviour is unit-tested
// without a live <textarea>. No WYSIWYG — these are plain markdown transforms.
export function applyMarkdown(
  text: string,
  start: number,
  end: number,
  action: MarkdownAction,
): ToolbarResult {
  const before = text.slice(0, start)
  const selected = text.slice(start, end)
  const after = text.slice(end)

  const wrap = (left: string, right = left): ToolbarResult => ({
    text: before + left + selected + right + after,
    selectionStart: start + left.length,
    selectionEnd: end + left.length,
  })

  switch (action) {
    case 'bold':
      return wrap('**')
    case 'italic':
      return wrap('_')
    case 'code':
      return wrap('`')
    case 'link':
      return {
        text: `${before}[${selected}](url)${after}`,
        selectionStart: start + 1,
        selectionEnd: end + 1, // selection stays over the link text
      }
    case 'checkbox': {
      // Prefix the line the cursor is on with a task marker.
      const lineStart = before.lastIndexOf('\n') + 1
      const marker = '- [ ] '
      return {
        text: text.slice(0, lineStart) + marker + text.slice(lineStart),
        selectionStart: start + marker.length,
        selectionEnd: end + marker.length,
      }
    }
  }
}
