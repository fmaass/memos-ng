import { describe, expect, it } from 'vitest'

import { applyMarkdown } from '@/lib/markdownToolbar'

describe('applyMarkdown', () => {
  it('wraps the selection in bold/italic/code markers', () => {
    expect(applyMarkdown('hello world', 6, 11, 'bold').text).toBe('hello **world**')
    expect(applyMarkdown('hello world', 6, 11, 'italic').text).toBe('hello _world_')
    expect(applyMarkdown('hello world', 6, 11, 'code').text).toBe('hello `world`')
  })

  it('keeps the selection over the wrapped word', () => {
    const r = applyMarkdown('hi there', 3, 8, 'bold')
    expect(r.text.slice(r.selectionStart, r.selectionEnd)).toBe('there')
  })

  it('builds a link with the selection as the link text', () => {
    const r = applyMarkdown('see docs', 4, 8, 'link')
    expect(r.text).toBe('see [docs](url)')
    expect(r.text.slice(r.selectionStart, r.selectionEnd)).toBe('docs')
  })

  it('prefixes the current line with a task checkbox', () => {
    const r = applyMarkdown('buy milk', 0, 0, 'checkbox')
    expect(r.text).toBe('- [ ] buy milk')
  })

  it('checkboxes only the line the cursor is on', () => {
    const text = 'first\nsecond'
    const r = applyMarkdown(text, 6, 6, 'checkbox') // cursor at start of "second"
    expect(r.text).toBe('first\n- [ ] second')
  })
})
