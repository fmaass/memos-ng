import { useQuery } from '@tanstack/vue-query'

import { useApi } from '@/composables/useApi'

// The user's tags with memo counts, for the sidebar tag list.
export function useTags() {
  const api = useApi()
  return useQuery({
    queryKey: ['tags'],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/tags')
      if (error) throw error
      return data
    },
  })
}
