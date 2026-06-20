<script setup lang="ts">
import { computed, ref } from 'vue'

import MediaLightbox from '@/components/MediaLightbox.vue'
import MediaTile from '@/components/MediaTile.vue'
import type { components } from '@/api/schema'
import { renderMemoMarkdown } from '@/lib/markdown'
import { toggleTask } from '@/lib/tasks'

type Memo = components['schemas']['MemoOut']

const props = defineProps<{ memo: Memo; restorable?: boolean }>()
const emit = defineEmits<{
  restore: [uid: string]
  'task-toggle': [payload: { uid: string; content: string }]
  'toggle-reaction': [payload: { uid: string; emoji: string; add: boolean }]
  'delete-attachment': [uid: string]
}>()

const REACTION_CHOICES = ['👍', '❤️', '😄', '🎉']
const pickerOpen = ref(false)
function toggleReaction(emoji: string, reacted: boolean) {
  emit('toggle-reaction', { uid: props.memo.uid, emoji, add: !reacted })
}
function addReaction(emoji: string) {
  pickerOpen.value = false
  emit('toggle-reaction', { uid: props.memo.uid, emoji, add: true })
}

// renderMemoMarkdown sanitizes (markdown-it html:false + DOMPurify allowlist), so v-html
// of its output is safe — raw HTML/script in memo content is escaped to inert text.
const html = computed(() => renderMemoMarkdown(props.memo.content))

// Lightbox over ALL of the memo's media (images + videos) — arrow left/right through them.
const lightboxIndex = ref<number | null>(null)
function openLightbox(uid: string) {
  const i = (props.memo.attachments ?? []).findIndex((a) => a.uid === uid)
  if (i >= 0) lightboxIndex.value = i
}
function onDeleteAttachment(uid: string) {
  if (!window.confirm('Delete this media permanently? This cannot be undone.')) return
  lightboxIndex.value = null
  emit('delete-attachment', uid)
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

    <MediaLightbox
      v-model="lightboxIndex"
      :items="memo.attachments ?? []"
      @delete="onDeleteAttachment"
    />

    <div v-if="memo.tags.length" class="flex flex-wrap gap-1">
      <span
        v-for="t in memo.tags"
        :key="t"
        class="text-xs px-2 py-0.5 rounded-full bg-primary-50 dark:bg-primary-400/10 text-primary"
        data-test="tag-chip"
        >#{{ t }}</span
      >
    </div>

    <div class="flex flex-wrap items-center gap-1" data-test="reactions">
      <button
        v-for="r in memo.reactions"
        :key="r.emoji"
        type="button"
        class="text-xs px-2 py-0.5 rounded-full border"
        :class="
          r.reacted
            ? 'border-primary bg-primary-50 dark:bg-primary-400/10'
            : 'border-surface-200 dark:border-surface-700'
        "
        data-test="reaction-chip"
        @click="toggleReaction(r.emoji, r.reacted)"
      >
        {{ r.emoji }} {{ r.count }}
      </button>
      <span class="relative">
        <button
          type="button"
          class="text-xs px-1.5 py-0.5 text-muted-color hover:text-color"
          aria-label="Add reaction"
          data-test="react-add"
          @click="pickerOpen = !pickerOpen"
        >
          <i class="pi pi-face-smile" />
        </button>
        <span
          v-if="pickerOpen"
          class="absolute z-10 left-0 mt-1 flex gap-1 rounded border border-surface-200 dark:border-surface-700 bg-surface-0 dark:bg-surface-900 p-1"
          data-test="react-picker"
        >
          <button
            v-for="e in REACTION_CHOICES"
            :key="e"
            type="button"
            class="text-sm px-1"
            :data-test="`react-choice-${e}`"
            @click="addReaction(e)"
          >
            {{ e }}
          </button>
        </span>
      </span>
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
