# `markdown-parity.json` — schema 1

The contract that keeps a second markdown renderer honest against the SPA's.

memos-ng renders memo markdown in the browser with markdown-it + DOMPurify
(`frontend/src/lib/markdown.ts`). The native iOS client renders the same markdown with a
different engine. Rather than compare HTML — which two engines will never agree on
byte-for-byte — both sides **project** their rendered output into the block structure defined
here and compare that. A renderer that flattens structure, drops task state, keeps a
`javascript:` link tappable or leaves `~~struck~~` unstyled cannot produce a matching
projection.

## Who writes it, who reads it

| | |
| --- | --- |
| **Sole writer** | `frontend/scripts/gen-markdown-parity.ts` (`npm run gen:markdown-parity`) |
| **Drift gate (web)** | `frontend/src/lib/__tests__/markdown-parity.spec.ts` — re-renders every committed `markdown` and deep-equals the committed `projection`; never writes |
| **Consumer (iOS)** | vendored next to `openapi.json` from one memos-ng SHA; a Swift Testing suite renders each case's `markdown` and asserts the same projection |

Never hand-edit the JSON. If the drift gate is red, either the renderer changed on purpose —
run the generator and review the diff — or it changed by accident, which is the point.

The projection is implemented once per platform. The web implementation is
`frontend/src/lib/markdownParity.ts` (`projectRenderedMarkdown(html)`), unit-tested against
literal HTML in `frontend/src/lib/__tests__/markdownParity.spec.ts`. **This file and that
function are one contract: changing either without the other is a defect.**

## Document

```jsonc
{
  "schema": 1,
  "generator": "markdown-it@14.2.0+markdown-it-task-lists@2.1.1+dompurify@3.4.11+renderer-sha256:<16 hex>",
  "cases": [
    { "id": "soft-breaks", "markdown": "line one\nline two", "projection": [ /* blocks */ ] }
  ]
}
```

- `schema` — `1`. A breaking change to the projection bumps it.
- `generator` — the renderer inputs the projections were derived from: the three parsing/
  sanitizing packages at their installed versions, plus a sha256 over `markdown.ts` and
  `markdownParity.ts`. Informational; consumers must not parse it.
- `cases` — sorted by `id`; each carries its own `markdown` **input**, so a consumer never
  needs the SPA to reproduce the expected output.

**Byte determinism.** Cases sorted by id, object keys sorted at every level, two-space indent,
trailing newline, and every non-ASCII character escaped as `\uXXXX` — the last so that no
editor, filesystem or transfer can renormalize the NFD case into NFC and silently rewrite what
the fixture tests. Running the generator twice leaves the tree clean.

## Projection

`projection` is an **ordered array of blocks** — the document flattened to its block sequence.

```ts
type Block = {
  type: 'heading' | 'paragraph' | 'list' | 'task' | 'code' | 'blockquote' | 'table' | 'hr'
  text: string
  links: { href: string, text: string }[]
  inline: { type: 'strong' | 'em' | 'strike' | 'code', text: string }[]
  level?: number        // heading: 1-6.  list/task: nesting depth, 0 = top level
  ordered?: boolean     // list/task: true when the item's list is an <ol>
  checked?: boolean     // task only
  items?: string[]      // list only
  rows?: string[][]     // table only
}
```

`type`, `text`, `links` and `inline` are present on every block; the rest only where noted.
`text` is `""` for `list`, `table` and `hr` — their content lives in `items` / `rows`.

### `text` normalization

Applied to every `text` **except** a `code` block's:

1. `<br>` becomes `\n` (with `breaks: true`, a single source newline is a `<br>`).
2. Every other whitespace run collapses to one space.
3. Whitespace hugging a `\n` is dropped, so a soft break is exactly `\n`.
4. Trimmed at both ends.
5. Normalized to Unicode **NFC**.

Inline code spans are normalized like any other text (`` `x  y` `` → `x y`); only fenced and
indented code blocks are verbatim.

### Blocks

- **heading** — `level` 1-6 from `<h1>`…`<h6>`.
- **paragraph** — one `<p>`. A paragraph the sanitizer emptied still appears, with `text: ""`
  (see *Images* below).
- **list** — a run of consecutive non-task items of one list: `items` holds their texts in
  order, `level` is the nesting depth, `ordered` is `true` for an `<ol>` and `false` for a
  `<ul>`. A task item or a sublist interrupts the run, so one `<ul>` can produce several
  `list` blocks with `task` blocks or deeper lists between them.
- **task** — one GFM task item. `checked` mirrors the rendered checkbox; `level` is the
  nesting depth; `ordered` describes the list the item sits in (`1. [ ] x` renders a checkbox
  in an `<ol>`), so a task keeps its own type and still says which list style holds it. Its
  `text` excludes any sublist hanging off it.
- **code** — one `<pre>`. `text` is the code **verbatim**: no NFC, no whitespace collapsing,
  no trim, and the trailing newline markdown-it emits is kept. `links` and `inline` are empty.
- **blockquote** — the whole quote as one block of flat text (see *Limitations*).
- **table** — `rows` are the cell texts, header row first, each row an array of cells.
- **hr** — `<hr>`; `text` is `""`.

**Ordering.** Blocks are in document order. A sublist's blocks follow the blocks of the item
that holds it, one `level` deeper — so `- a` / `  - a1` / `- b` projects as
`list[a] (0)`, `list[a1] (1)`, `list[b] (0)`, and which item a sublist hangs off stays
readable from the order alone. `ordered` is read from each list element separately, so a
bulleted sublist inside a numbered list projects `ordered: false` at the deeper level.

### `links`

Ordered `{href, text}` for every anchor in the block, in document order. **Only `http:`,
`https:` and `mailto:` hrefs are ever listed** — those are the schemes
`markdown.ts`'s `ALLOWED_URI_REGEXP` permits. Anything else renders as plain text and
contributes to `text` only. There are three layers behind that, and the fixture shows the
first one: markdown-it's own `validateLink` refuses `javascript:`/`data:` before a link is
ever created, so `[js](javascript:alert(1))` stays **literal markdown text** in the output.
DOMPurify (which strips such an `href` while keeping the element and its text) and the
projection's own scheme filter are the second and third; the projection unit tests cover them
with literal HTML.

A table's `links` are those of all its cells in row-major order; a `list` block's are those of
its items in order.

### `inline`

Ordered styled spans inside the block: `strong` (`<strong>`/`<b>`), `em` (`<em>`/`<i>`),
`strike` (`<s>`/`<del>`) and `code` (inline `<code>`). Order is **pre-order**: an outer span
is listed before the spans nested inside it, and a nested span is listed on its own as well,
so `**bold <em>inner</em>**` yields `strong("bold inner")` then `em("inner")`. Span texts are
normalized like block text. A `code` block's `inline` is always empty — the `<code>` inside a
`<pre>` is the block itself, not styled markup.

### Absences that are part of the contract

- **Tag-only lines.** A line consisting only of hashtags (`#curated`, `#curated #trip`) is
  stripped before rendering (`stripTagOnlyLines`) because the tags already show as chips under
  the memo. No block, no text. An inline `#tag` in prose stays, and `# Heading` is a heading —
  the difference is the space after `#`.
- **Raw HTML.** `html: false`, so `<script>alert('x')</script>` and `<b>tag</b>` are **literal
  text** in a paragraph, never markup.
- **Images.** `img` is not in the DOMPurify allowlist, so an image is removed entirely —
  **and its alt text with it**, since a removed void element leaves no children to keep.
  `![A red square](https://example.com/red.png)` therefore projects as a paragraph with
  `text: ""`. memos-ng does not render inline markdown images today (memo media is attachments,
  rendered by the app, not by the markdown pipeline). A consumer that renders the image, or
  that shows its alt text, will not match — matching the SPA means dropping it.

## Limitations of schema 1

Known and accepted; a consumer must not rely on the projection to catch these.

- **Blockquote structure is flattened.** A quote projects as one text block, so a list, a task
  or a code block *inside* a quote loses its structure (the task's `checked` state included).
- **A code block's language is not projected.** `class="language-js"` is dropped.
- **Inline styling is projected as spans, not as ranges.** `inline` says which styled spans
  exist and in what order, not where in `text` they sit.

## Residual: jsdom ≠ Safari

The committed projections are produced under **jsdom**, not in the browser that actually
serves the SPA. The pipeline that matters — markdown-it, the task-lists plugin, DOMPurify's
allowlist — is pure JavaScript and identical in both, and the projection reads only structure
and text (never layout, CSS or measurement). What jsdom cannot guarantee is HTML *parsing*
edge cases where its parser and WebKit's disagree, and any DOMPurify behaviour that depends on
the host DOM implementation. This is accepted: the fixture pins the renderer's contract, not
the browser's. Divergence found on a real device is a finding against this file, and the fix
is a case added here, not a patched projection.
