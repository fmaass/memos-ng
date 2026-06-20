<script setup lang="ts">
import { computed } from 'vue'

import MediaTile from '@/components/MediaTile.vue'
import type { components } from '@/api/schema'
import { renderMemoMarkdown } from '@/lib/markdown'

type Memo = components['schemas']['MemoOut']

const props = defineProps<{ memo: Memo }>()

// renderMemoMarkdown sanitizes (markdown-it html:false + DOMPurify allowlist), so v-html
// of its output is safe — raw HTML/script in memo content is escaped to inert text.
const html = computed(() => renderMemoMarkdown(props.memo.content))
</script>

<template>
  <article
    class="rounded-xl border border-surface-200 dark:border-surface-700 p-4 flex flex-col gap-2"
    data-test="memo-card"
  >
    <!-- eslint-disable-next-line vue/no-v-html -- content sanitized by renderMemoMarkdown -->
    <div class="memo-content break-words" data-test="memo-content" v-html="html" />
    <div
      v-if="memo.attachments?.length"
      class="grid grid-cols-2 sm:grid-cols-3 gap-2"
      data-test="media-grid"
    >
      <MediaTile v-for="a in memo.attachments" :key="a.uid" :att="a" />
    </div>
    <div v-if="memo.tags.length" class="flex flex-wrap gap-1">
      <span
        v-for="t in memo.tags"
        :key="t"
        class="text-xs px-2 py-0.5 rounded-full bg-primary-50 dark:bg-primary-400/10 text-primary"
        data-test="tag-chip"
        >#{{ t }}</span
      >
    </div>
  </article>
</template>
