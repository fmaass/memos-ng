import {
  type Component,
  inject,
  type InjectionKey,
  markRaw,
  onMounted,
  onUnmounted,
  type ShallowRef,
  shallowRef,
  toValue,
  watch,
  type WatchSource,
} from 'vue'

// The dock lives in the shell, and a routed view is the shell's DESCENDANT (App.vue: AppShell wraps
// RouterView), so provide/inject can only flow shell → view. The shell therefore provides this
// registration, the Library view registers its segment on it, and the dock renders whatever is
// registered while it is in segment mode.
// The override is stronger than the segment: while one is set the dock renders it and NOTHING else —
// no tabs, no Library icon, no segment — which is what the Library's select mode needs for its
// flat toolbar. Same registrant rule as the segment.
export interface DockSegmentRegistration {
  segment: Readonly<ShallowRef<Component | null>>
  register(component: Component): void
  unregister(component: Component): void
  override: Readonly<ShallowRef<Component | null>>
  setOverride(component: Component): void
  clearOverride(component: Component): void
}

export const DOCK_SEGMENT_KEY: InjectionKey<DockSegmentRegistration> = Symbol('dockSegment')

export function createDockSegment(): DockSegmentRegistration {
  const segment: ShallowRef<Component | null> = shallowRef(null)
  const override: ShallowRef<Component | null> = shallowRef(null)
  return {
    segment,
    override,
    setOverride(component) {
      override.value = markRaw(component)
    },
    clearOverride(component) {
      const current: Component | null = override.value
      if (current === component) override.value = null
    },
    register(component) {
      segment.value = markRaw(component)
    },
    // Only the registrant may withdraw: a view that unmounts after its successor mounted must not
    // take the successor's segment with it.
    unregister(component) {
      const current: Component | null = segment.value
      if (current === component) segment.value = null
    },
  }
}

export function useDockSegment(component: Component): void {
  const registration = inject(DOCK_SEGMENT_KEY, null)
  if (!registration) return
  onMounted(() => registration.register(component))
  onUnmounted(() => registration.unregister(component))
}

// Put `component` in place of the whole dock while `active` holds, and take it away when it stops or
// the view unmounts.
export function useDockOverride(component: Component, active: WatchSource<boolean>): void {
  const registration = inject(DOCK_SEGMENT_KEY, null)
  if (!registration) return
  watch(
    () => toValue(active),
    (on) => (on ? registration.setOverride(component) : registration.clearOverride(component)),
    { immediate: true },
  )
  onUnmounted(() => registration.clearOverride(component))
}
