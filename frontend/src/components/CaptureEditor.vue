<script setup lang="ts">
import Button from 'primevue/button'
import DatePicker from 'primevue/datepicker'
import Menu from 'primevue/menu'
import Select from 'primevue/select'
import { useToast } from 'primevue/usetoast'
import { computed, nextTick, ref, watch } from 'vue'

import { useApi } from '@/composables/useApi'
import { type UploadItem, useUploadQueue } from '@/composables/useResumableUpload'
import { useSettings } from '@/composables/useSettings'
import { autoMemoDate, captureOrder, toWireInstant } from '@/lib/autoMemoDate'
import {
  canPost,
  captureBlockedMessage,
  composerKeyAction,
  formatMemoDate,
  pendingCaptureItems,
  uploadsUnlinkableMessage,
} from '@/lib/composer'
import { applyMarkdown, type MarkdownAction } from '@/lib/markdownToolbar'

const emit = defineEmits<{ saved: [memo: unknown] }>()

const api = useApi()
const toast = useToast()
// No `clear`: a post releases exactly the tiles it submitted, one by one, so a file attached
// while it was in flight survives it (see save()).
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

// The memo's date has two modes (MEMNG-262). AUTO — the default for a new memo — is the EARLIEST
// capture instant among the attached media, recomputed on every add, remove, retry and late
// inspection; MANUAL is a date the person picked, and picking one is what switches the mode. The
// zone is the browser's: it is what resolves an EXIF time that carries no offset, and what the
// span hint counts calendar days in.
const dateMode = ref<'auto' | 'manual'>('auto')
const memoDate = ref<Date | null>(null)
const showDate = ref(false)
const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
// A plain snapshot of the reactive queue — the pure functions take arrays, not reactive proxies.
const tiles = computed<UploadItem[]>(() => [...uploads])
const auto = computed(() => autoMemoDate(tiles.value, zone))
// Capture-ordered tiles: what the composer shows IS what it posts, and what the server saves.
const orderedUploads = computed(() => captureOrder(tiles.value, zone))
const pendingCapture = computed(() => pendingCaptureItems(tiles.value))
const autoLabel = computed(() => {
  const { instant, approximate, span } = auto.value
  const head = instant ? `Auto · ${approximate ? '≈ ' : ''}${formatMemoDate(instant)}` : 'Auto · now'
  return span > 0 ? `${head} · spans ${span} days` : head
})
function useAutoDate() {
  dateMode.value = 'auto'
  memoDate.value = null
  showDate.value = false
}
function onManualDate(picked: Date | null) {
  memoDate.value = picked
  dateMode.value = picked ? 'manual' : 'auto'
}
const saving = ref(false)
const uploading = computed(() =>
  uploads.some((u) => u.status === 'queued' || u.status === 'uploading'),
)
// Postable with text OR finished media (a photo-only memo is valid, like old memos).
const hasContent = computed(() => canPost(content.value, uploads))

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

// Claimed SYNCHRONOUSLY, before the first await of a post — `saving` is a ref the template reads,
// and two clicks in one tick would both pass a check on it. One post at a time: the second
// invocation is refused rather than joined, because a second POST is a second MEMO.
let posting = false

async function save() {
  if (posting) return
  if (!hasContent.value) return
  // Settle before posting, REGARDLESS of the date mode: the tile ORDER depends on the capture
  // decisions as much as the Auto date does, and both are frozen by this post. Refused with one
  // sentence naming the file, never silently dropped or silently posted out of order.
  if (pendingCapture.value.length > 0) {
    toast.add({
      severity: 'warn',
      summary: 'One moment',
      detail: captureBlockedMessage(pendingCapture.value),
      life: 6000,
    })
    return
  }
  posting = true
  saving.value = true
  try {
    // Finalize-on-post: send finished uploads' upload_uids; the server turns them into attachments.
    // In CAPTURE ORDER — the server saves that order as each attachment's position.
    const upload_uids = orderedUploads.value
      .filter((u) => u.status === 'done' && u.uploadUid)
      .map((u) => u.uploadUid!)
    // What this post CONTAINS, snapshotted before the request goes out. The composer stays live
    // while it is in flight — someone can keep typing and keep attaching — and only what was
    // submitted may be cleared when it comes back. Every tile PRESENT at this moment belongs to
    // the submitted draft, including one whose upload failed (it was offered to this memo and
    // refused); a tile added later belongs to the next one.
    const submittedContent = content.value
    const submittedIds = uploads.map((u) => u.id)
    const submittedMode = dateMode.value
    const submittedDate = memoDate.value
    const body: Record<string, unknown> = {
      content: submittedContent,
      visibility: visibility.value,
      pinned: false,
      upload_uids,
    }
    // Manual sends what was picked. Auto sends the earliest capture instant it resolved — and
    // sends NOTHING when it resolved none, which is what makes "Auto · now" the server's now.
    const dated = dateMode.value === 'manual' ? memoDate.value : auto.value.instant
    if (dated) body.created_at = toWireInstant(dated)
    const { data, error } = await api.POST('/api/memos', { body: body as never })
    if (error) {
      // A composer left open past the upload expiry still shows its tiles as `done`, but the
      // sessions behind them are reaped and the server now refuses the whole post (MEMNG-113)
      // rather than saving the text without the photos. Say which files were lost — silently
      // failing here is what the strict refusal exists to prevent.
      const lost = uploadsUnlinkableMessage(error, uploads)
      if (lost) {
        toast.add({ severity: 'error', summary: 'Memo not posted', detail: lost, life: 10000 })
        return
      }
      throw error
    }
    emit('saved', data)
    // Clear the SUBMITTED draft, not whatever the composer holds now. A file attached while the
    // request was in flight belongs to the next memo and stays; text typed meanwhile stays too.
    // `removeUpload` per id, never `clearUploads()`, so the survivors keep their previews and
    // File references and the posted ones still release theirs.
    for (const id of submittedIds) removeUpload(id)
    // The DATE is part of the draft too: a date picked while the request was in flight belongs to
    // the next memo, and resetting to Auto because the text and the tiles happen to look untouched
    // would throw it away.
    const untouched =
      content.value === submittedContent &&
      uploads.length === 0 &&
      dateMode.value === submittedMode &&
      memoDate.value === submittedDate
    if (untouched) {
      content.value = ''
      useAutoDate() // a clean composer starts the next memo on Auto again
    }
    nextTick(autosize)
  } finally {
    posting = false
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

    <!-- the memo's date: the Auto chip by default, a manual chip once a date is picked, and the
         picker in between (the ⋯ menu opens it) -->
    <div
      v-if="dateMode === 'manual' && memoDate"
      class="flex items-center gap-2 self-start rounded-full bg-surface-100 px-3 py-1 text-sm text-muted-color dark:bg-surface-800"
      data-test="date-chip"
    >
      <i class="pi pi-clock text-xs" aria-hidden="true" />
      <span>{{ formatMemoDate(memoDate) }}</span>
      <button
        class="pi pi-times text-xs hover:text-color"
        aria-label="Use the automatic date"
        data-test="date-auto"
        @click="useAutoDate"
      />
    </div>
    <DatePicker
      v-else-if="showDate"
      :model-value="memoDate"
      show-time
      hour-format="24"
      size="small"
      placeholder="Pick date & time"
      class="w-full sm:w-64"
      aria-label="Memo date"
      data-test="memo-date"
      @update:model-value="onManualDate($event as Date | null)"
    />
    <div
      v-else
      class="flex items-center gap-2 self-start rounded-full bg-surface-100 px-3 py-1 text-sm text-muted-color dark:bg-surface-800"
      data-test="auto-date-chip"
    >
      <i class="pi pi-sparkles text-xs" aria-hidden="true" />
      <span>{{ autoLabel }}</span>
    </div>

    <div v-if="uploads.length" class="flex flex-wrap gap-2" data-test="uploads">
      <div
        v-for="u in orderedUploads"
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
        <!-- the inspection gave up on this file: the memo will not be dated by it, and saying so
             on the tile is the difference between an unknown date and a silently wrong one -->
        <span
          v-else-if="u.captureUnknown"
          class="absolute inset-x-0 top-0 bg-black/55 px-1 py-0.5 text-[10px] text-white"
          data-test="capture-unknown"
          >date unknown</span
        >
        <!-- uploaded, but its capture decision has not come back yet: this is the tile the refusal
             names, so it has to be visible as the one being waited for -->
        <div
          v-else-if="u.captureState === 'pending'"
          class="absolute inset-0 flex items-center justify-center bg-black/35 text-white"
          data-test="capture-pending"
          :aria-label="`Reading the date of ${u.name}`"
        >
          <i class="pi pi-spin pi-spinner text-sm" aria-hidden="true" />
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
          :disabled="!hasContent || uploading"
          data-test="save"
          @click="save"
        />
      </div>
    </div>
  </div>
</template>
