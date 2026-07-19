import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'

import { useApi } from '@/composables/useApi'

export interface SettingsPatch {
  default_visibility?: 'PRIVATE' | 'PROTECTED' | 'PUBLIC'
  theme?: string
  avatar?: string
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

  return { settings: query.data, isPending: query.isPending, update }
}
