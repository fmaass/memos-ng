// @vitest-environment jsdom
// jsdom, not the project-default happy-dom: DOMPurify supports jsdom, and the committed
// projections were generated under jsdom by scripts/gen-markdown-parity.ts.
import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import { renderMemoMarkdown } from '@/lib/markdown'
import { projectRenderedMarkdown, type ParityBlock } from '@/lib/markdownParity'

/**
 * Drift gate for the cross-platform parity fixture, mirroring the backend's OpenAPI drift test:
 * the committed file is the contract the iOS client is held to, so this test READS it and
 * re-derives it — it never writes. A change in the SPA renderer (markdown-it options, the
 * DOMPurify allowlist, tag stripping) or in the projection turns this red, and the fix is to
 * run `npm run gen:markdown-parity` and review the diff, never to edit the JSON by hand.
 */
interface ParityFixture {
  schema: number
  generator: string
  cases: { id: string; markdown: string; projection: ParityBlock[] }[]
}

// import.meta.dirname, not `new URL(..., import.meta.url)`: Vite rewrites that pattern into an
// http `/@fs/` asset URL at transform time, which readFileSync then refuses.
const FIXTURE_PATH = join(import.meta.dirname, '../../../../fixtures/markdown-parity.json')
const fixture = JSON.parse(readFileSync(FIXTURE_PATH, 'utf8')) as ParityFixture

describe('fixtures/markdown-parity.json', () => {
  it('is a schema 1 document with at least 15 cases', () => {
    expect(fixture.schema).toBe(1)
    expect(fixture.cases.length).toBeGreaterThanOrEqual(15)
    expect(fixture.generator).toEqual(expect.any(String))
    expect(fixture.generator.length).toBeGreaterThan(0)
  })

  it('has unique, non-empty case ids and carries the markdown input of every case', () => {
    const ids = fixture.cases.map((entry) => entry.id)
    expect(new Set(ids).size).toBe(ids.length)
    for (const entry of fixture.cases) {
      expect(entry.id).not.toBe('')
      expect(typeof entry.markdown).toBe('string')
      expect(Array.isArray(entry.projection)).toBe(true)
    }
  })

  it('reads the file the generator writes', () => {
    // The reader and the sole writer must agree on the path, or this gate guards nothing.
    const generatorPath = join(import.meta.dirname, '../../../scripts/gen-markdown-parity.ts')
    const target = /const target = new URL\('([^']+)'/.exec(readFileSync(generatorPath, 'utf8'))
    expect(target).not.toBeNull()
    expect(resolve(dirname(generatorPath), target![1]!)).toBe(resolve(FIXTURE_PATH))
  })

  it.each(fixture.cases.map((entry) => [entry.id, entry] as const))(
    'projection is current for case %s',
    (_id, entry) => {
      expect(
        projectRenderedMarkdown(renderMemoMarkdown(entry.markdown)),
        `case ${entry.id} is stale \u2014 run \`npm run gen:markdown-parity\` and review the diff`,
      ).toEqual(entry.projection)
    },
  )

  it('covers every rule of the markdown rendering contract', () => {
    const blocks = fixture.cases.flatMap((entry) => entry.projection)
    const types = new Set(blocks.map((block) => block.type))
    for (const type of ['heading', 'paragraph', 'list', 'task', 'code', 'blockquote', 'table', 'hr']) {
      expect(types).toContain(type)
    }

    const inlineTypes = new Set(blocks.flatMap((block) => block.inline).map((span) => span.type))
    for (const type of ['strong', 'em', 'strike', 'code']) {
      expect(inlineTypes).toContain(type)
    }

    // At least three cases must exercise inline styling, so a renderer that drops it fails here.
    const styledCases = fixture.cases.filter((entry) =>
      entry.projection.some((block) => block.inline.length > 0),
    )
    expect(styledCases.length).toBeGreaterThanOrEqual(3)

    // Both list styles, from `<ol>` and `<ul>`.
    const listy = blocks.filter((block) => block.type === 'list' || block.type === 'task')
    expect(listy.some((block) => block.ordered === true)).toBe(true)
    expect(listy.some((block) => block.ordered === false)).toBe(true)

    // Both task states, and nesting.
    expect(blocks.some((block) => block.type === 'task' && block.checked === true)).toBe(true)
    expect(blocks.some((block) => block.type === 'task' && block.checked === false)).toBe(true)
    expect(blocks.some((block) => block.type === 'task' && (block.level ?? 0) > 0)).toBe(true)

    // Only http(s)/mailto hrefs are ever projected.
    const hrefs = blocks.flatMap((block) => block.links).map((link) => link.href)
    expect(hrefs.length).toBeGreaterThan(0)
    expect(hrefs.every((href) => /^(?:https?|mailto):/i.test(href))).toBe(true)

    // Soft breaks survive as newlines.
    expect(blocks.some((block) => block.type !== 'code' && block.text.includes('\n'))).toBe(true)
  })

  it('has no tag-only line left in any projected text', () => {
    const tagOnly = /^[ \t]*#\w+(?:[ \t]+#\w+)*[ \t]*$/
    for (const entry of fixture.cases) {
      const lines = entry.projection
        .filter((block) => block.type !== 'code')
        .flatMap((block) => [block.text, ...(block.items ?? [])])
        .flatMap((text) => text.split('\n'))
      for (const line of lines) {
        expect(tagOnly.test(line), `${entry.id}: ${JSON.stringify(line)}`).toBe(false)
      }
    }
    // Positive control: the 0-hit assertion above only means something because a case really
    // does carry hashtag lines in its markdown input.
    expect(fixture.cases.some((entry) => /^#\w+/m.test(entry.markdown))).toBe(true)
  })
})
