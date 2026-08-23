/**
 * Regenerates `fixtures/markdown-parity.json` — the cross-platform markdown parity fixture.
 * Run it with `npm run gen:markdown-parity` (plain node; TypeScript is stripped at load).
 *
 * This script is the ONLY writer of that file. Every case's Markdown goes through the REAL SPA
 * renderer (`renderMemoMarkdown`, DOMPurify included) under jsdom and is projected by
 * `projectRenderedMarkdown`, so the committed projections are what the SPA actually produces,
 * never hand-written expectations. `src/lib/__tests__/markdown-parity.spec.ts` re-renders the
 * committed inputs and fails on any drift; it never writes.
 *
 * jsdom, not happy-dom: DOMPurify supports jsdom. Without a real `window` at import time
 * DOMPurify sets `isSupported = false` and `sanitize()` silently returns its input unchanged —
 * hence the hard assertion below rather than a hope.
 *
 * The output is byte-deterministic: cases sorted by id, object keys sorted, two-space indent,
 * all non-ASCII escaped as \uXXXX (so no editor or filesystem can renormalize the NFD case),
 * trailing newline. Running it twice must leave the tree clean.
 */
import { createHash } from 'node:crypto'
import { readFileSync, writeFileSync } from 'node:fs'
import { JSDOM } from 'jsdom'

const SCHEMA = 1

interface ParityCase {
  id: string
  markdown: string
}

/**
 * Together these cover every rule in the plan's markdown rendering contract. Keep them small
 * and single-purpose; `memo-mixed` is the one real-shaped memo.
 */
const CASES: ParityCase[] = [
  {
    id: 'blockquote',
    markdown: '> Quoted line one\n> Quoted line two\n\nAfter the quote.',
  },
  {
    id: 'code-fenced',
    markdown: '```js\nconst a = 1\n  indented\n<div>raw</div>\n```',
  },
  {
    id: 'emphasis-and-inline-code',
    markdown: '**bold** and _italic_ and ~~struck~~ and `inline code` in one line.',
  },
  {
    id: 'emphasis-in-list-and-quote',
    markdown: '- ~~done~~ item\n- **bold** item\n\n> a _quoted_ emphasis and `code`',
  },
  {
    id: 'headings-h1-h3',
    markdown: '# Heading one\n\n## Heading two\n\n### Heading three\n\nBody text.',
  },
  {
    id: 'hr',
    markdown: 'Above the rule.\n\n---\n\nBelow the rule.',
  },
  {
    id: 'image',
    markdown: '![A red square](https://example.com/red.png)\n\nThe image above renders as nothing.',
  },
  {
    id: 'link-schemes',
    markdown:
      '[site](https://example.com/a?b=1) and [mail](mailto:memo@example.com) are links;\n\n[js](javascript:alert(1)) and [data](data:text/html,hi) are not.',
  },
  {
    id: 'linkify-bare',
    markdown: 'Bare https://example.com/a?b=1 and memo@example.com become links.',
  },
  {
    id: 'list-ordered',
    markdown: '1. first\n2. second\n3. third',
  },
  {
    id: 'list-unordered-nested',
    markdown: '- alpha\n  - alpha one\n  - alpha two\n- beta',
  },
  {
    id: 'memo-mixed',
    markdown:
      '## Trip planning\n\n#curated #trip\n\nPacked the **camera** and a spare battery, see #gear for the rest.\nFerry leaves at 07:15.\n\n- [x] book tickets\n- [ ] print the [confirmation](https://example.com/booking?id=7)\n\n> Weather looks fine.\n\n---\n\nNotes in `~/trips/2026.md`.',
  },
  {
    id: 'raw-html-literal',
    markdown:
      "<script>alert('x')</script>\n\nAn inline <b>tag</b> and <img src=x onerror=alert(1)> stay literal.",
  },
  {
    id: 'soft-breaks',
    markdown: 'line one\nline two\nline three\n\nA second paragraph.',
  },
  {
    id: 'table',
    markdown:
      '| tier | size | note |\n| --- | --- | --- |\n| thumb | 320 | **fast** |\n| mobile | 1080 | [docs](https://example.com/tiers) |',
  },
  {
    id: 'tags-stripped-vs-inline',
    markdown: '#curated\n\nsee #curated inline\n\n#curated #trip\n\n# Heading is not a tag',
  },
  {
    id: 'task-list-basic',
    markdown: '- [ ] unchecked item\n- [x] checked item\n- plain item',
  },
  {
    id: 'task-list-nested',
    markdown: '- [x] outer done\n  - [ ] inner todo\n  - [x] inner done\n- [ ] outer todo',
  },
  {
    id: 'unicode-nfc-nfd',
    // Same word twice: precomposed U+00E9, then e + combining acute. Both project to NFC.
    markdown: 'caf\u00e9 vs cafe\u0301 \u{1F680}',
  },
  {
    id: 'whitespace-collapsing',
    markdown: 'a   b\t\tc   \n   d',
  },
]

// jsdom globals must exist BEFORE dompurify is evaluated, so the renderer is imported
// dynamically below rather than with a hoisted static import.
const dom = new JSDOM('<!doctype html><html><body></body></html>')
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  DOMParser: dom.window.DOMParser,
  Node: dom.window.Node,
  Element: dom.window.Element,
})

const DOMPurify = (await import('dompurify')).default
if (!DOMPurify.isSupported) {
  throw new Error('DOMPurify is not supported in this environment — it would pass HTML through unsanitized')
}

const { renderMemoMarkdown } = await import('../src/lib/markdown.ts')
const { projectRenderedMarkdown } = await import('../src/lib/markdownParity.ts')

const ids = new Set(CASES.map((entry) => entry.id))
if (ids.size !== CASES.length) throw new Error('duplicate case id')
if (CASES.length < 15) throw new Error(`need at least 15 cases, have ${CASES.length}`)

function readVersion(pkg: string): string {
  const url = new URL(`../node_modules/${pkg}/package.json`, import.meta.url)
  return `${pkg}@${JSON.parse(readFileSync(url, 'utf8')).version}`
}

function readSource(path: string): string {
  return readFileSync(new URL(path, import.meta.url), 'utf8')
}

/**
 * Identifies the inputs the projections depend on: the renderer, the projection, and the three
 * packages that do the parsing/sanitizing. It changes exactly when a regeneration is due.
 */
const generator = [
  readVersion('markdown-it'),
  readVersion('markdown-it-task-lists'),
  readVersion('dompurify'),
  `renderer-sha256:${createHash('sha256')
    .update(readSource('../src/lib/markdown.ts'))
    .update(readSource('../src/lib/markdownParity.ts'))
    .digest('hex')
    .slice(0, 16)}`,
].join('+')

const fixture = {
  schema: SCHEMA,
  generator,
  cases: [...CASES]
    .sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
    .map((entry) => ({
      id: entry.id,
      markdown: entry.markdown,
      projection: projectRenderedMarkdown(renderMemoMarkdown(entry.markdown)),
    })),
}

/** Sorted keys at every level, so a reordered literal above can never churn the file. */
function sortKeys(_key: string, value: unknown): unknown {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return value
  const source = value as Record<string, unknown>
  return Object.fromEntries(Object.keys(source).sort().map((key) => [key, source[key]]))
}

const json = `${JSON.stringify(fixture, sortKeys, 2).replace(
  /[\u007f-\uffff]/g,
  (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, '0')}`,
)}\n`

const target = new URL('../../fixtures/markdown-parity.json', import.meta.url)
writeFileSync(target, json, 'utf8')
console.log(`wrote ${fixture.cases.length} cases to ${target.pathname} (${generator})`)
