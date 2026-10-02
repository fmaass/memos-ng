import { useQueryClient } from '@tanstack/vue-query'
import { useToast } from 'primevue/usetoast'
import { ref } from 'vue'

import { useApi } from '@/composables/useApi'

export interface BulkDeleteResult {
  done: string[]
  /** The uid whose delete failed and stopped the batch, or null when every uid was deleted. */
  failed: string | null
  /** What came after the failure and was never sent. */
  unattempted: string[]
}

// One attachment delete, true only when the server answered 2xx. A 404 is a failure: already
// deleted, deleted concurrently and not mine all answer 404, and the client cannot tell them apart.
// The STATUS decides, not openapi-fetch's `error` — a 404 without a body comes back as `error: ''`
// (or `undefined` under `Content-Length: 0`), which is falsy. A request that got no answer fails too.
export async function deleteAttachment(api: ReturnType<typeof useApi>, uid: string): Promise<boolean> {
  try {
    const { response } = await api.DELETE('/api/attachments/{uid}', { params: { path: { uid } } })
    return response.ok
  } catch {
    return false
  }
}

// ONE delete at a time, like the memo card's "Remove all" (MemoFeed): a parallel fan-out would take
// a pooled DB connection per request and exhaust the bounded shared pool. The first failure stops
// the batch.
export async function bulkDelete(
  api: ReturnType<typeof useApi>,
  uids: readonly string[],
): Promise<BulkDeleteResult> {
  const done: string[] = []
  for (const [i, uid] of uids.entries()) {
    if (!(await deleteAttachment(api, uid))) return { done, failed: uid, unattempted: uids.slice(i + 1) }
    done.push(uid)
  }
  return { done, failed: null, unattempted: [] }
}

// Says what happened and nothing more: a failed delete can have removed some of the item's files
// before it failed, so no text claims the failed item is untouched.
export function deleteSummary(result: BulkDeleteResult): string {
  const n = result.done.length
  return result.failed === null ? `Deleted ${n}` : `Deleted ${n}, 1 failed`
}

// The Library's bulk delete. A run while one is in flight is refused (null), so a double tap is one
// batch on the wire. Success or failure, the library (list, tag counts and buckets all live under
// ['attachments']) and the memo feed (['memos'], whose cards show these attachments) are refreshed.
export function useBulkDelete() {
  const api = useApi()
  const queryClient = useQueryClient()
  const toast = useToast()
  const pending = ref(false)

  async function run(uids: readonly string[]): Promise<BulkDeleteResult | null> {
    if (pending.value || uids.length === 0) return null
    pending.value = true
    try {
      const result = await bulkDelete(api, uids)
      toast.add({
        severity: result.failed === null ? 'success' : 'error',
        summary: deleteSummary(result),
        life: 4000,
      })
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['attachments'] }),
        queryClient.invalidateQueries({ queryKey: ['memos'] }),
      ])
      return result
    } finally {
      pending.value = false
    }
  }

  return { run, pending }
}
