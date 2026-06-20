import { useQuery } from '@tanstack/vue-query'

import { useApi } from '@/composables/useApi'

// Per-day memo counts (YYYY-MM-DD → count) for the sidebar activity heatmap.
export function useHeatmap() {
  const api = useApi()
  return useQuery({
    queryKey: ['heatmap'],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/stats/heatmap')
      if (error) throw error
      return data
    },
  })
}
