<script setup lang="ts">
import AutoComplete, { type AutoCompleteCompleteEvent } from 'primevue/autocomplete'
import Button from 'primevue/button'
import SelectButton from 'primevue/selectbutton'
import { nextTick, ref } from 'vue'

import { useApi } from '@/composables/useApi'
import { useUploadQueue } from '@/composables/useResumableUpload'
import { applyMarkdown, type MarkdownAction } from '@/lib/markdownToolbar'

const emit = defineEmits<{ saved: [memo: unknown] }>()

const api = useApi()
const { items: uploads, add: addUploads } = useUploadQueue()

const content = ref('')
const visibility = ref<'PRIVATE' | 'PROTECTED' | 'PUBLIC'>('PRIVATE')
const visibilityOptions = [
  { label: 'Private', value: 'PRIVATE' },
  { label: 'Protected', value: 'PROTECTED' },
  { label: 'Public', value: 'PUBLIC' },
]
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

// #tag autocomplete from /api/tags (server returns {name,count}).
const tagSuggestions = ref<string[]>([])
const tagModel = ref<string[]>([])
async function completeTags(event: AutoCompleteCompleteEvent) {
  const { data } = await api.GET('/api/tags')
  const q = event.query.toLowerCase()
  tagSuggestions.value = (data ?? [])
    .map((t) => t.name)
    .filter((name) => name.toLowerCase().includes(q))
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
    const { data, error } = await api.POST('/api/memos', {
      body: { content: content.value, visibility: visibility.value, pinned: false, attachment_uids },
    })
    if (error) throw error
    emit('saved', data)
    content.value = ''
    tagModel.value = []
    uploads.length = 0
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div
    class="rounded-xl border border-surface-200 dark:border-surface-700 p-3 flex flex-col gap-2"
    data-test="capture-editor"
    @paste="onPaste"
    @dragover.prevent
    @drop.prevent="onFiles(($event as DragEvent).dataTransfer?.files ?? null)"
  >
    <div class="flex gap-1">
      <Button
        v-for="t in TOOLBAR"
        :key="t.action"
        text
        rounded
        size="small"
        :icon="t.icon"
        :aria-label="t.label"
        :data-test="`md-${t.action}`"
        @click="runToolbar(t.action)"
      />
    </div>

    <textarea
      ref="textarea"
      v-model="content"
      rows="3"
      placeholder="Write a memo… (#tags, drop or paste images & video)"
      class="w-full resize-y bg-transparent outline-none p-2"
      data-test="content"
    />

    <AutoComplete
      v-model="tagModel"
      multiple
      :suggestions="tagSuggestions"
      placeholder="#tags"
      data-test="tags"
      @complete="completeTags"
    />

    <div v-if="uploads.length" class="flex flex-col gap-1" data-test="uploads">
      <div v-for="u in uploads" :key="u.id" class="text-sm flex items-center gap-2">
        <span class="truncate">{{ u.name }}</span>
        <span class="text-muted-color">{{ u.status }} {{ u.progress }}%</span>
      </div>
    </div>

    <div class="flex items-center justify-between">
      <SelectButton
        v-model="visibility"
        :options="visibilityOptions"
        option-label="label"
        option-value="value"
        :allow-empty="false"
        data-test="visibility"
      />
      <div class="flex items-center gap-2">
        <label class="cursor-pointer">
          <input
            type="file"
            multiple
            accept="image/*,video/*"
            class="hidden"
            data-test="file-input"
            @change="onFiles(($event.target as HTMLInputElement).files)"
          />
          <span class="pi pi-paperclip p-2" aria-label="Attach" />
        </label>
        <Button
          label="Save"
          icon="pi pi-send"
          :loading="saving"
          :disabled="!content.trim()"
          data-test="save"
          @click="save"
        />
      </div>
    </div>
  </div>
</template>
