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
    // Inside a code block markdown-it parses no inline at all, so a REFERENCE IMAGE written there
    // survives character for character even though the label above it resolves. A consumer that
    // drops a resolved `![alt][ref]` without asking whether the line is code deletes a line of
    // the author's code, and nothing on screen says it did.
    id: 'code-fenced-tilde-reference-image',
    markdown: '[pic]: https://example.com/red.png\n\n~~~\n![alt][pic]\n~~~',
  },
  {
    // The same barrier from the other side: a destination the sanitiser would strip is still just
    // characters in a code block. A consumer that neutralises it there — by escaping the bracket,
    // say — puts a backslash on screen that the SPA does not show.
    id: 'code-fenced-tilde-unsafe-link',
    markdown: '~~~\n[x](javascript:alert(1))\n~~~',
  },
  {
    // A closing fence cannot carry a container prefix its opener did not have: `> ```` inside a
    // top-level fence is CONTENT, and the block runs on past it. A consumer that reads it as the
    // closer treats everything below as prose and rewrites bytes that are code.
    id: 'code-fenced-quoted-closer-is-content',
    markdown: '```\ncode one\n> ```\n[x](javascript:alert(1))\n```',
  },
  {
    // A BACKTICK fence's info string may not contain a backtick, so this opens no code block at
    // all — both lines are one paragraph, and the link in it is text the sanitiser never sees.
    // (A tilde fence's info may hold anything, backticks included.)
    id: 'code-fenced-backtick-in-info-string',
    markdown: '```a`b\n[x](javascript:alert(1))',
  },
  {
    // An INDENTED code block is a code block too, and it carries no fence to notice.
    id: 'code-indented-reference-image',
    markdown: '[pic]: https://example.com/red.png\n\n    ![alt][pic]',
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
    // An image whose source is not a scheme the SPA will open is DROPPED and its alt text shown as
    // plain text — the reader sees the words, never the markup and never a fetch. (The safe case
    // above stays as it is: the image goes, and its alt with it.) Canonical for both clients,
    // owner decision 2026-08-26.
    id: 'image-unsafe-scheme',
    markdown: '![A red square](javascript:alert(1))\n\n![The chart](data:text/html,hi)',
  },
  {
    id: 'link-reference-image',
    markdown:
      '![shot][pic]\n\nThe reference image above renders as nothing.\n\n[pic]: https://example.com/red.png',
  },
  {
    // A definition markdown-it REFUSES is not a definition: `rules_block/reference.js` returns
    // false when `validateLink` rejects the destination, the paragraph rule takes the line
    // instead, and a link reference definition cannot interrupt a paragraph. So the duplicate
    // directly below it never registers either, and the reference image that names the label is
    // text a reader sees. A consumer that keeps its block-start state across a refused definition
    // resolves the second one and deletes that image.
    id: 'link-reference-duplicate-refused-first',
    markdown:
      '[pic]: javascript:alert(1)\n[pic]: https://example.com/red.png\n\n![alt][pic]',
  },
  {
    // The same rule read from the other end: definition-shaped lines that follow prose are
    // paragraph text, and so is the line after them — including the second one, which is where a
    // scanner that resets its state per line goes wrong.
    id: 'link-reference-in-paragraph',
    markdown:
      'prose\n[a]: https://example.com/a\n[pic]: https://example.com/red.png\n\n![alt][pic]',
  },
  {
    // CommonMark registers a definition written inside a LIST ITEM for the whole document, so a
    // scanner that only knows about blockquote markers is one bullet away from being bypassed.
    id: 'link-reference-in-list-item',
    markdown: '- [evil]: javascript:alert(1)\n\n[click][evil]',
  },
  {
    // An indented code block ends at the first line that is not indented, and a definition may
    // sit on that line — the image below it is one the sanitiser drops, so no client should
    // fetch it.
    id: 'link-reference-after-indented-code',
    markdown:
      '    code line\n[pic]: https://example.com/red.png\n\n![shot][pic]',
  },
  {
    // The same memo written on a device that ends its lines with CRLF. Both renderers normalise
    // the endings before parsing; a consumer that compares a closing fence with the `\r` still
    // on it treats everything below as code and stops checking it.
    id: 'link-reference-crlf-fence',
    markdown:
      '```\r\ncode\r\n```\r\n\r\n[evil]: javascript:alert(1)\r\n\r\n[click][evil]',
  },
  {
    // A tab is whitespace after a list marker exactly as a space is; the definition below it is
    // still a definition, registered for the whole document.
    id: 'link-reference-tab-after-marker',
    markdown: '-\t[evil]: javascript:alert(1)\n\n[click][evil]',
  },
  {
    // A lone carriage return is a line ending in CommonMark, and both renderers normalise it
    // before they parse. A memo written on a device that ends its lines that way is the same memo.
    id: 'link-reference-carriage-return-only',
    markdown: '[evil]: javascript:alert(1)\r\r[click][evil]',
  },
  {
    // Indented code INSIDE a blockquote: the container prefix has to come off before the
    // indentation means anything, or the definition on the next quoted line is never seen.
    id: 'link-reference-in-quoted-indented-code',
    markdown:
      '>     code line\n> [evil]: javascript:alert(1)\n\n[click][evil]',
  },
  {
    // A list item may start with nothing at all, its content beginning on the following line.
    id: 'link-reference-marker-only-item',
    markdown: '-\n  [evil]: javascript:alert(1)\n\n[click][evil]',
  },
  {
    id: 'link-reference-safe',
    markdown: '[site][home] and a collapsed [home][].\n\n[home]: https://example.com/a?b=1',
  },
  {
    // markdown-it validates a link reference definition where it is DEFINED and refuses to
    // register an unsafe one, so the definition line and every reference that names it stay on
    // screen as text. A consumer that only checks the inline form leaves this spelling live.
    id: 'link-reference-unsafe',
    markdown: '[click][evil] and [evil] and [evil][].\n\n[evil]: javascript:alert(1)',
  },
  {
    // The destination may sit on the line after the label; it is validated the same way.
    id: 'link-reference-unsafe-multiline',
    markdown: '[evil]:\njavascript:alert(1)\n\n[click][evil]',
  },
  {
    id: 'link-schemes',
    markdown:
      '[site](https://example.com/a?b=1) and [mail](mailto:memo@example.com) are links;\n\n[js](javascript:alert(1)) and [data](data:text/html,hi) are not.',
  },
  {
    // `ftp:` and a scheme-relative `//host` are destinations markdown-it accepts and the SPA still
    // refuses to open. They render as the LABEL as plain text: no anchor element, and the
    // destination itself nowhere on screen. Canonical for both clients, owner decision 2026-08-26.
    id: 'link-schemes-ftp-and-relative',
    markdown:
      '[the file](ftp://example.com/pub/readme.txt) and [the host](//example.com/path) are not links.',
  },
  {
    id: 'linkify-bare',
    markdown: 'Bare https://example.com/a?b=1 and memo@example.com become links.',
  },
  {
    // A bare domain carrying NO scheme is auto-linked too, to the `http://` form linkify infers —
    // a client that leaves it as text shows the same words with nothing to tap. Canonical for both
    // clients, owner decision 2026-08-26.
    id: 'linkify-bare-domain',
    markdown: 'Visit example.com for the docs.',
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
    // A tag-only line written on a device that ends its lines with CRLF is NOT stripped:
    // `TAG_ONLY_LINE` ends in `[ \t]*$`, and JavaScript's `$` matches at the end of the string
    // only — without `/m` it does not match before a line terminator. So the line keeps its
    // carriage return, fails the test, and renders as body text. A consumer that treats `\r` as
    // trailing whitespace drops a line the SPA shows.
    id: 'tags-carriage-return-kept',
    markdown: '#curated\r\nHello',
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
    // One task item among plain ones: the checkbox belongs to THAT item only. A renderer that
    // draws a checkbox on every item of a list containing one task projects three tasks here
    // instead of list / task / list. Canonical for both clients, owner decision 2026-08-26.
    id: 'task-list-single-task-among-plain',
    markdown: '- plain one\n- [ ] the only task\n- plain two',
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
