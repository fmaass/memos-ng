<script setup lang="ts">
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import SelectButton from 'primevue/selectbutton'

import { computed, ref, watch } from 'vue'

import PatManager from '@/components/PatManager.vue'
import { useDarkMode } from '@/composables/useDarkMode'
import { usePushSubscription } from '@/composables/usePushSubscription'
import { useSession } from '@/composables/useSession'
import { type SettingsPatch, frameTestMessage, useSettings } from '@/composables/useSettings'

const version = __APP_VERSION__
const { data: user } = useSession()
const initials = computed(() =>
  (user.value?.display_name ?? user.value?.subject ?? '?').slice(0, 2).toUpperCase(),
)

function onAvatarFile(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  const reader = new FileReader()
  reader.onload = () => update.mutate({ avatar: String(reader.result) })
  reader.readAsDataURL(file)
}
const { supported, enabled, busy, enable } = usePushSubscription()
const { settings, update, frameTest } = useSettings()
const { setDark } = useDarkMode()

// Aura frame — local edit buffers seeded from the persisted settings.
const frameSendTo = ref('')
const frameSendFrom = ref('')
watch(
  settings,
  (s) => {
    if (s) {
      frameSendTo.value = s.frame_send_to ?? ''
      frameSendFrom.value = s.frame_send_from ?? ''
    }
  },
  { immediate: true },
)
const frameResult = ref<{ severity: 'success' | 'error'; text: string } | null>(null)

function saveFrame() {
  frameResult.value = null
  update.mutate({ frame_send_to: frameSendTo.value, frame_send_from: frameSendFrom.value })
}
async function sendFrameTest() {
  frameResult.value = null
  try {
    frameResult.value = frameTestMessage(await frameTest.mutateAsync())
  } catch (e) {
    frameResult.value = { severity: 'error', text: e instanceof Error ? e.message : 'Test failed' }
  }
}

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
  <section class="max-w-2xl mx-auto p-6 flex flex-col gap-8" data-test="view-settings">
    <div>
      <h1 class="text-2xl font-semibold">Settings</h1>
    </div>

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
      <p class="text-sm mt-2" data-test="profile-name">{{ user?.display_name ?? user?.subject }}</p>
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

    <div v-if="settings" data-test="aura-frame" class="flex flex-col gap-3">
      <div>
        <h2 class="text-lg font-medium">Aura frame</h2>
        <p class="text-sm text-muted-color">
          Photos in a memo tagged <code>#curated</code> are emailed to your Aura frame.
        </p>
      </div>
      <label class="flex flex-col gap-1 text-sm">
        Send to (frame address)
        <InputText
          v-model="frameSendTo"
          placeholder="…@send.auraframes.com"
          data-test="frame-send-to"
        />
      </label>
      <label class="flex flex-col gap-1 text-sm">
        Send from (your Aura member address)
        <InputText v-model="frameSendFrom" placeholder="you@icloud.com" data-test="frame-send-from" />
      </label>
      <div class="flex items-center gap-2">
        <Button label="Save" size="small" data-test="frame-save" @click="saveFrame" />
        <Button
          label="Send test photo"
          size="small"
          severity="secondary"
          icon="pi pi-image"
          :loading="frameTest.isPending.value"
          :disabled="!frameSendTo || !frameSendFrom"
          data-test="frame-test"
          @click="sendFrameTest"
        />
      </div>
      <p
        v-if="frameResult"
        class="text-sm"
        :class="frameResult.severity === 'success' ? 'text-green-500' : 'text-red-500'"
        data-test="frame-test-result"
      >
        {{ frameResult.text }}
      </p>
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

    <p class="text-muted-color text-sm" data-test="version">memos-ng v{{ version }}</p>
  </section>
</template>
