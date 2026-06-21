<script setup lang="ts">
import ConfirmDialog from 'primevue/confirmdialog'
import Toast from 'primevue/toast'

import AppShell from '@/layouts/AppShell.vue'
import { useMediaEvents } from '@/composables/useMediaEvents'

// One app-wide SSE subscription so media status updates live on EVERY feed (home, explore,
// search…), not just the view that happened to mount it.
useMediaEvents()
</script>

<template>
  <AppShell>
    <RouterView />
  </AppShell>
  <Toast />
  <ConfirmDialog />
</template>
