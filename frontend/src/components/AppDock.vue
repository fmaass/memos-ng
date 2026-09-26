<script setup lang="ts">
import { computed, inject, onMounted, onUnmounted, shallowRef, watch } from 'vue'
import { useRoute } from 'vue-router'

import { DOCK_SEGMENT_KEY } from '@/composables/useDockSegment'
import { type DockEvent, dockReducer, initialDockState, LIBRARY_ROUTE } from '@/lib/dockState'

// The app's three main destinations, mirroring the native app's tabs (plan decision 11). Nothing
// else lives in the dock — no search button (decision 4). PrimeVue-free markup on purpose.
const TABS = [
  { key: 'library', label: 'Library', icon: 'pi pi-images', to: '/resources' },
  { key: 'timeline', label: 'Timeline', icon: 'pi pi-home', to: '/' },
  { key: 'places', label: 'Places', icon: 'pi pi-map', to: '/places' },
] as const

const route = useRoute()
const registration = inject(DOCK_SEGMENT_KEY, null)

const state = shallowRef(initialDockState())
function dispatch(event: DockEvent) {
  state.value = dockReducer(state.value, event)
}

// The document is the Library's scroll host (the shell's <main> has no overflow of its own).
function onScroll() {
  dispatch({ type: 'scroll', y: document.scrollingElement?.scrollTop ?? 0, route: route.path })
}
onMounted(() => window.addEventListener('scroll', onScroll, { passive: true }))
onUnmounted(() => window.removeEventListener('scroll', onScroll))

watch(
  () => route.path,
  (path) => {
    if (path !== LIBRARY_ROUTE) dispatch({ type: 'leaveLibrary' })
  },
)

// The route check is the reducer's too; repeating it here keeps a registered segment off every
// other route even for the instant between a navigation and the watcher above.
const segment = computed(() =>
  state.value.mode === 'segment' && route.path === LIBRARY_ROUTE
    ? (registration?.segment.value ?? null)
    : null,
)

// The Library's select mode replaces the whole dock with its toolbar; like the segment, only on the
// Library route.
const override = computed(() =>
  route.path === LIBRARY_ROUTE ? (registration?.override.value ?? null) : null,
)

// A tap on the tab that is already active toggles the Library's two states instead of navigating:
// following the link would land on the bare route and drop the filter in its query.
function onTab(event: MouseEvent, active: boolean, navigate: (e?: MouseEvent) => unknown) {
  if (active) {
    event.preventDefault()
    dispatch({ type: 'tapActive', route: route.path })
    return
  }
  void navigate(event)
}
</script>

<template>
  <nav
    class="glass fixed bottom-[calc(env(safe-area-inset-bottom)+12px)] left-1/2 z-40 flex h-[var(--dock-height)] -translate-x-1/2 items-center gap-1 rounded-full p-1"
    aria-label="Main"
    data-test="app-dock"
  >
    <component :is="override" v-if="override" />
    <template v-else-if="segment">
      <button
        type="button"
        class="flex h-full items-center justify-center rounded-full bg-primary/10 px-4 text-primary"
        aria-label="Library"
        data-test="dock-library-toggle"
        @click="dispatch({ type: 'tapActive', route: route.path })"
      >
        <i class="pi pi-images" aria-hidden="true" />
      </button>
      <div class="flex h-full items-center" data-test="dock-segment">
        <component :is="segment" />
      </div>
    </template>
    <template v-else>
      <RouterLink
        v-for="tab in TABS"
        :key="tab.key"
        v-slot="{ href, navigate, isActive }"
        :to="tab.to"
        custom
      >
        <a
          :href="href"
          class="flex h-full min-w-20 flex-col items-center justify-center gap-0.5 rounded-full px-3 text-xs"
          :class="isActive ? 'bg-primary/10 text-primary' : 'text-muted-color'"
          :aria-current="isActive ? 'page' : undefined"
          :data-active="isActive ? 'true' : 'false'"
          :data-test="`dock-tab-${tab.key}`"
          @click="onTab($event, isActive, navigate)"
        >
          <i :class="tab.icon" aria-hidden="true" />
          <span>{{ tab.label }}</span>
        </a>
      </RouterLink>
    </template>
  </nav>
</template>
