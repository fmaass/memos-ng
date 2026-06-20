<script setup lang="ts">
import { useTags } from '@/composables/useTags'

const { data: tags } = useTags()
</script>

<template>
  <div v-if="tags?.length" class="flex flex-col gap-0.5 mt-4" data-test="tag-list">
    <div class="text-xs uppercase tracking-wide text-muted-color px-3 pb-1">Tags</div>
    <RouterLink
      v-for="t in tags"
      :key="t.name"
      :to="{ path: '/', query: { tag: t.name } }"
      class="flex items-center justify-between px-3 py-1 rounded hover:bg-surface-100 dark:hover:bg-surface-800 text-sm"
      data-test="tag-item"
    >
      <span class="truncate">#{{ t.name }}</span>
      <span class="text-muted-color text-xs">{{ t.count }}</span>
    </RouterLink>
  </div>
</template>
