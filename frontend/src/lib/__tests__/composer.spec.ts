import { describe, expect, it } from 'vitest'

import { composerKeyAction, formatMemoDate } from '@/lib/composer'

describe('formatMemoDate', () => {
  it('formats a full, untruncated date + 24h time', () => {
    expect(formatMemoDate(new Date(2026, 5, 21, 14, 30))).toBe('Jun 21, 2026 14:30')
    expect(formatMemoDate(new Date(2026, 0, 3, 9, 5))).toBe('Jan 3, 2026 09:05')
  })
})

describe('composerKeyAction', () => {
  it('maps ⌘/Ctrl+Enter to save', () => {
    expect(composerKeyAction({ key: 'Enter', metaKey: true, ctrlKey: false })).toBe('save')
    expect(composerKeyAction({ key: 'Enter', metaKey: false, ctrlKey: true })).toBe('save')
  })
  it('maps ⌘/Ctrl+B/I/K to markdown actions', () => {
    expect(composerKeyAction({ key: 'b', metaKey: true, ctrlKey: false })).toBe('bold')
    expect(composerKeyAction({ key: 'I', metaKey: true, ctrlKey: false })).toBe('italic')
    expect(composerKeyAction({ key: 'k', metaKey: false, ctrlKey: true })).toBe('link')
  })
  it('ignores plain keys and unmapped modified keys', () => {
    expect(composerKeyAction({ key: 'Enter', metaKey: false, ctrlKey: false })).toBeNull()
    expect(composerKeyAction({ key: 'b', metaKey: false, ctrlKey: false })).toBeNull()
    expect(composerKeyAction({ key: 'x', metaKey: true, ctrlKey: false })).toBeNull()
  })
})
