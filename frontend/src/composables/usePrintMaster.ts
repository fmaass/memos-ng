import { type QueryKey, useMutation, useQueryClient } from '@tanstack/vue-query'
import { useToast } from 'primevue/usetoast'
import { reactive } from 'vue'

import type { components } from '@/api/schema'
import { uploadFile } from '@/composables/useResumableUpload'
import type { PrintUploadState } from '@/lib/printMaster'

// Attaching, replacing and removing PRINT MASTERS (MEMNG-75).
//
// The bytes go through the same resumable/chunked upload every other file in this app uses
// (`uploadFile`), and the finished upload is then bound to the photo as an `attachment_renditions`
// row. The old path POSTed one multipart blob to `/print`, which wrote the legacy print_path column
// — a column that is NULL for every master in production, so the button reported success while
// storing the file where nothing reads it. Chunked also means a 300 MB TIFF survives a dropped
// connection and can report progress, which is what the per-tile chip renders.

type RenditionDetail = components['schemas']['RenditionDetail']

export interface PrintMasterVars {
  uid: string
  file: File
  variantKey: string // replacing keeps the master's own key; a first attach passes `print`
}

export interface PrintMasterRemoveVars {
  uid: string
  variantKey: string
}

// Keyed by ATTACHMENT uid, module-level on purpose: the tile chip that renders the progress lives
// in MemoCard, the mutation that drives it lives in the view above it, and a feed refetch replaces
// every memo object in between. One store outside the component tree is what keeps a running upload
// visible across all of that.
const uploads = reactive(new Map<string, PrintUploadState>())
// The File is NOT in the reactive store: Retry needs it, nothing renders it, and a reactive proxy
// around a File is a footgun (structured-clone / slice on a proxy).
const retryFiles = new Map<string, { file: File; variantKey: string }>()

/** The live per-photo upload states the memo-card chips read (see `printChipState`). */
export function printMasterUploads(): Map<string, PrintUploadState> {
  return uploads
}

/**
 * Push a print master through the resumable upload and bind it to the photo. Replaces the master
 * that key already holds, if any — the server does the swap in one transaction.
 */
export async function attachPrintMaster(
  { uid, file, variantKey }: PrintMasterVars,
  onProgress?: (pct: number) => void,
): Promise<void> {
  const uploadUid = await uploadFile(file, onProgress)
  const res = await fetch(`/api/attachments/${uid}/renditions`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ upload_uid: uploadUid, variant_key: variantKey }),
    credentials: 'include',
  })
  // Any non-2xx is a failure the user has to see — the rejection carries the status (MEMNG-74).
  if (!res.ok) throw new Error(`print master attach failed (${res.status})`)
}

export async function removePrintMaster({ uid, variantKey }: PrintMasterRemoveVars): Promise<void> {
  const res = await fetch(`/api/attachments/${uid}/rendition/${encodeURIComponent(variantKey)}`, {
    method: 'DELETE',
    credentials: 'include',
  })
  if (!res.ok) throw new Error(`print master remove failed (${res.status})`)
}

/** The photo's masters WITH their measured dimensions — what the viewer's print row states. */
export async function fetchPrintMasters(uid: string): Promise<RenditionDetail[]> {
  const res = await fetch(`/api/attachments/${uid}/renditions`, { credentials: 'include' })
  if (!res.ok) throw new Error(`print masters unavailable (${res.status})`)
  return (await res.json()) as RenditionDetail[]
}

export const PRINT_MASTER_ATTACHED_TOAST = {
  severity: 'success',
  summary: 'Print master attached',
  life: 2500,
} as const

export const PRINT_MASTER_ATTACH_ERROR_TOAST = {
  severity: 'error',
  summary: 'Print master upload failed',
  life: 4000,
} as const

export const PRINT_MASTER_REMOVED_TOAST = {
  severity: 'success',
  summary: 'Print master removed',
  life: 2500,
} as const

export const PRINT_MASTER_REMOVE_ERROR_TOAST = {
  severity: 'error',
  summary: 'Removing the print master failed',
  life: 4000,
} as const

/**
 * `invalidateKey` is the caller's own cache: ['memos'] for the feed, ['attachments'] for Resources
 * — each view refreshes the list it actually renders.
 */
export function usePrintMaster(invalidateKey: QueryKey) {
  const toast = useToast()
  const queryClient = useQueryClient()

  const attachMutation = useMutation({
    mutationFn: (vars: PrintMasterVars) =>
      attachPrintMaster(vars, (pct) => {
        const state = uploads.get(vars.uid)
        if (state) state.progress = pct
      }),
    onMutate: (vars) => {
      uploads.set(vars.uid, { status: 'uploading', progress: 0 })
      retryFiles.set(vars.uid, { file: vars.file, variantKey: vars.variantKey })
    },
    onSuccess: async (_data, vars) => {
      uploads.delete(vars.uid)
      retryFiles.delete(vars.uid)
      toast.add({ ...PRINT_MASTER_ATTACHED_TOAST })
      await queryClient.invalidateQueries({ queryKey: invalidateKey })
    },
    // Without this the feed swallowed every failure — no toast, no chip, no trace (MEMNG-74). The
    // state stays behind as `error` so the tile can offer Retry instead of just forgetting.
    onError: (_err, vars) => {
      uploads.set(vars.uid, { status: 'error', progress: uploads.get(vars.uid)?.progress ?? 0 })
      toast.add({ ...PRINT_MASTER_ATTACH_ERROR_TOAST })
    },
  })

  const removeMutation = useMutation({
    mutationFn: removePrintMaster,
    onSuccess: async () => {
      toast.add({ ...PRINT_MASTER_REMOVED_TOAST })
      await queryClient.invalidateQueries({ queryKey: invalidateKey })
    },
    onError: () => {
      toast.add({ ...PRINT_MASTER_REMOVE_ERROR_TOAST })
    },
  })

  /** Re-run the failed upload of a photo with the very file that failed (the tile's RETRY chip). */
  function retry(uid: string): void {
    const pending = retryFiles.get(uid)
    if (!pending) return
    attachMutation.mutate({ uid, file: pending.file, variantKey: pending.variantKey })
  }

  return {
    attach: (vars: PrintMasterVars) => attachMutation.mutate(vars),
    remove: (vars: PrintMasterRemoveVars) => removeMutation.mutate(vars),
    retry,
    uploads,
  }
}
