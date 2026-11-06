import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import { type Ref, computed, ref } from 'vue'

import { useApi } from '@/composables/useApi'
import {
  type DigestConfigDraft,
  type DigestLanguage,
  apiErrorMessage,
  draftFromSettings,
  putBodyFromDraft,
} from '@/lib/digest'

// One place for the Mail panel's server state, in the shape of `useSettings.ts`: queries for the
// four Phase-1 reads, and one single-purpose mutation per thing the panel can change. The three
// ACTION endpoints (preview, test-send, run) are deliberately NOT here — they live in
// `useDigestActions.ts` because they do not exist on the API yet.
export const digestKeys = {
  settings: ['digest', 'settings'] as const,
  recipients: ['digest', 'recipients'] as const,
  runs: ['digest', 'runs'] as const,
  hosting: ['digest', 'hosting'] as const,
}

/** The panel's Runs table is "the last 20", and the API's own default happens to agree — it is
 * passed explicitly all the same, so a change to that default cannot silently change the table. */
export const RUNS_LIMIT = 20

export interface NewRecipient {
  email: string
  display_name: string | null
  language: DigestLanguage
}

export function useDigest() {
  const api = useApi()
  const queryClient = useQueryClient()

  const settingsQuery = useQuery({
    queryKey: digestKeys.settings,
    queryFn: async () => {
      const { data, error } = await api.GET('/api/admin/digest/settings')
      if (error) throw error
      return data
    },
  })

  const recipientsQuery = useQuery({
    queryKey: digestKeys.recipients,
    queryFn: async () => {
      const { data, error } = await api.GET('/api/admin/digest/recipients')
      if (error) throw error
      return data
    },
  })

  // The runs list is the repo's cursor-paged shape (`{items, nextCursor, totalCount}`, like
  // `MemoList`). The panel shows one page and nothing else, so it asks for `limit` alone — no
  // `cursor`, and no `with_total`, which the API only computes when a caller asks for it.
  const runsQuery = useQuery({
    queryKey: digestKeys.runs,
    queryFn: async () => {
      const { data, error } = await api.GET('/api/admin/digest/runs', {
        params: { query: { limit: RUNS_LIMIT } },
      })
      if (error) throw error
      return data
    },
  })
  // `undefined` until the page arrives, deliberately: an empty array would say "no runs" and make
  // the status row promise a send today that a run today may already have taken.
  const runs = computed(() => runsQuery.data.value?.items)

  const hostingQuery = useQuery({
    queryKey: digestKeys.hosting,
    queryFn: async () => {
      const { data, error } = await api.GET('/api/admin/digest/hosting')
      if (error) throw error
      return data
    },
  })

  // PUT replaces the WHOLE row, so both writers below build their body from the server's current
  // settings — the grouped draft supplies the grouped fields and nothing else, and the switch
  // supplies `enabled` and nothing else. Neither can carry the other's unsaved state along.
  function currentSettings() {
    const current = settingsQuery.data.value
    if (!current) throw new Error('digest settings are not loaded yet')
    return current
  }

  const putSettings = useMutation({
    mutationFn: async (body: ReturnType<typeof putBodyFromDraft>) => {
      const { data, error, response } = await api.PUT('/api/admin/digest/settings', { body })
      if (error) throw new Error(apiErrorMessage(error, response?.status))
      return data
    },
    onSuccess: (data) => queryClient.setQueryData(digestKeys.settings, data),
  })

  const createRecipient = useMutation({
    mutationFn: async (body: NewRecipient) => {
      const { data, error, response } = await api.POST('/api/admin/digest/recipients', {
        body: {
          email: body.email,
          display_name: body.display_name,
          enabled: true,
          language: body.language,
        },
      })
      if (error) throw new Error(apiErrorMessage(error, response?.status))
      return data
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: digestKeys.recipients }),
  })

  const patchRecipient = useMutation({
    mutationFn: async (vars: { id: number; enabled?: boolean; language?: DigestLanguage }) => {
      const { id, ...body } = vars
      const { data, error, response } = await api.PATCH(
        '/api/admin/digest/recipients/{recipient_id}',
        { params: { path: { recipient_id: id } }, body },
      )
      if (error) throw new Error(apiErrorMessage(error, response?.status))
      return data
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: digestKeys.recipients }),
  })

  const deleteRecipient = useMutation({
    mutationFn: async (id: number) => {
      const { error, response } = await api.DELETE('/api/admin/digest/recipients/{recipient_id}', {
        params: { path: { recipient_id: id } },
      })
      if (error) throw new Error(apiErrorMessage(error, response?.status))
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: digestKeys.recipients }),
  })

  // ONE gate for EVERY writer of the settings row, not one per button. Saving the grouped config
  // and flipping the enabled switch are two paths to the same PUT, and that PUT replaces the WHOLE
  // row: two of them in flight together would each build a body from the row as it stood before the
  // other, and whichever answered second would silently undo the first. A disabled button is not
  // that guard — a double-click, a rapid toggle, or a toggle fired while a save is still on the
  // wire all pass it.
  //
  // The second writer is REFUSED, not queued: a queued toggle would land the state the admin just
  // left, and a queued save would re-send a draft the server has already moved past. The refusal
  // says so on the same notice the write would have used.
  const settingsWriter = ref<'config' | 'enabled' | null>(null)
  const savingConfig = computed(() => settingsWriter.value === 'config')
  const togglingEnabled = computed(() => settingsWriter.value !== null)
  const recipientBusy = ref<number | 'add' | null>(null)
  const configError = ref<string | null>(null)
  const enabledError = ref<string | null>(null)
  const recipientError = ref<string | null>(null)
  const notice = ref<string | null>(null)

  const SETTINGS_BUSY = 'Another settings change is still saving — try that again in a moment.'

  /** Run one settings write behind the gate. `build` is called AFTER the gate is taken, so its body
   * is built from the row the previous write's response installed rather than from a stale copy. */
  async function writeSettings(
    kind: 'config' | 'enabled',
    build: (current: ReturnType<typeof currentSettings>) => ReturnType<typeof putBodyFromDraft>,
    confirmation: string,
    errorRef: Ref<string | null>,
  ): Promise<boolean> {
    if (settingsWriter.value !== null) {
      errorRef.value = SETTINGS_BUSY
      return false
    }
    settingsWriter.value = kind
    errorRef.value = null
    try {
      await putSettings.mutateAsync(build(currentSettings()))
      notice.value = confirmation
      return true
    } catch (error) {
      errorRef.value = error instanceof Error ? error.message : apiErrorMessage(error)
      return false
    } finally {
      settingsWriter.value = null
    }
  }

  function saveConfig(draft: DigestConfigDraft): Promise<boolean> {
    return writeSettings(
      'config',
      (current) => putBodyFromDraft(draft, current.enabled),
      'Saved.',
      configError,
    )
  }

  function setEnabled(next: boolean): Promise<boolean> {
    return writeSettings(
      'enabled',
      (current) => putBodyFromDraft(draftFromSettings(current), next),
      next ? 'The digest is on.' : 'The digest is off.',
      enabledError,
    )
  }

  async function runRecipientChange(
    key: number | 'add',
    work: () => Promise<unknown>,
    confirmation: string,
  ): Promise<boolean> {
    if (recipientBusy.value !== null) return false
    recipientBusy.value = key
    recipientError.value = null
    try {
      await work()
      notice.value = confirmation
      return true
    } catch (error) {
      recipientError.value = error instanceof Error ? error.message : apiErrorMessage(error)
      return false
    } finally {
      recipientBusy.value = null
    }
  }

  function addRecipient(recipient: NewRecipient): Promise<boolean> {
    return runRecipientChange(
      'add',
      () => createRecipient.mutateAsync(recipient),
      `${recipient.email} added.`,
    )
  }

  function setRecipientEnabled(id: number, enabled: boolean): Promise<boolean> {
    return runRecipientChange(
      id,
      () => patchRecipient.mutateAsync({ id, enabled }),
      enabled ? 'Recipient enabled.' : 'Recipient disabled.',
    )
  }

  function setRecipientLanguage(id: number, language: DigestLanguage): Promise<boolean> {
    return runRecipientChange(
      id,
      () => patchRecipient.mutateAsync({ id, language }),
      'Recipient language changed.',
    )
  }

  function removeRecipient(id: number): Promise<boolean> {
    return runRecipientChange(id, () => deleteRecipient.mutateAsync(id), 'Recipient removed.')
  }

  function refreshRuns() {
    return queryClient.invalidateQueries({ queryKey: digestKeys.runs })
  }

  function clearNotice() {
    notice.value = null
  }

  return {
    settings: settingsQuery.data,
    recipients: recipientsQuery.data,
    runs,
    hosting: hostingQuery.data,
    isPending: settingsQuery.isPending,
    settingsError: settingsQuery.error,
    savingConfig,
    togglingEnabled,
    recipientBusy,
    configError,
    enabledError,
    recipientError,
    notice,
    clearNotice,
    saveConfig,
    setEnabled,
    addRecipient,
    setRecipientEnabled,
    setRecipientLanguage,
    removeRecipient,
    refreshRuns,
  }
}
