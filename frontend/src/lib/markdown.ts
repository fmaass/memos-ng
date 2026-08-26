import DOMPurify from 'dompurify'
import MarkdownIt from 'markdown-it'
import taskLists from 'markdown-it-task-lists'

// Schemes a link target or an image source may keep. One constant for the whole pipeline: the
// markdown-level policy below and the DOMPurify allowlist further down have to agree, or one
// layer keeps what the other drops and the two clients disagree about what a memo says.
const ALLOWED_URI = /^(?:https?|mailto):/i

type RuleInline = Parameters<MarkdownIt['inline']['ruler']['at']>[1]

/** markdown-it's own `image` rule, needed to re-run it with link validation relaxed. */
function stockImageRule(md: MarkdownIt): RuleInline {
  const ruler = md.inline.ruler as unknown as { __rules__?: { name: string; fn: RuleInline }[] }
  const rule = ruler.__rules__?.find((entry) => entry.name === 'image')
  // Fail loudly rather than silently skipping the policy if markdown-it ever renames the rule.
  if (!rule) throw new Error('markdown-it exposes no `image` inline rule to wrap')
  return rule.fn
}

/**
 * The canonical cross-client rendering decisions for targets the SPA refuses to open
 * (MEMNG-144): an image whose source is not an allowed scheme is DROPPED and its alt text shown
 * as plain text, and a link whose target is not an allowed scheme (`ftp:`, `//host`, a relative
 * path) renders as its plain label with no anchor at all. Both are decided here, on the token
 * stream, so the fixture-compared output is the policy's — DOMPurify stays the backstop it was.
 */
function memoTargetPolicy(md: MarkdownIt): void {
  const image = stockImageRule(md)

  // markdown-it ABORTS the image rule when `validateLink` refuses the source, which leaves the
  // whole `![alt](javascript:...)` on screen as literal markdown. Retry the same rule with the
  // validation relaxed, so the image becomes a token the core rule below can reduce to its alt.
  // Relaxing it only inside this retry keeps the strict verdict everywhere else — a
  // `[label](javascript:...)` LINK still stays literal markdown, which the parity fixture pins.
  md.inline.ruler.after('image', 'memo_unsafe_image', (state, silent) => {
    if (state.src.charCodeAt(state.pos) !== 0x21 /* ! */) return false
    const validateLink = md.validateLink.bind(md)
    md.validateLink = () => true
    try {
      return image(state, silent)
    } finally {
      md.validateLink = validateLink
    }
  })

  // Last in the core chain, so linkify's own links (always http/https) and the task-list plugin's
  // checkbox have already been added and are seen as they will be rendered.
  md.core.ruler.push('memo_target_policy', (state) => {
    for (const block of state.tokens) {
      if (block.type !== 'inline' || !block.children) continue
      const kept: typeof block.children = []
      // Links cannot nest in markdown-it, so one pending close is all there can ever be.
      let dropNextClose = false
      for (const token of block.children) {
        if (token.type === 'image' && !ALLOWED_URI.test(token.attrGet('src') ?? '')) {
          const alt = new state.Token('text', '', 0)
          alt.content = md.renderer.renderInlineAsText(token.children ?? [], md.options, state.env)
          kept.push(alt)
          continue
        }
        if (token.type === 'link_open' && !ALLOWED_URI.test(token.attrGet('href') ?? '')) {
          dropNextClose = true
          continue
        }
        if (token.type === 'link_close' && dropNextClose) {
          dropNextClose = false
          continue
        }
        kept.push(token)
      }
      block.children = kept
    }
  })
}

// Memo content is user-authored markdown rendered to HTML, so XSS is the headline risk.
// Two layers: markdown-it with html:false (raw HTML in the source is escaped, never parsed)
// and a DOMPurify pass with a strict allowlist (defence in depth — strips any script/event
// handler/javascript: URL that slipped through). linkify turns bare URLs into safe links.
// task-lists renders `- [ ]` lines as (enabled) checkboxes that MemoCard makes toggleable.
// memo_target_policy applies the cross-client decisions for targets we refuse to open.
const md = new MarkdownIt({ html: false, linkify: true, breaks: true })
  .use(taskLists, { enabled: true })
  .use(memoTargetPolicy)

const ALLOWED_TAGS = [
  'p', 'br', 'strong', 'em', 'b', 'i', 'del', 's', 'code', 'pre',
  'a', 'ul', 'ol', 'li', 'blockquote', 'hr', 'input',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'table', 'thead', 'tbody', 'tr', 'th', 'td',
]
// 'input' is constrained to checkboxes (type/checked/disabled); DOMPurify still strips any
// event-handler attribute, so this only enables task toggles, not an XSS surface.
const ALLOWED_ATTR = ['href', 'title', 'class', 'type', 'checked', 'disabled']

// A line that is ONLY hashtags (e.g. `#curated`, `#curated #trip`) — these already show as tag chips
// below the memo, so rendering them in the body too is redundant ("#curated" reads as a title).
// `#word` requires no space after `#`, so a real markdown heading (`# Heading`, `## Sub`) is preserved,
// and an inline tag in prose ("see #curated") is not a tag-only line and stays.
const TAG_ONLY_LINE = /^[ \t]*#\w+(?:[ \t]+#\w+)*[ \t]*$/

/** True for a line stripTagOnlyLines drops — exported so callers can map rendered output back
 *  to source line numbers (the renderer never sees these lines). */
export function isTagOnlyLine(line: string): boolean {
  return TAG_ONLY_LINE.test(line)
}

/** Drop whole lines that consist solely of hashtags — the tags still appear as chips below. */
export function stripTagOnlyLines(source: string): string {
  return (source ?? '')
    .split('\n')
    .filter((line) => !isTagOnlyLine(line))
    .join('\n')
}

/** Token stream from the SAME markdown-it instance renderMemoMarkdown renders with (task-lists
 *  plugin included). Callers pass stripTagOnlyLines() output — that is what the renderer parses —
 *  and read `token.map` to learn which source line produced which rendered element. */
export function parseMemoMarkdown(text: string) {
  return md.parse(text, {})
}

export function renderMemoMarkdown(source: string): string {
  const rendered = md.render(stripTagOnlyLines(source ?? ''))
  return DOMPurify.sanitize(rendered, {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    // Belt-and-braces: forbid javascript:/data: URLs even if an allowed tag carries one.
    ALLOWED_URI_REGEXP: ALLOWED_URI,
    // A custom ALLOWED_URI_REGEXP is applied to EVERY attribute value DOMPurify does not already
    // hold URI-safe, and `type` is not on that default list — so without this the checkbox loses
    // `type="checkbox"` and the browser draws a text field in every task list.
    ADD_URI_SAFE_ATTR: ['type'],
  })
}
