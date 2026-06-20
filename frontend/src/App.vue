<script setup lang="ts">
import ConfirmDialog from 'primevue/confirmdialog'
import Toast from 'primevue/toast'

import AppShell from '@/layouts/AppShell.vue'
</script>

<template>
  <AppShell>
    <RouterView />
  </AppShell>
  <Toast />
  <ConfirmDialog />
</template>
