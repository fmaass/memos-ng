<script setup lang="ts">
import Galleria from 'primevue/galleria'
import { computed, ref } from 'vue'

import MediaTile from '@/components/MediaTile.vue'
import type { components } from '@/api/schema'
import { renderMemoMarkdown } from '@/lib/markdown'
import { tileKind } from '@/lib/media'
import { toggleTask } from '@/lib/tasks'

type Memo = components['schemas']['MemoOut']

const props = defineProps<{ memo: Memo; restorable?: boolean }>()
const emit = defineEmits<{
  restore: [uid: string]
  'task-toggle': [payload: { uid: string; content: string }]
}>()

// renderMemoMarkdown sanitizes (markdown-it html:false + DOMPurify allowlist), so v-html
// of its output is safe — raw HTML/script in memo content is escaped to inert text.
const html = computed(() => renderMemoMarkdown(props.memo.content))

// Lightbox over the memo's ready images (Galleria, full-screen mask).
const images = computed(() =>
  (props.memo.attachments ?? []).filter((a) => tileKind(a.status, a.mime) === 'image'),
)
const lightboxOpen = ref(false)
const lightboxIndex = ref(0)
function openLightbox(uid: string) {
  const i = images.value.findIndex((a) => a.uid === uid)
  if (i >= 0) {
    lightboxIndex.value = i
    lightboxOpen.value = true
  }
}

// Clicking a rendered task checkbox flips it in the source markdown and asks the parent to
// persist (PATCH). preventDefault so the box reflects server truth on re-render, not the click.
function onContentClick(event: MouseEvent) {
  const target = event.target
  if (!(target instanceof HTMLInputElement) || !target.classList.contains('task-list-item-checkbox'))
    return
  event.preventDefault()
  const root = event.currentTarget as HTMLElement
  const index = Array.from(root.querySelectorAll('input.task-list-item-checkbox')).indexOf(target)
  if (index >= 0) {
    emit('task-toggle', { uid: props.memo.uid, content: toggleTask(props.memo.content, index) })
  }
}
</script>

<template>
  <article
    class="rounded-xl border border-surface-200 dark:border-surface-700 p-4 flex flex-col gap-2"
    data-test="memo-card"
  >
    <!-- eslint-disable-next-line vue/no-v-html -- content sanitized by renderMemoMarkdown -->
    <div class="memo-content break-words" data-test="memo-content" v-html="html" @click="onContentClick" />

    <div
      v-if="memo.attachments?.length"
      class="grid grid-cols-2 sm:grid-cols-3 gap-2"
      data-test="media-grid"
    >
      <MediaTile v-for="a in memo.attachments" :key="a.uid" :att="a" @open="openLightbox" />
    </div>

    <Galleria
      v-if="images.length"
      v-model:active-index="lightboxIndex"
      v-model:visible="lightboxOpen"
      :value="images"
      :full-screen="true"
      :circular="true"
      :show-thumbnails="false"
      data-test="lightbox"
    >
      <template #item="{ item }">
        <img :src="item.previewUrl" :alt="item.uid" class="max-h-screen max-w-full object-contain" />
      </template>
    </Galleria>

    <div v-if="memo.tags.length" class="flex flex-wrap gap-1">
      <span
        v-for="t in memo.tags"
        :key="t"
        class="text-xs px-2 py-0.5 rounded-full bg-primary-50 dark:bg-primary-400/10 text-primary"
        data-test="tag-chip"
        >#{{ t }}</span
      >
    </div>
    <div v-if="restorable" class="flex justify-end">
      <button
        class="text-sm text-primary hover:underline"
        data-test="restore"
        @click="emit('restore', memo.uid)"
      >
        Restore
      </button>
    </div>
  </article>
</template>
