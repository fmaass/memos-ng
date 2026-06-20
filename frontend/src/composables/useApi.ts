import { api } from '@/api/client'

// Thin accessor so views never import the client directly — the seam where auth
// handling / interceptors will attach in later phases.
export function useApi() {
  return api
}
