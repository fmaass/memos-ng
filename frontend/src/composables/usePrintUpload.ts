import { type QueryKey, useMutation, useQueryClient } from '@tanstack/vue-query'
import { useToast } from 'primevue/usetoast'

// Attaching a print-quality master to an existing attachment. Two views offer the action (the
// memo feed's card menu and the Resources lightbox); they used to carry a copy each and only the
// Resources copy reported anything, so a rejected upload from the feed (too large, wrong type,
// not the owner, 5xx) looked exactly like a successful one (MEMNG-74). One mutation here, with
// success AND error feedback, is what both call sites use — the only per-view difference is which
// query cache the new master invalidates.

export interface PrintUploadVars {
  uid: string
  file: File
}

// Raw multipart POST: the typed openapi-fetch client is JSON-only. Same-origin, so the BFF
// session cookie rides along. fetch is looked up per call (not captured) so tests can stub it.
export async function uploadPrintMaster({ uid, file }: PrintUploadVars): Promise<void> {
  const fd = new FormData()
  fd.append('file', file)
  const res = await fetch(`/api/attachments/${uid}/print`, {
    method: 'POST',
    body: fd,
    credentials: 'include',
  })
  // Any non-2xx is a failure the user has to see: the rejection carries the status so the
  // mutation's onError can fire (and the message names the code in logs).
  if (!res.ok) throw new Error(`print upload failed (${res.status})`)
}

export const PRINT_UPLOAD_SUCCESS_TOAST = {
  severity: 'success',
  summary: 'Print version attached',
  life: 2500,
} as const

export const PRINT_UPLOAD_ERROR_TOAST = {
  severity: 'error',
  summary: 'Print upload failed',
  life: 4000,
} as const

// `invalidateKey` is the caller's own cache: ['memos'] for the feed, ['attachments'] for
// Resources — each view refreshes the list it actually renders, as before.
export function usePrintUpload(invalidateKey: QueryKey) {
  const toast = useToast()
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: uploadPrintMaster,
    onSuccess: async () => {
      toast.add({ ...PRINT_UPLOAD_SUCCESS_TOAST })
      await queryClient.invalidateQueries({ queryKey: invalidateKey })
    },
    // Without this the feed swallowed every failure — no toast, no cache change, no trace.
    onError: () => {
      toast.add({ ...PRINT_UPLOAD_ERROR_TOAST })
    },
  })
}
