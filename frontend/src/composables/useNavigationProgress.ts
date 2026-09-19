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

  // The `to` location IS the navigation's identity: vue-router builds it once and hands that same
  // object to every hook below, so a report can be matched to the tap it belongs to.
  const unhook = [
    router.beforeEach((to) => {
      progress.started(to)
    }),
    // A navigation that was cancelled, aborted or duplicated arrives here WITH a failure; one
    // whose component never loaded arrives at `onError` instead and never here at all.
    router.afterEach((to) => progress.ended(to)),
    router.onError((_error, to) => progress.ended(to)),
  ]

  onScopeDispose(() => {
    for (const stop of unhook) stop()
    progress.stop()
  })

  return visible
}
