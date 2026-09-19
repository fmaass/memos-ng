<script setup lang="ts">
// The one piece of chrome that says a tap was taken. It is absolutely positioned so it costs the
// layout nothing: the map's first fit must see exactly the pane it saw before.
defineProps<{ visible: boolean }>()
</script>

<template>
  <div
    v-if="visible"
    class="pointer-events-none absolute inset-x-0 top-0 h-0.5 overflow-hidden"
    role="progressbar"
    aria-label="Loading the page"
    data-test="nav-progress"
  >
    <div
      class="h-full w-full bg-primary animate-pulse motion-reduce:animate-none"
      data-test="nav-progress-fill"
    />
  </div>
</template>
