import { useQuery } from '@tanstack/vue-query'

import { useApi } from '@/composables/useApi'

// The current BFF session's user. A 401 is handled centrally by the client's auth
// middleware (redirect to login), so this query simply surfaces the authenticated user
// for the shell (display name, logout). retry:false so a logged-out probe redirects once.
export function useSession() {
  const api = useApi()
  return useQuery({
    queryKey: ['session'],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/auth/user')
      if (error) throw error
      return data
    },
    retry: false,
    staleTime: 5 * 60 * 1000,
  })
}
