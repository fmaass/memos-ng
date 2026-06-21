<script setup lang="ts">
import Button from 'primevue/button'
import DatePicker from 'primevue/datepicker'
import Menu from 'primevue/menu'
import Select from 'primevue/select'
import { computed, nextTick, ref, watch } from 'vue'

import { useApi } from '@/composables/useApi'
import { useUploadQueue } from '@/composables/useResumableUpload'
import { useSettings } from '@/composables/useSettings'
import { composerKeyAction, formatMemoDate } from '@/lib/composer'
import { applyMarkdown, type MarkdownAction } from '@/lib/markdownToolbar'

const emit = defineEmits<{ saved: [memo: unknown] }>()

const api = useApi()
const { items: uploads, add: addUploads, remove: removeUpload } = useUploadQueue()
const { settings } = useSettings()

const content = ref('')
const visibility = ref<'PRIVATE' | 'PROTECTED' | 'PUBLIC'>('PRIVATE')
// Default from the user's setting. immediate so it applies even when settings are already cached
// at mount (else the picker stays on the initial PRIVATE despite a PUBLIC default).
watch(
  settings,
  (s) => {
    if (s) visibility.value = s.default_visibility
  },
  { immediate: true },
)
const visibilityOptions = [
  { label: 'Private', value: 'PRIVATE', icon: 'pi pi-lock' },
  { label: 'Protected', value: 'PROTECTED', icon: 'pi pi-users' },
  { label: 'Public', value: 'PUBLIC', icon: 'pi pi-globe' },
]
function visMeta(v: string) {
  return visibilityOptions.find((o) => o.value === v)
}

// Custom display date — null = server now; the ⋯ menu reveals a picker, then it shows as a chip.
const memoDate = ref<Date | null>(null)
const showDate = ref(false)
function clearDate() {
  memoDate.value = null
  showDate.value = false
}
const saving = ref(false)
const uploading = computed(() =>
  uploads.some((u) => u.status === 'queued' || u.status === 'uploading'),
)

const textarea = ref<HTMLTextAreaElement | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
// Track the selection so a menu action (which steals focus) still formats the right range.
let selStart = 0
let selEnd = 0
function trackSelection() {
  const el = textarea.value
  if (!el) return
  selStart = el.selectionStart
  selEnd = el.selectionEnd
}
function autosize() {
  const el = textarea.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${el.scrollHeight}px`
}
watch(content, () => nextTick(autosize))

const TOOLBAR: { icon: string; action: MarkdownAction; label: string }[] = [
  { icon: 'pi pi-bold', action: 'bold', label: 'Bold' },
  { icon: 'pi pi-italic', action: 'italic', label: 'Italic' },
  { icon: 'pi pi-code', action: 'code', label: 'Code' },
  { icon: 'pi pi-check-square', action: 'checkbox', label: 'Checkbox' },
  { icon: 'pi pi-link', action: 'link', label: 'Link' },
]
function runToolbar(action: MarkdownAction) {
  const r = applyMarkdown(content.value, selStart, selEnd, action)
  content.value = r.text
  nextTick(() => {
    const el = textarea.value
    el?.focus()
    el?.setSelectionRange(r.selectionStart, r.selectionEnd)
    selStart = r.selectionStart
    selEnd = r.selectionEnd
  })
}

const insertMenu = ref<InstanceType<typeof Menu> | null>(null)
const moreMenu = ref<InstanceType<typeof Menu> | null>(null)
const insertItems = computed(() => [
  { label: 'Attach media', icon: 'pi pi-image', command: () => fileInput.value?.click() },
  { separator: true },
  ...TOOLBAR.map((t) => ({ label: t.label, icon: t.icon, command: () => runToolbar(t.action) })),
])
const moreItems = computed(() => [
  {
    label: 'Set custom date…',
    icon: 'pi pi-clock',
    command: () => {
      showDate.value = true
    },
  },
])

function onKeydown(e: KeyboardEvent) {
  const action = composerKeyAction(e)
  if (!action) return
  e.preventDefault()
  if (action === 'save') void save()
  else runToolbar(action)
}

function onFiles(files: FileList | File[] | null) {
  if (!files) return
  const list = Array.from(files)
  if (list.length) addUploads(list)
}
function onPaste(event: ClipboardEvent) {
  const files = Array.from(event.clipboardData?.files ?? [])
  if (files.length) {
    event.preventDefault()
    addUploads(files)
  }
}

async function save() {
  if (!content.value.trim()) return
  saving.value = true
  try {
    // Finalize-on-post: send finished uploads' upload_uids; the server turns them into attachments.
    const upload_uids = uploads
      .filter((u) => u.status === 'done' && u.uploadUid)
      .map((u) => u.uploadUid!)
    const body: Record<string, unknown> = {
      content: content.value,
      visibility: visibility.value,
      pinned: false,
      upload_uids,
    }
    if (memoDate.value) body.created_at = memoDate.value.toISOString() // only when backdated
    const { data, error } = await api.POST('/api/memos', { body: body as never })
    if (error) throw error
    emit('saved', data)
    content.value = ''
    memoDate.value = null
    showDate.value = false
    uploads.length = 0
    nextTick(autosize)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div
    class="flex flex-col gap-2 rounded-xl border border-surface-200 p-3 dark:border-surface-700"
    data-test="capture-editor"
    @paste="onPaste"
    @dragover.prevent
    @drop.prevent="onFiles(($event as DragEvent).dataTransfer?.files ?? null)"
  >
    <textarea
      ref="textarea"
      v-model="content"
      rows="1"
      placeholder="Any thoughts…"
      class="max-h-[50vh] w-full resize-none overflow-y-auto whitespace-pre-wrap rounded-lg bg-transparent p-2 text-base outline-none transition focus:ring-2 focus:ring-primary/40"
      data-test="content"
      @input="autosize"
      @keydown="onKeydown"
      @select="trackSelection"
      @keyup="trackSelection"
      @mouseup="trackSelection"
      @blur="trackSelection"
    />

    <!-- backdate: a removable chip once set, or a picker the moment you choose "Set custom date…" -->
    <div
      v-if="memoDate"
      class="flex items-center gap-2 self-start rounded-full bg-surface-100 px-3 py-1 text-sm text-muted-color dark:bg-surface-800"
      data-test="date-chip"
    >
      <i class="pi pi-clock text-xs" aria-hidden="true" />
      <span>{{ formatMemoDate(memoDate) }}</span>
      <button class="pi pi-times text-xs hover:text-color" aria-label="Clear date" @click="clearDate" />
    </div>
    <DatePicker
      v-else-if="showDate"
      v-model="memoDate"
      show-time
      hour-format="24"
      size="small"
      placeholder="Pick date & time"
      class="w-full sm:w-64"
      aria-label="Memo date"
      data-test="memo-date"
    />

    <div v-if="uploads.length" class="flex flex-wrap gap-2" data-test="uploads">
      <div
        v-for="u in uploads"
        :key="u.id"
        class="group relative h-20 w-20 overflow-hidden rounded-lg bg-surface-100 dark:bg-surface-800"
        :title="u.name"
      >
        <img v-if="u.preview" :src="u.preview" :alt="u.name" class="h-full w-full object-cover" />
        <span v-else class="flex h-full w-full items-center justify-center text-muted-color">
          <i :class="u.isVideo ? 'pi pi-video' : 'pi pi-file'" class="text-xl" aria-hidden="true" />
        </span>
        <div
          v-if="u.status !== 'done'"
          class="absolute inset-0 flex items-center justify-center bg-black/45 text-xs font-medium text-white"
        >
          <span v-if="u.status === 'error'"
            ><i class="pi pi-exclamation-triangle text-red-500 dark:text-red-400" aria-hidden="true"
          /></span>
          <span v-else>{{ u.progress }}%</span>
        </div>
        <button
          class="absolute right-1 top-1 hidden h-5 w-5 items-center justify-center rounded-full bg-black/55 text-white group-hover:flex"
          aria-label="Remove"
          data-test="upload-remove"
          @click="removeUpload(u.id)"
        >
          <i class="pi pi-times text-[10px]" aria-hidden="true" />
        </button>
        <span class="absolute inset-x-0 bottom-0 truncate bg-black/40 px-1 py-0.5 text-[10px] text-white">
          {{ u.name }}
        </span>
      </div>
    </div>

    <!-- one clean bottom bar: insert + overflow on the left, visibility + Post on the right -->
    <div
      class="flex items-center justify-between gap-2 border-t border-surface-100 pt-2 dark:border-surface-800"
    >
      <div class="flex items-center gap-1">
        <Button
          text
          rounded
          severity="secondary"
          icon="pi pi-plus"
          aria-label="Insert"
          aria-haspopup="true"
          aria-controls="composer-insert-menu"
          data-test="insert-menu"
          @click="insertMenu?.toggle($event)"
        />
        <Menu id="composer-insert-menu" ref="insertMenu" :model="insertItems" popup />
        <Button
          text
          rounded
          severity="secondary"
          icon="pi pi-ellipsis-h"
          aria-label="More options"
          aria-haspopup="true"
          aria-controls="composer-more-menu"
          data-test="more-menu"
          @click="moreMenu?.toggle($event)"
        />
        <Menu id="composer-more-menu" ref="moreMenu" :model="moreItems" popup />
        <input
          ref="fileInput"
          type="file"
          multiple
          accept="image/*,video/*"
          class="hidden"
          data-test="file-input"
          @change="onFiles(($event.target as HTMLInputElement).files)"
        />
      </div>
      <div class="flex items-center gap-2">
        <Select
          v-model="visibility"
          :options="visibilityOptions"
          option-label="label"
          option-value="value"
          size="small"
          aria-label="Visibility"
          class="!border-none bg-transparent text-muted-color"
          data-test="visibility"
        >
          <template #value="{ value }">
            <span class="flex items-center gap-1.5 text-sm">
              <i :class="visMeta(value)?.icon" aria-hidden="true" />
              <span class="hidden sm:inline">{{ visMeta(value)?.label }}</span>
            </span>
          </template>
          <template #option="{ option }">
            <span class="flex items-center gap-2">
              <i :class="option.icon" aria-hidden="true" />
              {{ option.label }}
            </span>
          </template>
        </Select>
        <Button
          :label="uploading ? 'Uploading…' : 'Post'"
          size="small"
          :loading="saving"
          :disabled="!content.trim() || uploading"
          data-test="save"
          @click="save"
        />
      </div>
    </div>
  </div>
</template>
