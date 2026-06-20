<script setup lang="ts">
import CaptureEditor from '@/components/CaptureEditor.vue'
</script>

<template>
  <div class="max-w-2xl mx-auto p-4 flex flex-col gap-4" data-test="view-home">
    <CaptureEditor />
    <p class="text-muted-color text-sm" data-test="timeline-placeholder">
      Your timeline — coming in T3.3.
    </p>
  </div>
</template>
