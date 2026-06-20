<script setup lang="ts">
import Button from 'primevue/button'

import { usePushSubscription } from '@/composables/usePushSubscription'

const version = __APP_VERSION__
const { supported, enabled, busy, enable } = usePushSubscription()
</script>

<template>
  <section class="p-6" data-test="view-settings">
    <h1 class="text-2xl font-semibold">Settings</h1>
    <p class="text-muted-color mt-2">Profile, access tokens, members — coming in T3.5.</p>

    <div class="mt-6">
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

    <p class="text-muted-color mt-8 text-sm" data-test="version">memos-ng v{{ version }}</p>
  </section>
</template>
