<script setup lang="ts">
import CaptureEditor from '@/components/CaptureEditor.vue'
import MemoFeed from '@/components/MemoFeed.vue'
import { useMediaEvents } from '@/composables/useMediaEvents'

// Live media status for the home timeline (patches the ['memos','timeline'] cache).
useMediaEvents()
</script>

<template>
  <div class="max-w-2xl mx-auto p-4 flex flex-col gap-4" data-test="view-home">
    <CaptureEditor />
    <MemoFeed />
  </div>
</template>
