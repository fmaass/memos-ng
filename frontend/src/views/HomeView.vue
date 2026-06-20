<script setup lang="ts">
import CaptureEditor from '@/components/CaptureEditor.vue'
import MemoTimeline from '@/components/MemoTimeline.vue'
</script>

<template>
  <div class="max-w-2xl mx-auto p-4 flex flex-col gap-4" data-test="view-home">
    <CaptureEditor />
    <MemoTimeline />
  </div>
</template>
