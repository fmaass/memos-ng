<script setup lang="ts">
import Button from 'primevue/button'
import DatePicker from 'primevue/datepicker'
import Select from 'primevue/select'
import { computed, nextTick, ref, watch } from 'vue'

import { useApi } from '@/composables/useApi'
import { useUploadQueue } from '@/composables/useResumableUpload'
import { useSettings } from '@/composables/useSettings'
import { applyMarkdown, type MarkdownAction } from '@/lib/markdownToolbar'

const emit = defineEmits<{ saved: [memo: unknown] }>()

const api = useApi()
const { items: uploads, add: addUploads } = useUploadQueue()
const { settings } = useSettings()

const content = ref('')
const visibility = ref<'PRIVATE' | 'PROTECTED' | 'PUBLIC'>('PRIVATE')
// Default the picker to the user's setting. immediate so it applies even when settings are
// ALREADY cached at mount (AppShell loads them first) — otherwise the watch never fires and the
// picker is stuck on the initial PRIVATE despite a PUBLIC default.
watch(settings, (s) => {
  if (s) visibility.value = s.default_visibility
}, { immediate: true })
const visibilityOptions = [
  { label: 'Private', value: 'PRIVATE', icon: 'pi pi-lock' },
  { label: 'Protected', value: 'PROTECTED', icon: 'pi pi-users' },
  { label: 'Public', value: 'PUBLIC', icon: 'pi pi-globe' },
]
// Custom display date — null means "now" (server default); set it to backdate a memo (parity).
const memoDate = ref<Date | null>(null)
const saving = ref(false)
// Block Post while media is still uploading — only finished uploads attach, so posting early
// would silently drop the in-flight ones (the thumbnails show their progress).
const uploading = computed(() => uploads.some((u) => u.status === 'queued' || u.status === 'uploading'))

const textarea = ref<HTMLTextAreaElement | null>(null)
const TOOLBAR: { icon: string; action: MarkdownAction; label: string }[] = [
  { icon: 'pi pi-bold', action: 'bold', label: 'Bold' },
  { icon: 'pi pi-italic', action: 'italic', label: 'Italic' },
  { icon: 'pi pi-code', action: 'code', label: 'Code' },
  { icon: 'pi pi-check-square', action: 'checkbox', label: 'Checkbox' },
  { icon: 'pi pi-link', action: 'link', label: 'Link' },
]

function runToolbar(action: MarkdownAction) {
  const el = textarea.value
  const start = el?.selectionStart ?? content.value.length
  const end = el?.selectionEnd ?? content.value.length
  const r = applyMarkdown(content.value, start, end, action)
  content.value = r.text
  nextTick(() => {
    el?.focus()
    el?.setSelectionRange(r.selectionStart, r.selectionEnd)
  })
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
    // Finalize-on-post: send the finished uploads' upload_uids; the server turns them into
    // attachments now. Uploads still in flight are not posted (and simply expire) — no orphans.
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
    uploads.length = 0
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
      rows="3"
      placeholder="Write a memo…  #tags, drop or paste images & video"
      class="w-full resize-y bg-transparent p-2 text-base outline-none"
      data-test="content"
    />

    <div v-if="uploads.length" class="flex flex-wrap gap-2" data-test="uploads">
      <div
        v-for="u in uploads"
        :key="u.id"
        class="relative h-20 w-20 overflow-hidden rounded-lg bg-surface-100 dark:bg-surface-800"
        :title="u.name"
      >
        <img v-if="u.preview" :src="u.preview" alt="" class="h-full w-full object-cover" />
        <span v-else class="flex h-full w-full items-center justify-center text-muted-color">
          <i :class="u.isVideo ? 'pi pi-video' : 'pi pi-file'" class="text-xl" aria-hidden="true" />
        </span>
        <!-- uploading: dim + percent; done: check; error: red -->
        <div
          v-if="u.status !== 'done'"
          class="absolute inset-0 flex items-center justify-center bg-black/45 text-xs font-medium text-white"
        >
          <span v-if="u.status === 'error'"><i class="pi pi-exclamation-triangle text-red-400" /></span>
          <span v-else>{{ u.progress }}%</span>
        </div>
        <span
          v-else
          class="absolute bottom-1 right-1 flex h-4 w-4 items-center justify-center rounded-full bg-primary text-[10px] text-white"
        >
          <i class="pi pi-check" style="font-size: 9px" aria-hidden="true" />
        </span>
      </div>
    </div>

    <!-- one slim action bar: formatting + media on the left, date / visibility / post on the right.
         flex-wrap so on a phone the right cluster drops to its own line instead of overflowing. -->
    <div
      class="flex flex-wrap items-center gap-1 gap-y-2 border-t border-surface-100 pt-2 dark:border-surface-800"
    >
      <Button
        v-for="t in TOOLBAR"
        :key="t.action"
        text
        rounded
        size="small"
        severity="secondary"
        :icon="t.icon"
        :aria-label="t.label"
        :data-test="`md-${t.action}`"
        @click="runToolbar(t.action)"
      />
      <label class="cursor-pointer rounded-full p-2 text-muted-color hover:text-color" title="Attach media">
        <input
          type="file"
          multiple
          accept="image/*,video/*"
          class="hidden"
          data-test="file-input"
          @change="onFiles(($event.target as HTMLInputElement).files)"
        />
        <i class="pi pi-image" aria-hidden="true" />
      </label>

      <div class="ml-auto flex items-center gap-2">
        <DatePicker
          v-model="memoDate"
          show-time
          hour-format="24"
          show-icon
          icon-display="input"
          size="small"
          placeholder="Now"
          date-format="M d,"
          class="w-40"
          input-class="text-sm text-muted-color"
          data-test="memo-date"
        />
        <Select
          v-model="visibility"
          :options="visibilityOptions"
          option-label="label"
          option-value="value"
          size="small"
          class="!border-none bg-transparent text-muted-color"
          data-test="visibility"
        >
          <template #value="{ value }">
            <span class="flex items-center gap-1.5 text-sm">
              <i :class="visibilityOptions.find((o) => o.value === value)?.icon" aria-hidden="true" />
              {{ visibilityOptions.find((o) => o.value === value)?.label }}
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
          icon="pi pi-send"
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
