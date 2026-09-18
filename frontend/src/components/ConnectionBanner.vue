<script setup lang="ts">
import Button from 'primevue/button'

import { useReachability } from '@/composables/useReachability'

const { unreachable, retryNow } = useReachability()
</script>

<template>
  <!-- Non-blocking and it never covers anything: it sits above the sticky header in the layout
       flow rather than floating over it, so every header control stays clickable while it shows.
       `role="status"` + polite live region so a screen reader hears it without being interrupted;
       it appears and disappears on its own as the verdict changes. -->
  <div
    v-if="unreachable"
    role="status"
    aria-live="polite"
    data-test="connection-banner"
    class="flex items-center gap-2 text-sm bg-amber-100 text-amber-900 dark:bg-amber-400/15 dark:text-amber-200 border-b border-amber-300 dark:border-amber-400/30 pt-[calc(0.5rem+env(safe-area-inset-top))] pb-2 pl-[calc(1rem+env(safe-area-inset-left))] pr-[calc(0.5rem+env(safe-area-inset-right))]"
  >
    <i class="pi pi-exclamation-triangle shrink-0" aria-hidden="true" />
    <span class="flex-1 min-w-0">Can't reach the server. Retrying…</span>
    <Button
      text
      size="small"
      label="Retry now"
      data-test="connection-retry"
      class="!text-amber-900 dark:!text-amber-200"
      @click="retryNow"
    />
  </div>
</template>
