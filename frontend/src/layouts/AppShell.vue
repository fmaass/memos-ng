<script setup lang="ts">
import Button from 'primevue/button'
import Drawer from 'primevue/drawer'
import { computed, ref, watch } from 'vue'

import CalendarHeatmap from '@/components/CalendarHeatmap.vue'
import ConnectionBanner from '@/components/ConnectionBanner.vue'
import TagList from '@/components/TagList.vue'
import { useDarkMode } from '@/composables/useDarkMode'
import { useSession } from '@/composables/useSession'
import { useSettings } from '@/composables/useSettings'

// The primary destinations, mirroring the original memos sidebar. One list, both navigations.
interface NavItem {
  label: string
  icon: string
  to: string
}
const NAV: NavItem[] = [
  { label: 'Home', icon: 'pi pi-home', to: '/' },
  { label: 'Explore', icon: 'pi pi-compass', to: '/explore' },
  { label: 'Places', icon: 'pi pi-map', to: '/places' },
  { label: 'Archived', icon: 'pi pi-inbox', to: '/archived' },
  { label: 'Resources', icon: 'pi pi-images', to: '/resources' },
  { label: 'Settings', icon: 'pi pi-cog', to: '/settings' },
]

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
  <div class="min-h-screen flex flex-col md:flex-row">
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
        :data-test="`nav-${item.label.toLowerCase()}`"
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
        class="sticky top-0 z-30 bg-surface-50/90 backdrop-blur-sm dark:bg-surface-950/90"
        data-test="app-chrome"
      >
        <ConnectionBanner @occupied="chromeRowShown = $event" />

        <header
          class="flex items-center gap-2 min-h-14 border-b border-surface-200 dark:border-surface-700 pl-[calc(1rem+env(safe-area-inset-left))] pr-[calc(1rem+env(safe-area-inset-right))]"
          :class="headerInsetClass"
        >
          <Button
            class="md:hidden"
            text
            rounded
            icon="pi pi-bars"
            aria-label="Open menu"
            data-test="menu-toggle"
            @click="mobileOpen = true"
          />
          <div class="flex-1" />
          <RouterLink to="/search" aria-label="Search" data-test="nav-search">
            <Button text rounded icon="pi pi-search" aria-label="Search" />
          </RouterLink>
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

      <main class="flex-1 min-w-0 pb-[env(safe-area-inset-bottom)]">
        <slot />
      </main>
    </div>
  </div>
</template>
