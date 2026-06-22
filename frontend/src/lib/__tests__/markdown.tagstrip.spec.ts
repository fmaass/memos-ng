import { describe, expect, it } from 'vitest'

import { renderMemoMarkdown, stripTagOnlyLines } from '@/lib/markdown'

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
