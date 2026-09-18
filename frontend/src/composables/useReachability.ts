import { computed, getCurrentInstance, onUnmounted, ref } from 'vue'

import { reachability } from '@/api/reachability'
import type { ReachabilityStatus } from '@/lib/reachability'

/** The reachability verdict as reactive state, for the one component that renders it. */
export function useReachability() {
  const status = ref<ReachabilityStatus>(reachability.status())
  const stop = reachability.subscribe((next) => {
    status.value = next
  })
  if (getCurrentInstance()) onUnmounted(stop)

  return {
    status,
    // Only the debounced verdict reaches the screen: `suspect` is a hunch, and a hunch is not
    // worth a banner.
    unreachable: computed(() => status.value === 'unreachable'),
    retryNow: () => reachability.retryNow(),
  }
}
