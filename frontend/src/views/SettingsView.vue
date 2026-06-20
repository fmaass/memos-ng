<script setup lang="ts">
import Button from 'primevue/button'

import PatManager from '@/components/PatManager.vue'
import { usePushSubscription } from '@/composables/usePushSubscription'
import { useSession } from '@/composables/useSession'

const version = __APP_VERSION__
const { data: user } = useSession()
const { supported, enabled, busy, enable } = usePushSubscription()
</script>

<template>
  <section class="max-w-2xl mx-auto p-6 flex flex-col gap-8" data-test="view-settings">
    <div>
      <h1 class="text-2xl font-semibold">Settings</h1>
    </div>

    <div data-test="profile">
      <h2 class="text-lg font-medium">Profile</h2>
      <p class="text-sm mt-1" data-test="profile-name">{{ user?.display_name ?? user?.subject }}</p>
      <p class="text-sm text-muted-color">{{ user?.email }}</p>
      <p class="text-xs text-muted-color mt-1">Identity is managed by single sign-on.</p>
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
