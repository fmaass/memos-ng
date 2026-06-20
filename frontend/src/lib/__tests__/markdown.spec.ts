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
})
