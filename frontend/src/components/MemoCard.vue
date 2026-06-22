<script setup lang="ts">
import Button from 'primevue/button'
import DatePicker from 'primevue/datepicker'
import Menu from 'primevue/menu'
import Popover from 'primevue/popover'
import Select from 'primevue/select'
import { useConfirm } from 'primevue/useconfirm'
import { computed, onUnmounted, ref } from 'vue'

import MediaTile from '@/components/MediaTile.vue'
import type { components } from '@/api/schema'
import { useMediaLightbox } from '@/composables/useMediaLightbox'
import { useUploadQueue } from '@/composables/useResumableUpload'
import { attachmentUidsToClear } from '@/lib/attachments'
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
  'delete-attachments': [uids: string[]] // bulk remove — deleted SEQUENTIALLY by the parent
  'delete-memo': [uid: string]
  'add-print': [payload: { uid: string; file: File }]
  edit: [
    payload: {
      uid: string
      content: string
      visibility: Visibility
      created_at: string
      upload_uids: string[] // new images added while editing
    },
  ]
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
// New images added while editing — uploaded in the background, attached on Save (finalize-on-patch).
// Removing an existing image is a delete (onDeleteAttachment), so it's immediate, not staged.
const { items: editUploads, add: addEditUploads, remove: removeEditUpload } = useUploadQueue()
const editFileInput = ref<HTMLInputElement | null>(null)
const editUploading = computed(() =>
  editUploads.some((u) => u.status === 'queued' || u.status === 'uploading'),
)
function onEditFiles(files: FileList | null) {
  if (files?.length) addEditUploads(Array.from(files))
}
function startEdit() {
  editContent.value = props.memo.content
  editVisibility.value = props.memo.visibility
  editDate.value = new Date(props.memo.created_at)
  editUploads.length = 0
  editing.value = true
}
function saveEdit() {
  emit('edit', {
    uid: props.memo.uid,
    content: editContent.value,
    visibility: editVisibility.value,
    created_at: (editDate.value ?? new Date(props.memo.created_at)).toISOString(),
    upload_uids: editUploads
      .filter((u) => u.status === 'done' && u.uploadUid)
      .map((u) => u.uploadUid as string),
  })
  editUploads.length = 0
  editing.value = false
}

// Owner actions menu (⋯) — always visible (the old hover-only pencil was invisible on touch).
const actionsMenu = ref<InstanceType<typeof Menu> | null>(null)
const memoActions = computed(() => [
  { label: 'Edit', icon: 'pi pi-pencil', testId: 'memo-action-edit', command: startEdit },
  { label: 'Delete', icon: 'pi pi-trash', testId: 'memo-action-delete', command: confirmDeleteMemo },
])
function confirmDeleteMemo() {
  confirm.require({
    header: 'Delete memo',
    message: 'Delete this memo? This cannot be undone.',
    icon: 'pi pi-exclamation-triangle',
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    acceptProps: { label: 'Delete', severity: 'danger' },
    accept: () => emit('delete-memo', props.memo.uid),
  })
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

// A subtle badge on memos that carry print masters (any attachment with a hidden print/rendition).
const hasPrints = computed(() => (props.memo.attachments ?? []).some((a) => a.hasPrint))

// Full-screen viewer (PhotoSwipe) over ALL of the memo's media — swipe/zoom images, play videos.
const lightbox = useMediaLightbox()
function openLightbox(uid: string) {
  const atts = props.memo.attachments ?? []
  const i = atts.findIndex((a) => a.uid === uid)
  if (i >= 0)
    lightbox.open(atts, i, {
      onDelete: onDeleteAttachment,
      onAddPrint: (uid, file) => emit('add-print', { uid, file }),
    })
}
onUnmounted(() => lightbox.destroy())
const confirm = useConfirm()
function onDeleteAttachment(uid: string) {
  confirm.require({
    header: 'Delete media',
    message: 'Delete this media permanently? This cannot be undone.',
    icon: 'pi pi-exclamation-triangle',
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    acceptProps: { label: 'Delete', severity: 'danger' },
    accept: () => emit('delete-attachment', uid),
  })
}

// "Remove all" — one confirm, then delete every existing photo (reuses the per-uid delete path). Lets
// the owner wipe a memo and re-upload screen+print pairs, which file into the per-memo folder.
function onRemoveAllAttachments() {
  const uids = attachmentUidsToClear(props.memo)
  if (!uids.length) return
  confirm.require({
    header: 'Remove all photos',
    message: `Remove all ${uids.length} photos from this memo? This cannot be undone.`,
    icon: 'pi pi-exclamation-triangle',
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    acceptProps: { label: 'Remove all', severity: 'danger' },
    accept: () => {
      // ONE event with all uids — the parent deletes them sequentially. Emitting N single deletes
      // would fan out N concurrent requests and exhaust the bounded shared Postgres pool (5/5).
      emit('delete-attachments', uids)
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
    class="group relative isolate flex flex-col gap-2 rounded-xl border border-surface-200 p-4 dark:border-surface-700"
    data-test="memo-card"
  >
    <template v-if="memo.mine && !editing && !restorable">
      <button
        type="button"
        class="absolute right-2 top-2 z-20 rounded-full bg-surface-0/70 p-1.5 text-muted-color backdrop-blur-sm transition hover:text-color dark:bg-surface-900/60"
        aria-label="Memo actions"
        aria-haspopup="true"
        aria-controls="memo-actions"
        data-test="memo-edit"
        @click="actionsMenu?.toggle($event)"
      >
        <i class="pi pi-ellipsis-h text-sm" aria-hidden="true" />
      </button>
      <Menu id="memo-actions" ref="actionsMenu" :model="memoActions" popup>
        <template #item="{ item, props }">
          <a class="flex items-center gap-2 px-3 py-2" :data-test="item.testId" v-bind="props.action">
            <i :class="item.icon" aria-hidden="true" />
            <span>{{ item.label }}</span>
          </a>
        </template>
      </Menu>
    </template>

    <!-- edit mode: content + visibility + display date -->
    <div v-if="editing" class="flex flex-col gap-2" data-test="memo-editor">
      <textarea
        v-model="editContent"
        rows="3"
        class="w-full resize-y rounded-md border border-surface-200 bg-transparent p-2 outline-none dark:border-surface-700"
        data-test="edit-content"
      />

      <!-- attachments: remove an existing image (immediate delete) or add new ones (attached on Save) -->
      <div
        v-if="memo.attachments?.length || editUploads.length"
        class="flex flex-wrap gap-2"
        data-test="edit-attachments"
      >
        <div
          v-for="a in memo.attachments"
          :key="a.uid"
          class="relative h-16 w-16 overflow-hidden rounded-md bg-surface-100 dark:bg-surface-800"
        >
          <img :src="a.thumbnailUrl" :alt="a.uid" class="h-full w-full object-cover" />
          <button
            type="button"
            class="absolute right-0.5 top-0.5 flex h-5 w-5 items-center justify-center rounded-full bg-black/55 text-white"
            aria-label="Remove image"
            data-test="edit-remove-attachment"
            @click="onDeleteAttachment(a.uid)"
          >
            <i class="pi pi-times text-[10px]" aria-hidden="true" />
          </button>
        </div>
        <div
          v-for="u in editUploads"
          :key="u.id"
          class="relative h-16 w-16 overflow-hidden rounded-md bg-surface-100 dark:bg-surface-800"
        >
          <img v-if="u.preview" :src="u.preview" :alt="u.name" class="h-full w-full object-cover" />
          <span v-else class="flex h-full w-full items-center justify-center text-muted-color">
            <i :class="u.isVideo ? 'pi pi-video' : 'pi pi-file'" aria-hidden="true" />
          </span>
          <div
            v-if="u.status !== 'done'"
            class="absolute inset-0 flex items-center justify-center bg-black/45 text-xs font-medium text-white"
          >
            {{ u.progress }}%
          </div>
          <button
            type="button"
            class="absolute right-0.5 top-0.5 flex h-5 w-5 items-center justify-center rounded-full bg-black/55 text-white"
            aria-label="Remove"
            @click="removeEditUpload(u.id)"
          >
            <i class="pi pi-times text-[10px]" aria-hidden="true" />
          </button>
        </div>
      </div>
      <div class="flex items-center gap-1">
        <Button
          text
          size="small"
          icon="pi pi-image"
          label="Add image"
          data-test="edit-add-image"
          @click="editFileInput?.click()"
        />
        <Button
          v-if="memo.attachments?.length"
          text
          size="small"
          severity="danger"
          icon="pi pi-trash"
          label="Remove all"
          data-test="edit-remove-all"
          @click="onRemoveAllAttachments"
        />
        <input
          ref="editFileInput"
          type="file"
          multiple
          accept="image/*,video/*"
          class="hidden"
          data-test="edit-file-input"
          @change="onEditFiles(($event.target as HTMLInputElement).files)"
        />
      </div>

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
          <Button
            size="small"
            :label="editUploading ? 'Uploading…' : 'Save'"
            icon="pi pi-check"
            :disabled="editUploading"
            data-test="edit-save"
            @click="saveEdit"
          />
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

    <div v-if="memo.tags.length || hasPrints" class="flex flex-wrap items-center gap-1">
      <span
        v-for="t in memo.tags"
        :key="t"
        class="text-xs px-2 py-0.5 rounded-full bg-primary-50 dark:bg-primary-400/10 text-primary"
        data-test="tag-chip"
        >#{{ t }}</span
      >
      <span
        v-if="hasPrints"
        class="text-xs px-2 py-0.5 rounded-full bg-surface-200 dark:bg-surface-700 text-muted-color"
        data-test="print-badge"
        title="This memo has print versions"
        aria-label="Has print versions"
        >🖨</span
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
