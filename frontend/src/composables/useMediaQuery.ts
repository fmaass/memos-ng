import { onBeforeUnmount, type Ref, ref } from 'vue'

// Whether a CSS media query matches, kept live. Read synchronously at setup so the first render
// already takes the right branch (a phone must not flash the desktop layout); false where the
// environment has no matchMedia.
export function useMediaQuery(query: string): Readonly<Ref<boolean>> {
  const matches = ref(false)
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return matches
  const media = window.matchMedia(query)
  const sync = () => {
    matches.value = media.matches
  }
  sync()
  media.addEventListener('change', sync)
  onBeforeUnmount(() => media.removeEventListener('change', sync))
  return matches
}
