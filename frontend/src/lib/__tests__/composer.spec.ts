import { describe, expect, it } from 'vitest'

import {
  canPost,
  composerKeyAction,
  formatMemoDate,
  uploadsUnlinkableMessage,
} from '@/lib/composer'

describe('formatMemoDate', () => {
  it('formats a full, untruncated date + 24h time', () => {
    expect(formatMemoDate(new Date(2026, 5, 21, 14, 30))).toBe('Jun 21, 2026 14:30')
    expect(formatMemoDate(new Date(2026, 0, 3, 9, 5))).toBe('Jan 3, 2026 09:05')
  })
})

describe('canPost', () => {
  it('is true for non-empty text even with no uploads', () => {
    expect(canPost('hello', [])).toBe(true)
  })
  it('is true for a media-only memo when an upload is done (no text)', () => {
    expect(canPost('   ', [{ status: 'done' }])).toBe(true)
  })
  it('is false for empty text with no finished uploads', () => {
    expect(canPost('', [])).toBe(false)
    expect(canPost('  \n ', [{ status: 'uploading' }, { status: 'queued' }])).toBe(false)
    expect(canPost('', [{ status: 'error' }])).toBe(false)
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

describe('uploadsUnlinkableMessage', () => {
  const staged = [
    { uploadUid: 'u-holiday', name: 'holiday.jpg' },
    { uploadUid: 'u-cake', name: 'cake.png' },
  ]
  const refusal = (missing: string[], incomplete: string[] = []) => ({
    detail: {
      code: 'uploads_unlinkable',
      missing_upload_uids: missing,
      incomplete_upload_uids: incomplete,
    },
  })

  it('names the files the queue can still identify', () => {
    const message = uploadsUnlinkableMessage(refusal(['u-holiday']), staged)
    expect(message).toContain('holiday.jpg')
    expect(message).not.toContain('u-holiday') // the uid is server vocabulary, not the user's
    expect(message).toContain('Add the file again')
  })

  it('covers both buckets and pluralises', () => {
    const message = uploadsUnlinkableMessage(refusal(['u-holiday'], ['u-cake']), staged)
    expect(message).toContain('holiday.jpg')
    expect(message).toContain('cake.png')
    expect(message).toContain('Add the files again')
  })

  it('counts instead of naming when the queue cannot name every uid', () => {
    // the edit path posts from the feed, which never saw the tiles
    expect(uploadsUnlinkableMessage(refusal(['u-holiday', 'u-gone']), staged)).toContain(
      '2 attachments',
    )
    expect(uploadsUnlinkableMessage(refusal(['u-gone']))).toContain('1 attachment')
  })

  it('is null for every other failure, so existing handling still applies', () => {
    expect(uploadsUnlinkableMessage(undefined)).toBeNull()
    expect(uploadsUnlinkableMessage(new Error('network down'))).toBeNull()
    expect(uploadsUnlinkableMessage({ detail: 'Memo not found' })).toBeNull()
    // FastAPI's own 422 body — an array of field errors, never this code
    expect(
      uploadsUnlinkableMessage({ detail: [{ loc: ['body', 'content'], msg: 'required' }] }),
    ).toBeNull()
    expect(uploadsUnlinkableMessage(refusal([], []))).toBeNull()
  })
})
