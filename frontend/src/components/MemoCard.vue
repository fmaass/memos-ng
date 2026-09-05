<script setup lang="ts">
import Button from 'primevue/button'
import DatePicker from 'primevue/datepicker'
import Menu from 'primevue/menu'
import Popover from 'primevue/popover'
import Select from 'primevue/select'
import { useConfirm } from 'primevue/useconfirm'
import { computed, onUnmounted, ref, watch } from 'vue'

import MediaTile from '@/components/MediaTile.vue'
import type { components } from '@/api/schema'
import { useElementWidth } from '@/composables/useElementWidth'
import { type AttachPrintPayload, useMediaLightbox } from '@/composables/useMediaLightbox'
import { fetchPrintMasters, printMasterUploads } from '@/composables/usePrintMaster'
import { type UploadItem, useUploadQueue } from '@/composables/useResumableUpload'
import { attachmentUidsToClear } from '@/lib/attachments'
import { captureOrder, toWireInstant } from '@/lib/autoMemoDate'
import { captureBlockedMessage, pendingCaptureItems } from '@/lib/composer'
import {
  printBadgeLabel,
  printChipState,
  selectPrintMaster,
  toPrintMasterInfo,
} from '@/lib/printMaster'
import { justifyRows } from '@/lib/justified'
import { renderMemoMarkdown } from '@/lib/markdown'
import {
  justifiedTileSizes,
  mediaLayout,
  singleTileMaxWidthPx,
  tileSizes,
} from '@/lib/mediaLayout'
import { TASK_CHECKBOX_CLASS, toggleTask } from '@/lib/tasks'

type Memo = components['schemas']['MemoOut']
type Visibility = Memo['visibility']

const props = defineProps<{
  memo: Memo
  restorable?: boolean
  /**
   * The parent's PATCH for THIS memo is still in flight. The card cannot know that on its own —
   * the parent owns the request — and it is what releases the card's own claim, so a save cannot
   * be repeated by reopening the editor while the first one is still out (MEMNG-262).
   */
  editPending?: boolean
}>()
const emit = defineEmits<{
  restore: [uid: string]
  'task-toggle': [payload: { uid: string; content: string }]
  'toggle-reaction': [payload: { uid: string; emoji: string; add: boolean }]
  'delete-attachment': [uid: string]
  'delete-attachments': [uids: string[]] // bulk remove — deleted SEQUENTIALLY by the parent
  'delete-memo': [uid: string]
  retry: [uid: string] // a tile exhausted its in-place retries — ask the feed to re-mint URLs
  // Print masters: attach/replace (already confirmed here), remove (ditto), and retry a failed one.
  'attach-print': [payload: { uid: string; file: File; variantKey: string }]
  'remove-print': [payload: { uid: string; variantKey: string }]
  'retry-print': [uid: string]
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
const {
  items: editUploads,
  add: addEditUploads,
  remove: removeEditUpload,
  clear: clearEditUploads,
} = useUploadQueue()
const editFileInput = ref<HTMLInputElement | null>(null)
const editUploading = computed(() =>
  editUploads.some((u) => u.status === 'queued' || u.status === 'uploading'),
)
// An EDIT is always a manual date — it opens on the memo's own date and Auto is not offered (an
// attachment that is already saved carries no capture instant this browser can read). The capture
// decisions still have to SETTLE before Save, because the files added here are appended in capture
// order among themselves, and that order is saved (MEMNG-262).
const editZone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
const editTiles = computed<UploadItem[]>(() => [...editUploads])
// The tiles are SHOWN in the order they will be SAVED — an edit that displayed pick order while
// submitting capture order would be telling the person something untrue about their own memo.
const editOrdered = computed(() => captureOrder(editTiles.value, editZone))
const editCapturePending = computed(() => pendingCaptureItems(editTiles.value))
const editCaptureMessage = computed(() =>
  editCapturePending.value.length ? captureBlockedMessage(editCapturePending.value) : '',
)
function onEditFiles(files: FileList | null) {
  if (files?.length) addEditUploads(Array.from(files))
}
function startEdit() {
  editContent.value = props.memo.content
  editVisibility.value = props.memo.visibility
  editDate.value = new Date(props.memo.created_at)
  clearEditUploads() // NOT `editUploads.length = 0` — that strands preview object URLs + File refs
  editing.value = true
}
// Claimed synchronously (a double click lands both handlers in one tick), and held until the
// PATCH COMPLETES — which the parent reports through `editPending`. Reopening the editor while it
// is out therefore shows the save in progress instead of offering a second one.
const submittingEdit = ref(false)
const editBusy = computed(() => submittingEdit.value || props.editPending === true)
watch(
  () => props.editPending,
  (now, before) => {
    // Landed or failed — either way the request is over and this card may be edited again.
    if (before && !now) submittingEdit.value = false
  },
)

function saveEdit() {
  if (editBusy.value) return
  if (editCapturePending.value.length > 0) return // the sentence is already on screen
  submittingEdit.value = true
  // Snapshot what is being submitted, then release exactly those tiles — a file attached in the
  // same moment is not part of this edit and must not vanish with it. Everything staged NOW is
  // part of it, a failed upload included; only what its uid reaches the wire.
  const staged = [...editOrdered.value]
  const submitted = staged.filter((u) => u.status === 'done' && u.uploadUid)
  emit('edit', {
    uid: props.memo.uid,
    content: editContent.value,
    visibility: editVisibility.value,
    // Seconds precision, the same serializer the composer uses — the wire carries whole seconds
    // everywhere in this feature, and a millisecond spelling here would be a second dialect.
    created_at: toWireInstant(editDate.value ?? new Date(props.memo.created_at)),
    // In capture order among themselves; the server appends them after the memo's existing
    // attachments, whose saved order this edit never re-sorts.
    upload_uids: submitted.map((u) => u.uploadUid as string),
  })
  for (const item of staged) removeEditUpload(item.id)
  editing.value = false
  // The claim is NOT released here, nor by reopening or cancelling: only the parent knows when
  // its PATCH is over, and it says so through `editPending`.
}
// Cancel discards the staged uploads — they were never going to be attached (startEdit would drop
// them on the next open anyway), so release them now instead of stranding their object URLs.
function cancelEdit() {
  clearEditUploads()
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

// The memo-level print badge is a COUNT ("🖨 1 of 3"): the old flag could not say WHICH photo it
// meant. Per-photo state lives on the tiles themselves (printChipFor).
const printBadge = computed(() => printBadgeLabel(props.memo.attachments ?? []))

// Live print-master uploads, keyed by attachment uid — shared with the view that runs the mutation
// (usePrintMaster), so progress and failure survive the feed refetch that replaces this memo object.
const printUploads = printMasterUploads()
function printChipFor(attachment: components['schemas']['AttachmentRef']) {
  return printChipState(attachment, printUploads.get(attachment.uid))
}

// The media area's shape follows the attachment count (see mediaLayout): a lone photo keeps its own
// ratio at content width, two crop into a uniform two-up, three or more pack into justified rows.
const attachments = computed(() => props.memo.attachments ?? [])
const layout = computed(() => mediaLayout(attachments.value))

// Which of the tiles' srcset candidates to load: the width the box renders at. A pair tile is a
// fixed share of the column, but a lone photo is only as wide as the height cap leaves it (the same
// limit singleStyle puts on the box below), so its hint is per-photo. Justified tiles know their own.
const singleSizes = computed(() =>
  tileSizes(
    'single',
    singleTileMaxWidthPx(attachments.value[0]?.width, attachments.value[0]?.height),
  ),
)
const pairSizes = tileSizes('pair')

// Capping the HEIGHT of a lone photo has to narrow its box, otherwise a full-width portrait would
// have to crop to obey the cap. The box runs the column width up to (cap × ratio); its
// aspect-ratio then keeps the height at or under the cap.
const singleStyle = computed(() => {
  const l = layout.value
  if (l.mode !== 'single') return undefined
  return { width: '100%', maxWidth: `${((l.width / l.height) * l.maxHeightRem).toFixed(3)}rem` }
})

// Justified rows are laid out in px, so they need the rendered width of the media column.
const mediaRoot = ref<HTMLElement | null>(null)
const mediaWidth = useElementWidth(mediaRoot)
const justifiedRows = computed(() =>
  layout.value.mode === 'justified'
    ? justifyRows(attachments.value, mediaWidth.value, layout.value.targetHeight, layout.value.gap)
    : [],
)

// Full-screen viewer (PhotoSwipe) over ALL of the memo's media — swipe/zoom images, play videos.
const lightbox = useMediaLightbox()
function openLightbox(uid: string) {
  const atts = props.memo.attachments ?? []
  const i = atts.findIndex((a) => a.uid === uid)
  if (i >= 0)
    lightbox.open(atts, i, {
      onDelete: onDeleteAttachment,
      onAttachPrint,
      onRemovePrint,
      loadPrintMaster,
    })
}
onUnmounted(() => lightbox.destroy())

// The viewer holds the master it fetched; the feed refetching (an upload landed, a master was
// removed) is the signal that what it holds is stale.
watch(
  () => props.memo.attachments,
  () => lightbox.refreshPrintMaster(),
)

/** The one master the print row describes — the same row the backend derives the photo book from. */
async function loadPrintMaster(uid: string) {
  return toPrintMasterInfo(selectPrintMaster(await fetchPrintMasters(uid)))
}

// Replacing DESTROYS the master that is there, so it is confirmed — after the file is picked, so
// the confirmation can name what is about to overwrite it. A first attach destroys nothing.
function onAttachPrint({ uid, file, variantKey, replacing }: AttachPrintPayload) {
  if (!replacing) {
    emit('attach-print', { uid, file, variantKey })
    return
  }
  confirm.require({
    header: 'Replace print master',
    message: `Replace the print master with “${file.name}”? The current one is deleted.`,
    icon: 'pi pi-exclamation-triangle',
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    acceptProps: { label: 'Replace' },
    accept: () => emit('attach-print', { uid, file, variantKey }),
  })
}

function onRemovePrint({ uid, variantKey }: { uid: string; variantKey: string }) {
  confirm.require({
    header: 'Remove print master',
    message: 'Remove this photo’s print master? The photo itself stays.',
    icon: 'pi pi-exclamation-triangle',
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    acceptProps: { label: 'Remove', severity: 'danger' },
    accept: () => emit('remove-print', { uid, variantKey }),
  })
}
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
// The DOM index is only meaningful because toggleTask enumerates source tasks with the renderer's
// own parse — same checkboxes, same order (TASK_CHECKBOX_CLASS is the shared anchor).
function onContentClick(event: MouseEvent) {
  const target = event.target
  if (!(target instanceof HTMLInputElement) || !target.classList.contains(TASK_CHECKBOX_CLASS))
    return
  event.preventDefault()
  const root = event.currentTarget as HTMLElement
  const index = Array.from(root.querySelectorAll(`input.${TASK_CHECKBOX_CLASS}`)).indexOf(target)
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
          v-for="u in editOrdered"
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
          <!-- the same two capture states the composer shows: still being read, and given up on -->
          <span
            v-else-if="u.captureUnknown"
            class="absolute inset-x-0 top-0 bg-black/55 px-1 py-0.5 text-[9px] text-white"
            data-test="edit-capture-unknown"
            >date unknown</span
          >
          <div
            v-else-if="u.captureState === 'pending'"
            class="absolute inset-0 flex items-center justify-center bg-black/35 text-white"
            data-test="edit-capture-item-pending"
            :aria-label="`Reading the date of ${u.name}`"
          >
            <i class="pi pi-spin pi-spinner text-xs" aria-hidden="true" />
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
      <p v-if="editCaptureMessage" class="text-sm text-muted-color" data-test="edit-capture-pending">
        {{ editCaptureMessage }}
      </p>
      <p v-else-if="editBusy" class="text-sm text-muted-color" data-test="edit-in-flight">
        Saving this memo… reopening it does not send it twice.
      </p>
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
          <Button text size="small" label="Cancel" data-test="edit-cancel" @click="cancelEdit" />
          <Button
            size="small"
            :label="editBusy ? 'Saving…' : editUploading ? 'Uploading…' : 'Save'"
            icon="pi pi-check"
            :disabled="editUploading || editBusy || editCapturePending.length > 0"
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

    <!-- one photo: content width, its own ratio, height-capped (no crop) -->
    <div v-if="layout.mode === 'single'" :style="singleStyle" data-test="media-single">
      <MediaTile
        :att="attachments[0]"
        :width="layout.width"
        :height="layout.height"
        :sizes="singleSizes"
        :print-chip="printChipFor(attachments[0])"
        @open="openLightbox"
        @retry="emit('retry', $event)"
        @print-retry="emit('retry-print', $event)"
      />
    </div>

    <!-- two photos: uniform boxes side by side, each cropping to the shared ratio -->
    <div v-else-if="layout.mode === 'pair'" class="grid grid-cols-2 gap-2" data-test="media-grid">
      <MediaTile
        v-for="a in attachments"
        :key="a.uid"
        :att="a"
        :width="layout.width"
        :height="layout.height"
        :sizes="pairSizes"
        :print-chip="printChipFor(a)"
        @open="openLightbox"
        @retry="emit('retry', $event)"
        @print-retry="emit('retry-print', $event)"
      />
    </div>

    <!-- three or more: justified rows, every photo at its own ratio (rows fill the column exactly) -->
    <div
      v-else-if="layout.mode === 'justified'"
      ref="mediaRoot"
      class="flex flex-col"
      :style="{ gap: layout.gap + 'px' }"
      data-test="media-justified"
    >
      <div
        v-for="(row, ri) in justifiedRows"
        :key="ri"
        class="flex"
        :style="{ gap: layout.gap + 'px', height: row.height + 'px' }"
      >
        <MediaTile
          v-for="li in row.items"
          :key="li.item.uid"
          :att="li.item"
          :width="li.item.width"
          :height="li.item.height"
          :sizes="justifiedTileSizes(li.width)"
          :print-chip="printChipFor(li.item)"
          :style="{ width: li.width + 'px', height: li.height + 'px' }"
          @open="openLightbox"
          @retry="emit('retry', $event)"
          @print-retry="emit('retry-print', $event)"
        />
      </div>
    </div>

    <div v-if="memo.tags.length || printBadge" class="flex flex-wrap items-center gap-1">
      <span
        v-for="t in memo.tags"
        :key="t"
        class="text-xs px-2 py-0.5 rounded-full bg-primary-50 dark:bg-primary-400/10 text-primary"
        data-test="tag-chip"
        >#{{ t }}</span
      >
      <span
        v-if="printBadge"
        class="text-xs px-2 py-0.5 rounded-full bg-surface-200 dark:bg-surface-700 text-muted-color"
        data-test="print-badge"
        title="Photos in this memo that have a print master"
        :aria-label="`${printBadge.replace('🖨', 'Print masters:')}`"
        >{{ printBadge }}</span
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
