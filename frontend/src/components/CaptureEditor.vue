<script setup lang="ts">
import Button from 'primevue/button'
import DatePicker from 'primevue/datepicker'
import Select from 'primevue/select'
import { nextTick, ref, watch } from 'vue'

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
// Default the picker to the user's setting once it loads (prod parity).
watch(settings, (s) => {
  if (s) visibility.value = s.default_visibility
})
const visibilityOptions = [
  { label: 'Private', value: 'PRIVATE', icon: 'pi pi-lock' },
  { label: 'Protected', value: 'PROTECTED', icon: 'pi pi-users' },
  { label: 'Public', value: 'PUBLIC', icon: 'pi pi-globe' },
]
// Custom display date — null means "now" (server default); set it to backdate a memo (parity).
const memoDate = ref<Date | null>(null)
const saving = ref(false)

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
    // Link any uploads that have already finished; a memo posts without waiting on the rest.
    const attachment_uids = uploads
      .filter((u) => u.status === 'done' && u.attachmentUid)
      .map((u) => u.attachmentUid!)
    const body: Record<string, unknown> = {
      content: content.value,
      visibility: visibility.value,
      pinned: false,
      attachment_uids,
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

    <div v-if="uploads.length" class="flex flex-col gap-1" data-test="uploads">
      <div v-for="u in uploads" :key="u.id" class="flex items-center gap-2 text-sm">
        <span class="truncate">{{ u.name }}</span>
        <span class="text-muted-color">{{ u.status }} {{ u.progress }}%</span>
      </div>
    </div>

    <!-- one slim action bar: formatting + media on the left, date / visibility / post on the right -->
    <div class="flex items-center gap-1 border-t border-surface-100 pt-2 dark:border-surface-800">
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
          label="Post"
          icon="pi pi-send"
          size="small"
          :loading="saving"
          :disabled="!content.trim()"
          data-test="save"
          @click="save"
        />
      </div>
    </div>
  </div>
</template>
