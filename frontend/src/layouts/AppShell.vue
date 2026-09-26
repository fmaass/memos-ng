<script setup lang="ts">
import Button from 'primevue/button'
import Drawer from 'primevue/drawer'
import { computed, onBeforeUnmount, onMounted, provide, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AppDock from '@/components/AppDock.vue'
import CalendarHeatmap from '@/components/CalendarHeatmap.vue'
import ConnectionBanner from '@/components/ConnectionBanner.vue'
import NavigationProgressBar from '@/components/NavigationProgressBar.vue'
import TagList from '@/components/TagList.vue'
import TimelineHeader from '@/components/TimelineHeader.vue'
import { createDockSegment, DOCK_SEGMENT_KEY } from '@/composables/useDockSegment'
import { TIMELINE_COMPOSER_KEY } from '@/composables/useTimelineComposer'
import { useDarkMode } from '@/composables/useDarkMode'
import { useSession } from '@/composables/useSession'
import { useNavigationProgress } from '@/composables/useNavigationProgress'
import { useSettings } from '@/composables/useSettings'

// The secondary destinations, one list for the sidebar and the drawer. Library, Timeline and Places
// are the dock's tabs (AppDock.vue); Search is here because no header carries a search button.
interface NavItem {
  label: string
  icon: string
  to: string
  /** Overrides the `nav-<label>` test id. */
  testId?: string
}
const NAV: NavItem[] = [
  { label: 'Explore', icon: 'pi pi-compass', to: '/explore' },
  // Not `nav-search`: that id named the header search link this list replaces, and the checks that
  // it is gone must not be answered by this entry.
  { label: 'Search', icon: 'pi pi-search', to: '/search', testId: 'nav-text-search' },
  { label: 'Archived', icon: 'pi pi-inbox', to: '/archived' },
  { label: 'Settings', icon: 'pi pi-cog', to: '/settings' },
]

const route = useRoute()
const onTimeline = computed(() => route.path === '/')

// The dock is ours and a routed view is our descendant, so the view's dock segment has to come to
// us: we provide the registration, the Library registers its pill on it (useDockSegment).
provide(DOCK_SEGMENT_KEY, createDockSegment())

// The Timeline's new-memo editor: its button is in our header, the inline editor in the view.
const composerOpen = ref(false)
provide(TIMELINE_COMPOSER_KEY, composerOpen)
watch(onTimeline, (on) => {
  if (!on) composerOpen.value = false
})

// The chrome's rendered height, for whatever sticks beneath it (the Library's month headers). It
// grows by a row while the connection banner shows, so it is measured, never assumed.
const chrome = ref<HTMLElement | null>(null)
const chromeHeight = ref(0)
let chromeObserver: ResizeObserver | null = null
onMounted(() => {
  const el = chrome.value
  if (!el) return
  chromeHeight.value = el.getBoundingClientRect().height
  if (typeof ResizeObserver === 'undefined') return
  chromeObserver = new ResizeObserver(() => {
    chromeHeight.value = el.getBoundingClientRect().height
  })
  chromeObserver.observe(el)
})
onBeforeUnmount(() => chromeObserver?.disconnect())

// The document scrolls every route but Places, so <main> keeps its last row above the dock; Places
// is a viewport-high view that scrolls an inner region and pads that region itself.
const mainPadClass = computed(() =>
  route.path === '/places' ? 'pb-[env(safe-area-inset-bottom)]' : 'pb-[var(--dock-clearance)]',
)

// A tap on a lazy route (Places above all) can take a second over the tunnel; until now nothing
// on screen said the tap had been taken at all.
const navigating = useNavigationProgress(useRouter())

const { isDark, toggle, setDark } = useDarkMode()
const { data: user } = useSession()
const { settings, update } = useSettings()

// Apply the server-persisted theme once it loads (source of truth across devices).
watch(settings, (s) => {
  if (s) setDark(s.theme === 'dark')
})

// Toggle AND persist to the server, else the watch above re-applies the old server theme on the
// next load and the choice appears not to stick.
function onToggleTheme() {
  toggle()
  update.mutate({ theme: isDark.value ? 'dark' : 'light' })
}

const mobileOpen = ref(false)

// True while the connection banner (or its recovery notice) is drawing the first row of chrome.
const chromeRowShown = ref(false)

// iOS standalone draws under the status bar/Dynamic Island (viewport-fit=cover +
// black-translucent), so the chrome pads with the safe-area insets — 0 in normal browsers,
// non-zero only in that context, so it needs no display-mode gating. Exactly ONE row pays the
// top inset: whichever is currently the top of the chrome. Both paying it would double the gap
// under the notch the moment the banner appears.
const headerInsetClass = computed(() =>
  chromeRowShown.value ? 'pt-0' : 'pt-[env(safe-area-inset-top)]',
)
</script>

<template>
  <div
    class="min-h-screen flex flex-col md:flex-row"
    :style="{ '--chrome-height': `${chromeHeight}px` }"
  >
    <!-- Persistent sidebar on md+ -->
    <aside
      class="hidden md:flex md:flex-col w-60 shrink-0 border-r border-surface-200 dark:border-surface-700 p-4 gap-1 pt-[calc(1rem+env(safe-area-inset-top))] pl-[calc(1rem+env(safe-area-inset-left))]"
      data-test="sidebar"
    >
      <div class="text-xl font-semibold px-2 pb-4">memos-ng</div>
      <RouterLink
        v-for="item in NAV"
        :key="item.to"
        :to="item.to"
        class="flex items-center gap-3 px-3 py-2 rounded-lg hover:bg-surface-100 dark:hover:bg-surface-800"
        active-class="bg-primary-50 dark:bg-primary-400/10 text-primary"
        :data-test="item.testId ?? `nav-${item.label.toLowerCase()}`"
      >
        <i :class="item.icon" />
        <span>{{ item.label }}</span>
      </RouterLink>
      <div class="overflow-y-auto">
        <TagList />
        <CalendarHeatmap />
      </div>
    </aside>

    <!-- Mobile drawer -->
    <Drawer
      v-model:visible="mobileOpen"
      header="memos-ng"
      data-test="mobile-drawer"
      :pt="{ header: { class: 'pt-[calc(1.25rem+env(safe-area-inset-top))]' } }"
    >
      <nav class="flex flex-col gap-1">
        <RouterLink
          v-for="item in NAV"
          :key="item.to"
          :to="item.to"
          class="flex items-center gap-3 px-3 py-2 rounded-lg hover:bg-surface-100 dark:hover:bg-surface-800"
          active-class="bg-primary-50 dark:bg-primary-400/10 text-primary"
          @click="mobileOpen = false"
        >
          <i :class="item.icon" />
          <span>{{ item.label }}</span>
        </RouterLink>
      </nav>
    </Drawer>

    <div class="flex-1 flex flex-col min-w-0">
      <!-- Banner and header stick as ONE block: this app is normally scrolled deep into the
           timeline, and a banner left in the layout flow would have scrolled away long before
           the connection drops. The chrome simply grows by a row while it shows, so it still
           covers none of the header's controls. The background and blur live here so both rows
           share them in either theme. -->
      <div
        ref="chrome"
        class="relative sticky top-0 z-30 bg-surface-50/90 backdrop-blur-sm dark:bg-surface-950/90"
        data-test="app-chrome"
      >
        <NavigationProgressBar :visible="navigating" />
        <ConnectionBanner @occupied="chromeRowShown = $event" />

        <header
          class="flex items-center gap-2 min-h-14 border-b border-surface-200 dark:border-surface-700 pl-[calc(1rem+env(safe-area-inset-left))] pr-[calc(1rem+env(safe-area-inset-right))]"
          :class="headerInsetClass"
        >
          <TimelineHeader v-if="onTimeline" v-model:open="composerOpen" />
          <Button
            v-else
            class="md:hidden"
            text
            rounded
            icon="pi pi-bars"
            aria-label="Open menu"
            data-test="menu-toggle"
            @click="mobileOpen = true"
          />
          <div class="flex-1" />
          <Button
            text
            rounded
            :icon="isDark ? 'pi pi-sun' : 'pi pi-moon'"
            :aria-label="isDark ? 'Switch to light mode' : 'Switch to dark mode'"
            data-test="dark-toggle"
            @click="onToggleTheme"
          />
          <img
            v-if="user?.avatar"
            :src="user.avatar"
            class="w-7 h-7 rounded-full object-cover"
            alt="avatar"
            data-test="header-avatar"
          />
        </header>
      </div>

      <main class="flex-1 min-w-0" :class="mainPadClass">
        <slot />
      </main>
    </div>

    <AppDock />
  </div>
</template>
