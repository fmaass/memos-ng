<script setup lang="ts">
import Button from 'primevue/button'
import SelectButton from 'primevue/selectbutton'
import Tab from 'primevue/tab'
import TabList from 'primevue/tablist'
import TabPanel from 'primevue/tabpanel'
import TabPanels from 'primevue/tabpanels'
import Tabs from 'primevue/tabs'

import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import DigestPanel from '@/components/DigestPanel.vue'
import PatManager from '@/components/PatManager.vue'
import { useDarkMode } from '@/composables/useDarkMode'
import { usePushSubscription } from '@/composables/usePushSubscription'
import { useSession } from '@/composables/useSession'
import { type SettingsPatch, useSettings } from '@/composables/useSettings'
import { type SettingsTab, tabFromQuery } from '@/lib/digest'

const version = __APP_VERSION__
const { data: user } = useSession()
const initials = computed(() =>
  (user.value?.display_name ?? user.value?.subject ?? '?').slice(0, 2).toUpperCase(),
)

// The Mail tab is not rendered for a non-admin — not disabled, not a stub. The refusal that
// matters is the API's `require_admin`; this only keeps a panel out of a view that could not use it.
const isAdmin = computed(() => user.value?.role === 'admin')

const route = useRoute()
const router = useRouter()

// The tab lives in the URL, so it is linkable and survives a reload. The session arrives
// asynchronously, so `?tab=mail` is re-resolved once the role is known — before that an admin
// looks like a non-admin and would otherwise be dropped onto General for good.
const activeTab = ref<SettingsTab>(tabFromQuery(route.query.tab, isAdmin.value))
watch([() => route.query.tab, isAdmin], ([tab, admin]) => {
  activeTab.value = tabFromQuery(tab, admin)
})

function selectTab(value: string | number) {
  const tab = tabFromQuery(String(value), isAdmin.value)
  activeTab.value = tab
  if (route.query.tab !== tab) void router.replace({ query: { ...route.query, tab } })
}

function onAvatarFile(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  const reader = new FileReader()
  reader.onload = () => update.mutate({ avatar: String(reader.result) })
  reader.readAsDataURL(file)
}
const { supported, enabled, busy, enable } = usePushSubscription()
const { settings, update } = useSettings()
const { setDark } = useDarkMode()

const visibilityOptions = [
  { label: 'Private', value: 'PRIVATE' },
  { label: 'Protected', value: 'PROTECTED' },
  { label: 'Public', value: 'PUBLIC' },
]
const themeOptions = [
  { label: 'Dark', value: 'dark' },
  { label: 'Light', value: 'light' },
]

function setVisibility(value: SettingsPatch['default_visibility']) {
  if (value) update.mutate({ default_visibility: value })
}
function setTheme(value: string) {
  if (!value) return
  update.mutate({ theme: value })
  setDark(value === 'dark') // apply immediately
}
</script>

<template>
  <section class="max-w-2xl mx-auto p-6 flex flex-col gap-6" data-test="view-settings">
    <div>
      <h1 class="text-2xl font-semibold">Settings</h1>
    </div>

    <Tabs :value="activeTab" data-test="settings-tabs" @update:value="selectTab">
      <TabList>
        <Tab value="general" data-test="settings-tab-general">General</Tab>
        <Tab v-if="isAdmin" value="mail" data-test="settings-tab-mail">Mail</Tab>
      </TabList>

      <TabPanels>
        <TabPanel value="general">
          <div class="flex flex-col gap-8 pt-2">
            <div data-test="profile">
              <h2 class="text-lg font-medium">Profile</h2>
              <div class="flex items-center gap-3 mt-2">
                <img
                  v-if="settings?.avatar"
                  :src="settings.avatar"
                  class="w-16 h-16 rounded-full object-cover"
                  alt="avatar"
                  data-test="avatar-img"
                />
                <div
                  v-else
                  class="w-16 h-16 rounded-full bg-surface-200 dark:bg-surface-700 flex items-center justify-center text-lg"
                  data-test="avatar-initials"
                >
                  {{ initials }}
                </div>
                <div class="flex flex-col gap-1">
                  <label class="text-sm text-primary cursor-pointer" data-test="avatar-upload-label">
                    Change avatar
                    <input
                      type="file"
                      accept="image/*"
                      class="hidden"
                      data-test="avatar-input"
                      @change="onAvatarFile"
                    />
                  </label>
                  <button
                    v-if="settings?.avatar"
                    class="text-sm text-muted-color text-left hover:underline"
                    data-test="avatar-clear"
                    @click="update.mutate({ avatar: '' })"
                  >
                    Remove
                  </button>
                </div>
              </div>
              <p class="text-sm mt-2" data-test="profile-name">
                {{ user?.display_name ?? user?.subject }}
              </p>
              <p class="text-sm text-muted-color">{{ user?.email }}</p>
              <p class="text-xs text-muted-color mt-1">Identity is managed by single sign-on.</p>
            </div>

            <div v-if="settings" data-test="preferences" class="flex flex-col gap-4">
              <div>
                <h2 class="text-lg font-medium">Default visibility</h2>
                <p class="text-sm text-muted-color mb-2">New memos use this unless you change it.</p>
                <SelectButton
                  :model-value="settings.default_visibility"
                  :options="visibilityOptions"
                  option-label="label"
                  option-value="value"
                  :allow-empty="false"
                  data-test="default-visibility"
                  @update:model-value="setVisibility"
                />
              </div>
              <div>
                <h2 class="text-lg font-medium">Theme</h2>
                <SelectButton
                  :model-value="settings.theme"
                  :options="themeOptions"
                  option-label="label"
                  option-value="value"
                  :allow-empty="false"
                  data-test="theme"
                  @update:model-value="setTheme"
                />
              </div>
            </div>

            <PatManager />

            <div>
              <h2 class="text-lg font-medium">Notifications</h2>
              <p v-if="!supported" class="text-muted-color text-sm mt-1" data-test="push-unsupported">
                Push notifications aren't supported in this browser.
              </p>
              <Button
                v-else-if="!enabled"
                class="mt-2"
                :loading="busy"
                label="Enable notifications"
                icon="pi pi-bell"
                data-test="push-enable"
                @click="enable"
              />
              <p v-else class="text-muted-color text-sm mt-1" data-test="push-enabled">
                Notifications enabled for this device.
              </p>
            </div>
          </div>
        </TabPanel>

        <TabPanel v-if="isAdmin" value="mail">
          <div class="pt-2">
            <DigestPanel />
          </div>
        </TabPanel>
      </TabPanels>
    </Tabs>

    <p class="text-muted-color text-sm" data-test="version">memos-ng v{{ version }}</p>
  </section>
</template>
