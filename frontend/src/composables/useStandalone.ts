import { ref } from 'vue'

// True when the app is running as an installed PWA (standalone), so the UI can gate
// iOS-only affordances (e.g. "Add to Home Screen" hints) and safe-area treatment.
function detect(): boolean {
  if (typeof window === 'undefined') return false
  const displayMode = window.matchMedia?.('(display-mode: standalone)').matches ?? false
  // iOS Safari predates display-mode and exposes navigator.standalone instead.
  const iosStandalone =
    (window.navigator as Navigator & { standalone?: boolean }).standalone === true
  return displayMode || iosStandalone
}

export function useStandalone() {
  return { isStandalone: ref(detect()) }
}
