<script setup lang="ts">
import Button from 'primevue/button'
import DatePicker from 'primevue/datepicker'
import Popover from 'primevue/popover'
import Select from 'primevue/select'
import { useConfirm } from 'primevue/useconfirm'
import { computed, ref } from 'vue'

import MediaLightbox from '@/components/MediaLightbox.vue'
import MediaTile from '@/components/MediaTile.vue'
import type { components } from '@/api/schema'
import { renderMemoMarkdown } from '@/lib/markdown'
import { toggleTask } from '@/lib/tasks'

type Memo = components['schemas']['MemoOut']
type Visibility = Memo['visibility']

const props = defineProps<{ memo: Memo; restorable?: boolean }>()
const emit = defineEmits<{
  restore: [uid: string]
  'task-toggle': [payload: { uid: string; content: string }]
  'toggle-reaction': [payload: { uid: string; emoji: string; add: boolean }]
  'delete-attachment': [uid: string]
  'add-print': [payload: { uid: string; file: File }]
  edit: [payload: { uid: string; content: string; visibility: Visibility; created_at: string }]
}>()

const visibilityOptions = [
  { label: 'Private', value: 'PRIVATE', icon: 'pi pi-lock' },
  { label: 'Protected', value: 'PROTECTED', icon: 'pi pi-users' },
  { label: 'Public', value: 'PUBLIC', icon: 'pi pi-globe' },
]
const editing = ref(false)
const editContent = ref('')
const editVisibility = ref<Visibility>('PRIVATE')
const editDate = ref<Date | null>(null)
function startEdit() {
  editContent.value = props.memo.content
  editVisibility.value = props.memo.visibility
  editDate.value = new Date(props.memo.created_at)
  editing.value = true
}
function saveEdit() {
  emit('edit', {
    uid: props.memo.uid,
    content: editContent.value,
    visibility: editVisibility.value,
    created_at: (editDate.value ?? new Date(props.memo.created_at)).toISOString(),
  })
  editing.value = false
}

const REACTION_CHOICES = ['👍', '❤️', '😄', '🎉']
const reactPicker = ref<InstanceType<typeof Popover> | null>(null)
function toggleReaction(emoji: string, reacted: boolean) {
  emit('toggle-reaction', { uid: props.memo.uid, emoji, add: !reacted })
}
function addReaction(emoji: string) {
  reactPicker.value?.hide()
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
const confirm = useConfirm()
function onDeleteAttachment(uid: string) {
  confirm.require({
    header: 'Delete media',
    message: 'Delete this media permanently? This cannot be undone.',
    icon: 'pi pi-exclamation-triangle',
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    acceptProps: { label: 'Delete', severity: 'danger' },
    accept: () => {
      lightboxIndex.value = null
      emit('delete-attachment', uid)
    },
  })
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
    class="group relative flex flex-col gap-2 rounded-xl border border-surface-200 p-4 dark:border-surface-700"
    data-test="memo-card"
  >
    <button
      v-if="memo.mine && !editing && !restorable"
      type="button"
      class="absolute right-2 top-2 p-1.5 text-muted-color opacity-0 transition hover:text-color group-hover:opacity-100"
      aria-label="Edit memo"
      data-test="memo-edit"
      @click="startEdit"
    >
      <i class="pi pi-pencil text-sm" aria-hidden="true" />
    </button>

    <!-- edit mode: content + visibility + display date -->
    <div v-if="editing" class="flex flex-col gap-2" data-test="memo-editor">
      <textarea
        v-model="editContent"
        rows="3"
        class="w-full resize-y rounded-md border border-surface-200 bg-transparent p-2 outline-none dark:border-surface-700"
        data-test="edit-content"
      />
      <div class="flex flex-wrap items-center gap-2">
        <DatePicker
          v-model="editDate"
          show-time
          hour-format="24"
          show-icon
          icon-display="input"
          size="small"
          date-format="M d, yy"
          class="w-48"
          aria-label="Memo date"
          data-test="edit-date"
        />
        <Select
          v-model="editVisibility"
          :options="visibilityOptions"
          option-label="label"
          option-value="value"
          size="small"
          aria-label="Visibility"
          class="text-muted-color"
          data-test="edit-visibility"
        />
        <div class="ml-auto flex gap-1">
          <Button text size="small" label="Cancel" data-test="edit-cancel" @click="editing = false" />
          <Button size="small" label="Save" icon="pi pi-check" data-test="edit-save" @click="saveEdit" />
        </div>
      </div>
    </div>

    <!-- eslint-disable-next-line vue/no-v-html -- content sanitized by renderMemoMarkdown -->
    <div
      v-else
      class="memo-content break-words"
      data-test="memo-content"
      v-html="html"
      @click="onContentClick"
    />

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
      @add-print="emit('add-print', $event)"
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
      <button
        type="button"
        class="text-xs px-1.5 py-0.5 text-muted-color hover:text-color"
        aria-label="Add reaction"
        aria-haspopup="true"
        aria-controls="react-picker"
        data-test="react-add"
        @click="reactPicker?.toggle($event)"
      >
        <i class="pi pi-face-smile" aria-hidden="true" />
      </button>
      <Popover id="react-picker" ref="reactPicker" data-test="react-picker">
        <div class="flex gap-1">
          <button
            v-for="e in REACTION_CHOICES"
            :key="e"
            type="button"
            class="rounded px-1 text-lg transition hover:scale-125"
            :aria-label="`React ${e}`"
            :data-test="`react-choice-${e}`"
            @click="addReaction(e)"
          >
            {{ e }}
          </button>
        </div>
      </Popover>
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
