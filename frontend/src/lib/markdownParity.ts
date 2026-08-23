/**
 * Projection of the SPA's rendered memo HTML into the block structure that
 * `fixtures/markdown-parity.json` compares across platforms.
 *
 * It exists so a second renderer (the iOS client) can be held to the SAME output as
 * `renderMemoMarkdown` without shipping markdown-it: both sides render their own HTML/AST,
 * project it through THIS definition, and deep-equal the result. The definition of record
 * is `fixtures/markdown-parity.schema.md` — change one and you must change the other.
 *
 * Pure and input-only: it takes the already-sanitized HTML string and never renders markdown
 * itself, so it is unit-tested against literal HTML (see __tests__/markdownParity.spec.ts)
 * and needs no markdown-it. The only ambient dependency is `DOMParser`.
 *
 * This module deliberately imports nothing: the generator script loads it directly under
 * plain node (no bundler, so no `@/` alias and no extensionless resolution).
 */

export type ParityBlockType =
  | 'heading'
  | 'paragraph'
  | 'list'
  | 'task'
  | 'code'
  | 'blockquote'
  | 'table'
  | 'hr'

/** Styled span types projected from inline markup. `strike` covers both `<s>` and `<del>`. */
export type ParityInlineType = 'strong' | 'em' | 'strike' | 'code'

export interface ParityLink {
  href: string
  text: string
}

export interface ParityInline {
  type: ParityInlineType
  text: string
}

export interface ParityBlock {
  type: ParityBlockType
  /** Normalized text (NFC, whitespace runs collapsed, trimmed, `<br>` as `\n`); verbatim for `code`. */
  text: string
  /** Ordered `{href, text}` for every anchor in the block whose scheme survived sanitizing. */
  links: ParityLink[]
  /** Ordered styled spans inside the block, in document order of their opening tag. */
  inline: ParityInline[]
  /** Heading level 1-6; list/task nesting depth (0 = top level). */
  level?: number
  /** `list` and `task` blocks: true when the item's list is an `<ol>`. */
  ordered?: boolean
  /** Task items only. */
  checked?: boolean
  /** `list` blocks: the texts of this list's own (non-task) items, in order. */
  items?: string[]
  /** `table` blocks: rows of cell texts, header row first. */
  rows?: string[][]
}

/**
 * Class `markdown-it-task-lists` puts on every rendered checkbox. Mirrors
 * `TASK_CHECKBOX_CLASS` in `tasks.ts`; kept as a local literal so this module stays
 * import-free (the spec asserts the two agree).
 */
const TASK_CHECKBOX_CLASS = 'task-list-item-checkbox'

/**
 * Schemes a link keeps. Mirrors `ALLOWED_URI_REGEXP` in `markdown.ts`: markdown-it's own
 * `validateLink` already refuses `javascript:`/`data:` (they stay literal markdown text) and
 * DOMPurify strips such an `href` if one ever reaches the DOM — this is the third layer, so
 * the projection is correct even when handed raw HTML.
 */
const ALLOWED_LINK_SCHEME = /^(?:https?|mailto):/i

const TEXT_NODE = 3
const ELEMENT_NODE = 1

const INLINE_TYPES: Record<string, ParityInlineType> = {
  strong: 'strong',
  b: 'strong',
  em: 'em',
  i: 'em',
  s: 'strike',
  del: 'strike',
  code: 'code',
}

interface Collected {
  text: string
  links: ParityLink[]
  inline: ParityInline[]
}

function tagOf(el: Element): string {
  return el.tagName.toLowerCase()
}

/**
 * Finish a raw collected string: drop the whitespace that hugs a `<br>`-derived newline,
 * collapse the remaining horizontal runs, trim, normalize to NFC.
 */
function finishText(raw: string): string {
  return raw
    .replace(/[^\S\n]*\n[^\S\n]*/g, '\n')
    .replace(/[^\S\n]+/g, ' ')
    .replace(/^\s+|\s+$/g, '')
    .normalize('NFC')
}

/** Walk `node`'s children, gathering text, links and styled spans in document order. */
function collect(node: Node, skip: (el: Element) => boolean): Collected {
  const out: Collected = { text: '', links: [], inline: [] }
  const children = Array.from(node.childNodes)
  for (const child of children) {
    if (child.nodeType === TEXT_NODE) {
      out.text += (child.nodeValue ?? '').replace(/\s+/g, ' ')
      continue
    }
    if (child.nodeType !== ELEMENT_NODE) continue
    const el = child as Element
    if (skip(el)) continue
    const tag = tagOf(el)
    if (tag === 'br') {
      out.text += '\n'
      continue
    }
    const inner = collect(el, skip)
    // Pre-order: an outer span is listed before the spans nested inside it.
    const inlineType = INLINE_TYPES[tag]
    if (inlineType) out.inline.push({ type: inlineType, text: finishText(inner.text) })
    if (tag === 'a') {
      const href = el.getAttribute('href')
      // An anchor the sanitizer stripped the href from contributes its text only.
      if (href && ALLOWED_LINK_SCHEME.test(href)) {
        out.links.push({ href, text: finishText(inner.text) })
      }
    }
    out.links.push(...inner.links)
    out.inline.push(...inner.inline)
    out.text += inner.text
  }
  return out
}

const SKIP_NOTHING = () => false
const SKIP_NESTED_LISTS = (el: Element) => {
  const tag = tagOf(el)
  return tag === 'ul' || tag === 'ol'
}

function collectBlock(el: Element, skip: (child: Element) => boolean = SKIP_NOTHING): Collected {
  const collected = collect(el, skip)
  return { ...collected, text: finishText(collected.text) }
}

function directChildren(el: Element, ...tags: string[]): Element[] {
  return Array.from(el.children).filter((child) => tags.includes(tagOf(child)))
}

/** The checkbox belonging to THIS item (a loose item nests it in a `<p>`), not to a sublist. */
function ownTaskCheckbox(li: Element): Element | null {
  const inputs = Array.from(li.querySelectorAll(`input.${TASK_CHECKBOX_CLASS}`))
  for (const input of inputs) {
    if (input.closest('li') === li) return input
  }
  return null
}

function projectList(list: Element, depth: number, out: ParityBlock[]): void {
  // `<ol>` vs `<ul>` — the one structural difference the sanitized DOM keeps that the block
  // text cannot show, so a renderer that bullets a numbered list is caught here.
  const ordered = tagOf(list) === 'ol'
  let items: string[] = []
  let links: ParityLink[] = []
  let inline: ParityInline[] = []

  const flush = () => {
    if (items.length === 0) return
    out.push({ type: 'list', level: depth, ordered, text: '', items, links, inline })
    items = []
    links = []
    inline = []
  }

  for (const li of directChildren(list, 'li')) {
    const collected = collectBlock(li, SKIP_NESTED_LISTS)
    const checkbox = ownTaskCheckbox(li)
    if (checkbox) {
      // A task interrupts the run of plain items: emit what came before it first.
      flush()
      out.push({
        type: 'task',
        level: depth,
        ordered,
        checked: checkbox.hasAttribute('checked'),
        text: collected.text,
        links: collected.links,
        inline: collected.inline,
      })
    } else {
      items.push(collected.text)
      links.push(...collected.links)
      inline.push(...collected.inline)
    }

    // Document order: a sublist's blocks follow the blocks of the item that holds it, one
    // level deeper, so which item a sublist hangs off stays readable from the order alone.
    const sublists = directChildren(li, 'ul', 'ol')
    if (sublists.length > 0) {
      flush()
      for (const sublist of sublists) projectList(sublist, depth + 1, out)
    }
  }
  flush()
}

function projectTable(table: Element, out: ParityBlock[]): void {
  const rows: string[][] = []
  const links: ParityLink[] = []
  const inline: ParityInline[] = []
  for (const tr of Array.from(table.querySelectorAll('tr'))) {
    const cells: string[] = []
    for (const cell of Array.from(tr.children)) {
      const tag = tagOf(cell)
      if (tag !== 'th' && tag !== 'td') continue
      const collected = collectBlock(cell)
      cells.push(collected.text)
      links.push(...collected.links)
      inline.push(...collected.inline)
    }
    rows.push(cells)
  }
  out.push({ type: 'table', text: '', rows, links, inline })
}

function projectElement(el: Element, out: ParityBlock[]): void {
  const tag = tagOf(el)

  const heading = /^h([1-6])$/.exec(tag)
  if (heading) {
    const collected = collectBlock(el)
    out.push({
      type: 'heading',
      level: Number(heading[1]),
      text: collected.text,
      links: collected.links,
      inline: collected.inline,
    })
    return
  }

  if (tag === 'hr') {
    out.push({ type: 'hr', text: '', links: [], inline: [] })
    return
  }

  if (tag === 'pre') {
    // Verbatim on purpose: no NFC, no whitespace collapsing, no trim — a code block's
    // indentation and trailing newline ARE its content. Inline spans are not projected
    // inside code (the `<code>` here is the block itself, not styled markup).
    out.push({ type: 'code', text: el.textContent ?? '', links: [], inline: [] })
    return
  }

  if (tag === 'ul' || tag === 'ol') {
    projectList(el, 0, out)
    return
  }

  if (tag === 'table') {
    projectTable(el, out)
    return
  }

  const collected = collectBlock(el)
  // blockquote flattens to text (schema 1 does not project structure inside a quote);
  // anything else the sanitizer let through is projected as a paragraph rather than dropped.
  out.push({
    type: tag === 'blockquote' ? 'blockquote' : 'paragraph',
    text: collected.text,
    links: collected.links,
    inline: collected.inline,
  })
}

/**
 * Project the sanitized HTML of one memo into its ordered parity blocks.
 * `html` is what `renderMemoMarkdown()` returns.
 */
export function projectRenderedMarkdown(html: string): ParityBlock[] {
  const doc = new DOMParser().parseFromString(html, 'text/html')
  const blocks: ParityBlock[] = []
  for (const el of Array.from(doc.body.children)) projectElement(el, blocks)
  return blocks
}
