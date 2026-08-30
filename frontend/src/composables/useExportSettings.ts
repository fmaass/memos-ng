// EVERY request the Export tab makes, and nothing else makes them: this file is the one seam that
// knows the endpoint, and every component above it deals in drafts.
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import { type Ref, computed, ref } from 'vue'

import { useApi } from '@/composables/useApi'
import { apiErrorMessage } from '@/lib/digest'
import {
  type ExportGroup,
  type ExportPutBody,
  type ExportSettings,
  type ExportSettingsPut,
  type MailProfileDraft,
  type PrintProfileDraft,
  putBodyFromMailDraft,
  putBodyFromPrintDraft,
} from '@/lib/exportSettings'


export const exportKeys = {
  settings: ['export', 'settings'] as const,
}

export const EXPORT_SETTINGS_PATH = '/api/admin/export/settings'

export function useExportSettings() {
  const api = useApi()
  const queryClient = useQueryClient()

  const settingsQuery = useQuery({
    queryKey: exportKeys.settings,
    queryFn: async () => {
      const { data, error } = await api.GET(EXPORT_SETTINGS_PATH)
      if (error) throw error
      return data
    },
  })

  // PUT replaces the WHOLE row, so both writers below build their body from the server's current
  // settings: the Print bar supplies the Print fields and nothing else, the Mail bar the Mail
  // fields and nothing else. Neither can carry the other's unsaved state along.
  function currentSettings(): ExportSettings {
    const current = settingsQuery.data.value
    if (!current) throw new Error('export settings are not loaded yet')
    return current
  }

  const putSettings = useMutation({
    mutationFn: async (body: ExportSettingsPut) => {
      const { data, error, response } = await api.PUT(EXPORT_SETTINGS_PATH, { body })
      if (error || !data) throw new Error(apiErrorMessage(error, response?.status))
      return data
    },
    onSuccess: (data) => queryClient.setQueryData(exportKeys.settings, data),
  })

  // ONE gate for EVERY writer of the settings row, not one per Save bar. The two profile groups are
  // two paths to the same PUT, and that PUT replaces the WHOLE row: two of them in flight together
  // would each build a body from the row as it stood before the other, and whichever answered second
  // would silently undo the first. A disabled button is not that guard — a double-click, or a second
  // group saved while the first is still on the wire, both pass it.
  //
  // The second writer is REFUSED, not queued: a queued save would re-send a draft the server has
  // already moved past. The refusal says so on the bar the write would have used.
  const settingsWriter = ref<ExportGroup | null>(null)
  const savingPrint = computed(() => settingsWriter.value === 'print')
  const savingMail = computed(() => settingsWriter.value === 'mail')
  const writerBusy = computed(() => settingsWriter.value !== null)
  const printError = ref<string | null>(null)
  const mailError = ref<string | null>(null)
  const notice = ref<string | null>(null)

  const SETTINGS_BUSY = 'Another export profile is still saving — try that again in a moment.'

  /** Run one settings write behind the gate. `build` is called AFTER the gate is taken, so its body
   * is built from the row the previous write's response installed rather than from a stale copy. */
  async function writeSettings(
    kind: ExportGroup,
    build: (current: ExportSettings) => ExportPutBody,
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
      // The body is the WHOLE row, so it carries the other profile's stored values. If those are
      // outside the published bands the request is refused HERE, on the bar that was pressed, and
      // nothing goes out — a 422 naming a field the admin did not touch explains nothing.
      const assembled = build(currentSettings())
      if (!assembled.ok) {
        errorRef.value = assembled.message
        return false
      }
      await putSettings.mutateAsync(assembled.body)
      notice.value = confirmation
      return true
    } catch (error) {
      errorRef.value = error instanceof Error ? error.message : apiErrorMessage(error)
      return false
    } finally {
      settingsWriter.value = null
    }
  }

  function savePrint(draft: PrintProfileDraft): Promise<boolean> {
    return writeSettings(
      'print',
      (current) => putBodyFromPrintDraft(draft, current),
      'The print profile is saved.',
      printError,
    )
  }

  function saveMail(draft: MailProfileDraft): Promise<boolean> {
    return writeSettings(
      'mail',
      (current) => putBodyFromMailDraft(draft, current),
      'The mail profile is saved.',
      mailError,
    )
  }

  function clearNotice() {
    notice.value = null
  }

  return {
    settings: settingsQuery.data,
    isPending: settingsQuery.isPending,
    settingsError: settingsQuery.error,
    savingPrint,
    savingMail,
    writerBusy,
    printError,
    mailError,
    notice,
    clearNotice,
    savePrint,
    saveMail,
  }
}
