import { onScopeDispose, ref, type Ref } from 'vue'
import type { Router } from 'vue-router'

import { createNavigationProgress } from '@/lib/navigationProgress'

/**
 * Whether a navigation is keeping the viewer waiting. The router is the only thing that knows a
 * tap has been taken but not yet answered; `navigationProgress` decides whether that is worth
 * saying, and this is the thin wire between the two.
 */
export function useNavigationProgress(router: Router): Ref<boolean> {
  const visible = ref(false)
  const progress = createNavigationProgress({
    setTimer: (fn, delayMs) => window.setTimeout(fn, delayMs),
    clearTimer: (handle) => window.clearTimeout(handle as number),
    onChange: (next) => {
      visible.value = next
    },
  })

  const unhook = [
    router.beforeEach(() => {
      progress.started()
    }),
    // A navigation that was cancelled, aborted or duplicated arrives here WITH a failure; one
    // whose component never loaded arrives at `onError` instead and never here at all.
    router.afterEach((_to, _from, failure) => progress.ended(failure ? 'unfinished' : 'completed')),
    router.onError(() => progress.ended('unfinished')),
  ]

  onScopeDispose(() => {
    for (const stop of unhook) stop()
    progress.stop()
  })

  return visible
}
