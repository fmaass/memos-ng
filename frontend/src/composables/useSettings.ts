import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'

import { useApi } from '@/composables/useApi'

export interface SettingsPatch {
  default_visibility?: 'PRIVATE' | 'PROTECTED' | 'PUBLIC'
  theme?: string
  avatar?: string
  frame_send_to?: string
  frame_send_from?: string
}

export interface FrameTestResult {
  ok: boolean
  detail: string
}

// Pure mapping of the test-send result to a UI banner. Extracted so it can be unit-tested without
// happy-dom. Deliberately relays the server's wording (which says "check your frame", never claims
// delivery) rather than inventing a success message here.
export function frameTestMessage(r: FrameTestResult): { severity: 'success' | 'error'; text: string } {
  return { severity: r.ok ? 'success' : 'error', text: r.detail }
}

// The current user's server-persisted settings (default memo visibility + theme).
export function useSettings() {
  const api = useApi()
  const queryClient = useQueryClient()

  const query = useQuery({
    queryKey: ['settings'],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/settings')
      if (error) throw error
      return data
    },
  })

  const update = useMutation({
    mutationFn: async (patch: SettingsPatch) => {
      const { data, error } = await api.PATCH('/api/settings', { body: patch })
      if (error) throw error
      return data
    },
    onSuccess: (data) => queryClient.setQueryData(['settings'], data),
  })

  // Send a test photo to the frame through the real render+SMTP path. Returns the SMTP outcome;
  // mapping to a UI banner is done by the pure frameTestMessage above.
  const frameTest = useMutation({
    mutationFn: async (): Promise<FrameTestResult> => {
      const { data, error } = await api.POST('/api/settings/frame-test', {})
      if (error) throw error
      return data as FrameTestResult
    },
  })

  return { settings: query.data, isPending: query.isPending, update, frameTest }
}
