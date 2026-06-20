import DOMPurify from 'dompurify'
import MarkdownIt from 'markdown-it'

// Memo content is user-authored markdown rendered to HTML, so XSS is the headline risk.
// Two layers: markdown-it with html:false (raw HTML in the source is escaped, never parsed)
// and a DOMPurify pass with a strict allowlist (defence in depth — strips any script/event
// handler/javascript: URL that slipped through). linkify turns bare URLs into safe links.
const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

const ALLOWED_TAGS = [
  'p', 'br', 'strong', 'em', 'b', 'i', 'del', 's', 'code', 'pre',
  'a', 'ul', 'ol', 'li', 'blockquote', 'hr',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'table', 'thead', 'tbody', 'tr', 'th', 'td',
]
const ALLOWED_ATTR = ['href', 'title', 'class']

export function renderMemoMarkdown(source: string): string {
  const rendered = md.render(source ?? '')
  return DOMPurify.sanitize(rendered, {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    // Belt-and-braces: forbid javascript:/data: URLs even if an allowed tag carries one.
    ALLOWED_URI_REGEXP: /^(?:https?|mailto):/i,
  })
}
