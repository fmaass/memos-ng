import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import { isTagOnlyLine, renderMemoMarkdown, stripTagOnlyLines } from '@/lib/markdown'

/**
 * The literal cases below stay literal on purpose: they are what a broken fixture loader or an
 * empty JSON file would otherwise pass vacuously against. Everything the digest's server-side
 * port is also held to lives in `fixtures/tag-only-lines.json`, and this spec is the side that
 * PRODUCES those expectations — the SPA is the rule, the file records it, the Python port obeys.
 */

describe('stripTagOnlyLines', () => {
  it('drops a line that is only a tag', () => {
    expect(stripTagOnlyLines('#curated')).toBe('')
    expect(stripTagOnlyLines('#curated ')).toBe('')
  })

  it('drops a line of several tags', () => {
    expect(stripTagOnlyLines('#curated #trip')).toBe('')
  })

  it('keeps prose that contains an inline tag', () => {
    expect(stripTagOnlyLines('see #curated here')).toBe('see #curated here')
  })

  it('keeps a real markdown heading (space after #)', () => {
    expect(stripTagOnlyLines('# Heading')).toBe('# Heading')
    expect(stripTagOnlyLines('## Sub')).toBe('## Sub')
  })

  it('removes only the tag line from mixed content', () => {
    expect(stripTagOnlyLines('#curated\nMy trip photos')).toBe('My trip photos')
  })
})

describe('renderMemoMarkdown drops a tag-only title', () => {
  it('renders nothing for a tag-only memo (the chip shows it instead)', () => {
    expect(renderMemoMarkdown('#curated').trim()).toBe('')
  })

  it('still renders the prose around a stripped tag line', () => {
    expect(renderMemoMarkdown('#curated\nHello')).toContain('Hello')
    expect(renderMemoMarkdown('#curated\nHello')).not.toContain('#curated')
  })
})

// --- the shared vector file, which the backend's tests/test_tag_lines.py reads too ---------------

interface TagOnlyFixture {
  schema: number
  rule: string
  cases: { id: string; input: string; stripped: string }[]
  predicate: { id: string; line: string; tag_only: boolean }[]
  render: {
    id: string
    input: string
    html_empty: boolean
    html_contains: string[]
    html_excludes: string[]
  }[]
}

// import.meta.dirname, not `new URL(..., import.meta.url)`: Vite rewrites that pattern into an
// http `/@fs/` asset URL at transform time, which readFileSync then refuses
// (markdown-parity.spec.ts carries the same note).
const FIXTURE_PATH = join(import.meta.dirname, '../../../../fixtures/tag-only-lines.json')
const fixture = JSON.parse(readFileSync(FIXTURE_PATH, 'utf8')) as TagOnlyFixture

describe('fixtures/tag-only-lines.json', () => {
  it('is a schema 1 document carrying at least the cases it has today', () => {
    expect(fixture.schema).toBe(1)
    // 15 strip cases and 2 render cases as of 2026-09-07: the file may grow, never shrink.
    expect(fixture.cases.length).toBeGreaterThanOrEqual(15)
    expect(fixture.predicate.length).toBeGreaterThanOrEqual(11)
    expect(fixture.render.length).toBeGreaterThanOrEqual(2)
  })

  it('has unique, non-empty case ids', () => {
    const ids = [...fixture.cases, ...fixture.predicate, ...fixture.render].map((e) => e.id)
    expect(ids.every((id) => id.length > 0)).toBe(true)
    expect(new Set(ids).size).toBe(ids.length)
  })

  it('records the rule this module actually applies', () => {
    // The regex source, not a description of it: the Python port asserts the same equality, so a
    // change on either side has to travel through this file.
    const rule = new RegExp(fixture.rule)
    for (const line of ['#curated', '#curated #trip', '\t#curated\t']) {
      expect(rule.test(line)).toBe(true)
      expect(isTagOnlyLine(line)).toBe(true)
    }
    for (const line of ['# Heading', 'see #curated here', '#café', '#curated\r']) {
      expect(rule.test(line)).toBe(false)
      expect(isTagOnlyLine(line)).toBe(false)
    }
  })

  it.each(fixture.cases)('strips $id as the file records', ({ input, stripped }) => {
    expect(stripTagOnlyLines(input)).toBe(stripped)
  })

  // The predicate takes WHOLE STRINGS, terminators included: `'#curated\n'` is tag-only under a
  // `$` anchor in Python and never in JavaScript, which is exactly what the port had to match.
  it.each(fixture.predicate)('answers $id as the file records', ({ line, tag_only }) => {
    expect(isTagOnlyLine(line)).toBe(tag_only)
  })

  it('is the side that PRODUCES the trailing-newline vector', () => {
    // Literal, so an emptied `predicate` section cannot make the loop above vanish silently.
    expect(isTagOnlyLine('#curated')).toBe(true)
    expect(isTagOnlyLine('#curated\n')).toBe(false)
  })

  it.each(fixture.render)('renders $id as the file records', (entry) => {
    const html = renderMemoMarkdown(entry.input)
    if (entry.html_empty) {
      expect(html.trim()).toBe('')
    } else {
      expect(html.trim()).not.toBe('')
    }
    for (const needle of entry.html_contains) {
      expect(html).toContain(needle)
    }
    for (const needle of entry.html_excludes) {
      expect(html).not.toContain(needle)
    }
  })
})
