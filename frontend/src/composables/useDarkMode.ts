import { ref, watch } from 'vue'

// Single shared dark-mode state. PrimeVue's Aura preset is configured with
// darkModeSelector '.app-dark' (see main.ts), so toggling that class on <html>
// switches the whole theme. Defaults to dark; the choice persists across reloads.
const STORAGE_KEY = 'memos-ng:dark'

// window.localStorage (not the bare global, which Node 22 shadows with an unavailable
// experimental one) and guarded — it can be absent or throw in private mode / SSR.
function store(): Storage | null {
  try {
    return typeof window !== 'undefined' ? window.localStorage : null
  } catch {
    return null
  }
}

function readInitial(): boolean {
  const saved = store()?.getItem(STORAGE_KEY)
  if (saved != null) return saved === '1'
  return true // default dark
}

function apply(dark: boolean): void {
  document.documentElement.classList.toggle('app-dark', dark)
}

const isDark = ref(readInitial())
apply(isDark.value)
watch(isDark, (dark) => {
  apply(dark)
  store()?.setItem(STORAGE_KEY, dark ? '1' : '0')
})

export function useDarkMode() {
  return {
    isDark,
    toggle: () => {
      isDark.value = !isDark.value
    },
  }
}
