import type { components } from '@/api/schema'

type Memo = components['schemas']['MemoOut']

/**
 * The uids of every attachment on a memo — the set the editor's "Remove all" action clears in one
 * step so the owner can wipe a memo's photos and re-upload screen+print pairs (which then file into
 * the per-memo folder). Pure so it's unit-testable without the PrimeVue confirm dialog.
 */
export function attachmentUidsToClear(memo: Pick<Memo, 'attachments'>): string[] {
  return (memo.attachments ?? []).map((a) => a.uid)
}
