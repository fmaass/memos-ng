import {
  type Component,
  inject,
  type InjectionKey,
  markRaw,
  onMounted,
  onUnmounted,
  type ShallowRef,
  shallowRef,
} from 'vue'

// The dock lives in the shell, and a routed view is the shell's DESCENDANT (App.vue: AppShell wraps
// RouterView), so provide/inject can only flow shell → view. The shell therefore provides this
// registration, the Library view registers its segment on it, and the dock renders whatever is
// registered while it is in segment mode.
export interface DockSegmentRegistration {
  segment: Readonly<ShallowRef<Component | null>>
  register(component: Component): void
  unregister(component: Component): void
}

export const DOCK_SEGMENT_KEY: InjectionKey<DockSegmentRegistration> = Symbol('dockSegment')

export function createDockSegment(): DockSegmentRegistration {
  const segment: ShallowRef<Component | null> = shallowRef(null)
  return {
    segment,
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
