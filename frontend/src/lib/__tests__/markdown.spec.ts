// @vitest-environment jsdom
// jsdom, not the project-default happy-dom: DOMPurify only sanitizes when a real `window` was
// present at import time, and sets `isSupported = false` (returning its input UNCHANGED) when it
// was not. Under happy-dom these tests would grade markdown-it's raw output and never see the
// sanitizer at all — the same reason scripts/gen-markdown-parity.ts uses jsdom.
import { describe, expect, it } from 'vitest'

import { renderMemoMarkdown } from '@/lib/markdown'

describe('renderMemoMarkdown', () => {
  it('renders ordinary markdown', () => {
    const html = renderMemoMarkdown('**bold** and _italic_ and `code`')
    expect(html).toContain('<strong>bold</strong>')
    expect(html).toContain('<em>italic</em>')
    expect(html).toContain('<code>code</code>')
  })

  it('renders lists and links', () => {
    const html = renderMemoMarkdown('- one\n- two\n\n[docs](https://example.com)')
    expect(html).toContain('<li>one</li>')
    expect(html).toContain('href="https://example.com"')
  })

  it('renders task lines as checkboxes (checked state preserved)', () => {
    const html = renderMemoMarkdown('- [ ] todo\n- [x] done')
    expect(html).toContain('type="checkbox"')
    expect((html.match(/type="checkbox"/g) ?? []).length).toBe(2)
    expect(html).toContain('checked') // the [x] item
  })

  // --- the load-bearing security cases: dangerous source must be inert (escaped), never live ---

  it('renders an inline <script> inert (escaped, not a live tag)', () => {
    const html = renderMemoMarkdown('hi <script>alert(1)</script> there')
    expect(html).not.toContain('<script') // no live script element
    expect(html).toContain('&lt;script&gt;') // escaped to visible text
  })

  it('escapes a raw <img> so no onerror handler is live', () => {
    const html = renderMemoMarkdown('<img src=x onerror=alert(1)>')
    expect(html).not.toContain('<img') // no live img element to fire onerror
    expect(html).toContain('&lt;img') // escaped instead
  })

  it('does not emit a javascript: link href', () => {
    const html = renderMemoMarkdown('[click](javascript:alert(1))')
    expect(html.toLowerCase()).not.toContain('href="javascript:') // protocol rejected
  })

  it('escapes a raw anchor so its onclick is not live', () => {
    const html = renderMemoMarkdown('<a href="#" onclick="steal()">x</a>')
    expect(html).not.toContain('<a ') // no live anchor element
    expect(html).toContain('&lt;a') // escaped to inert text
  })
  // --- the canonical cross-client rendering decisions (MEMNG-144, owner 2026-08-26) ---
  // The iOS client is held to the same four behaviours through fixtures/markdown-parity.json.

  it('(1) draws a checkbox on the task items of a mixed list and on nothing else', () => {
    const html = renderMemoMarkdown('- plain one\n- [ ] the only task\n- plain two')
    expect((html.match(/type="checkbox"/g) ?? []).length).toBe(1)
    expect(html).toContain('<li>plain one</li>')
    expect(html).toContain('<li>plain two</li>')
  })

  it('(2) auto-links a bare domain that carries no scheme', () => {
    const html = renderMemoMarkdown('Visit example.com for the docs.')
    expect(html).toContain('<a href="http://example.com">example.com</a>')
  })

  it('(3) drops an image with an unsafe scheme and shows its alt as plain text', () => {
    for (const source of ['![A red square](javascript:alert(1))', '![A red square](data:text/html,hi)']) {
      const html = renderMemoMarkdown(source)
      expect(html).toContain('A red square') // the alt survives as text
      expect(html).not.toContain('![') // and no literal markdown is left on screen
      expect(html).not.toContain('<img')
      expect(html.toLowerCase()).not.toContain('javascript:')
      expect(html.toLowerCase()).not.toContain('data:')
    }
  })

  it('(3) still drops an image with an allowed scheme entirely, alt included', () => {
    // Unchanged behaviour, pinned so the rule above cannot grow into "every image shows its alt":
    // memo media are attachments, not markdown images.
    const html = renderMemoMarkdown('![A red square](https://example.com/red.png)')
    expect(html).not.toContain('A red square')
    expect(html).not.toContain('<img')
  })

  it('(4) renders an ftp: or scheme-relative link as plain text, with no anchor at all', () => {
    const html = renderMemoMarkdown(
      '[the file](ftp://example.com/pub/readme.txt) and [the host](//example.com/path).',
    )
    expect(html).toContain('the file')
    expect(html).toContain('the host')
    expect(html).not.toContain('<a') // not even an href-less anchor
    expect(html).not.toContain('ftp:')
    expect(html).not.toContain('//example.com')
  })

  it('leaves a link with an unsafe scheme as literal markdown', () => {
    // The image rule relaxes markdown-it's link validation to reach the alt text; this pins that
    // it did not relax it for LINKS, whose literal-markdown outcome the parity fixture depends on.
    const html = renderMemoMarkdown('[click](javascript:alert(1)) stays as written')
    expect(html).toContain('[click](javascript:alert(1))')
  })
})
