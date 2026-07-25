import { onBeforeUnmount, onMounted, ref, watch, type Ref } from 'vue'

/**
 * Track an element's rendered width (ResizeObserver, with the mount-time `clientWidth` as the first
 * value). Layouts that must know their container in pixels — justified rows — read this; keeping it
 * in one place means the gallery and the memo feed measure identically.
 * Degrades to the mount-time width where ResizeObserver is absent.
 */
export function useElementWidth(el: Ref<HTMLElement | null>): Ref<number> {
  const width = ref(0)
  let ro: ResizeObserver | null = null

  function attach(node: HTMLElement | null) {
    ro?.disconnect()
    ro = null
    if (!node) {
      width.value = 0
      return
    }
    width.value = node.clientWidth
    if (typeof ResizeObserver === 'undefined') return
    ro = new ResizeObserver((entries) => {
      width.value = entries[0].contentRect.width
    })
    ro.observe(node)
  }

  onMounted(() => attach(el.value))
  // The element can appear/change after mount (a memo gaining attachments swaps the media area).
  watch(el, attach)
  onBeforeUnmount(() => {
    ro?.disconnect()
    ro = null
  })

  return width
}
