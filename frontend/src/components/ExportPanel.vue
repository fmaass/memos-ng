<script setup lang="ts">
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import SelectButton from 'primevue/selectbutton'
import ToggleSwitch from 'primevue/toggleswitch'
import { computed, ref, watch } from 'vue'

import { useExportSettings } from '@/composables/useExportSettings'
import {
  type ExportQuery,
  MAIL_LONG_EDGE_OPTIONS,
  MAIL_QUALITY_RANGE,
  METADATA_POLICY_OPTIONS,
  type MailProfileDraft,
  PRINT_LONG_EDGE_OPTIONS,
  PRINT_QUALITY_RANGE,
  type PrintProfileDraft,
  SUBSAMPLING_OPTIONS,
  defaultExportQuery,
  describePrintProfile,
  dirtyMailFields,
  dirtyPrintFields,
  exportCurlCommand,
  mailDraftFromSettings,
  printDraftFromSettings,
  validateExportQuery,
  validateMailDraft,
  validatePrintDraft,
} from '@/lib/exportSettings'

const {
  settings,
  savingPrint,
  savingMail,
  writerBusy,
  printError,
  mailError,
  notice,
  clearNotice,
  savePrint,
  saveMail,
} = useExportSettings()

// Each draft is seeded ONCE, when the settings first arrive, and re-seeded only by its own Save or
// Discard. A background refetch must not silently overwrite what the admin is editing — and a save
// of the OTHER profile must not either, which is why they are two refs and not one.
const printDraft = ref<PrintProfileDraft | null>(null)
const mailDraft = ref<MailProfileDraft | null>(null)
watch(
  settings,
  (value) => {
    if (!value) return
    if (printDraft.value === null) printDraft.value = printDraftFromSettings(value)
    if (mailDraft.value === null) mailDraft.value = mailDraftFromSettings(value)
  },
  { immediate: true },
)

const printErrors = computed(() => (printDraft.value ? validatePrintDraft(printDraft.value) : {}))
const mailErrors = computed(() => (mailDraft.value ? validateMailDraft(mailDraft.value) : {}))
const printDirty = computed(() =>
  printDraft.value && settings.value
    ? dirtyPrintFields(printDraft.value, settings.value).length > 0
    : false,
)
const mailDirty = computed(() =>
  mailDraft.value && settings.value
    ? dirtyMailFields(mailDraft.value, settings.value).length > 0
    : false,
)
const canSavePrint = computed(
  () => printDirty.value && Object.keys(printErrors.value).length === 0 && !writerBusy.value,
)
const canSaveMail = computed(
  () => mailDirty.value && Object.keys(mailErrors.value).length === 0 && !writerBusy.value,
)

// NaN is deliberate: an emptied or garbled quality field must fail validation rather than quietly
// become 0, which would be a legal-looking setting that renders unusable JPEGs.
function setPrintQuality(value: string | undefined) {
  if (!printDraft.value) return
  const raw = (value ?? '').trim()
  printDraft.value.print_jpeg_quality = raw === '' ? NaN : Number(raw)
}
function setMailQuality(value: string | undefined) {
  if (!mailDraft.value) return
  const raw = (value ?? '').trim()
  mailDraft.value.mail_jpeg_quality = raw === '' ? NaN : Number(raw)
}

function discardPrint() {
  if (settings.value) printDraft.value = printDraftFromSettings(settings.value)
  printError.value = null
}
function discardMail() {
  if (settings.value) mailDraft.value = mailDraftFromSettings(settings.value)
  mailError.value = null
}

async function onSavePrint() {
  if (!printDraft.value || !canSavePrint.value) return
  if (await savePrint(printDraft.value)) discardPrint() // re-seed from what the server stored
}
async function onSaveMail() {
  if (!mailDraft.value || !canSaveMail.value) return
  if (await saveMail(mailDraft.value)) discardMail()
}

// --- the read-only "how to query" block ---
const query = ref<ExportQuery>(defaultExportQuery(new Date()))
const queryErrors = computed(() => validateExportQuery(query.value))
const origin = computed(() =>
  typeof window === 'undefined' ? 'https://memos.example' : window.location.origin,
)
const curl = computed(() => exportCurlCommand(query.value, origin.value))
const profileLine = computed(() => (settings.value ? describePrintProfile(settings.value) : ''))

const copied = ref(false)
/** Best effort, and honest when it is not available: a browser without the async clipboard (an
 * insecure origin, an older engine) or a page denied permission leaves the command on screen to
 * select, and claims nothing. The absent-clipboard branch is checked BEFORE the await and not
 * through `?.`, because `navigator.clipboard?.writeText(…)` resolves `undefined` rather than
 * throwing — awaiting it succeeds, and "Copied." would be a lie about an empty clipboard. */
async function copyCommand() {
  const clipboard = navigator.clipboard as Clipboard | undefined
  if (!clipboard) {
    copied.value = false
    return
  }
  try {
    await clipboard.writeText(curl.value)
    copied.value = true
  } catch {
    copied.value = false
  }
}
</script>

<template>
  <div class="flex flex-col gap-8" data-test="export-panel">
    <div v-if="!settings" class="text-sm text-muted-color" data-test="export-loading">
      Loading the export settings…
    </div>

    <template v-else>
      <p
        v-if="notice"
        class="text-sm text-green-600"
        role="status"
        aria-live="polite"
        data-test="export-notice"
        @click="clearNotice"
      >
        {{ notice }}
      </p>

      <!-- Print profile -->
      <section v-if="printDraft" class="flex flex-col gap-3" data-test="export-print">
        <div>
          <h2 class="text-lg font-medium">Print profile</h2>
          <p class="text-sm text-muted-color">
            The photobook export, and the full-size copies the digest hosts.
          </p>
        </div>

        <div class="flex flex-col gap-1 text-sm">
          <span id="print-long-edge-label">Long edge</span>
          <SelectButton
            v-model="printDraft.print_long_edge"
            :options="PRINT_LONG_EDGE_OPTIONS"
            option-label="label"
            option-value="value"
            :allow-empty="false"
            aria-labelledby="print-long-edge-label"
            data-test="export-print-long-edge"
          />
          <span
            v-if="printErrors.print_long_edge"
            class="text-xs text-red-600"
            role="alert"
            data-test="export-error-print_long_edge"
          >
            {{ printErrors.print_long_edge }}
          </span>
        </div>

        <label class="flex flex-col gap-1 text-sm max-w-48">
          JPEG quality ({{ PRINT_QUALITY_RANGE[0] }}–{{ PRINT_QUALITY_RANGE[1] }})
          <InputText
            type="number"
            :model-value="String(printDraft.print_jpeg_quality)"
            :invalid="!!printErrors.print_jpeg_quality"
            data-test="export-print-quality"
            @update:model-value="setPrintQuality"
          />
          <span
            v-if="printErrors.print_jpeg_quality"
            class="text-xs text-red-600"
            role="alert"
            data-test="export-error-print_jpeg_quality"
          >
            {{ printErrors.print_jpeg_quality }}
          </span>
        </label>

        <div class="flex flex-col gap-1 text-sm">
          <span id="print-metadata-label">Metadata</span>
          <SelectButton
            v-model="printDraft.print_metadata_policy"
            :options="METADATA_POLICY_OPTIONS"
            option-label="label"
            option-value="value"
            :allow-empty="false"
            aria-labelledby="print-metadata-label"
            data-test="export-print-metadata"
          />
          <span class="text-xs text-muted-color">
            Stripping always keeps the colour profile and bakes the orientation.
          </span>
          <span
            v-if="printErrors.print_metadata_policy"
            class="text-xs text-red-600"
            role="alert"
            data-test="export-error-print_metadata_policy"
          >
            {{ printErrors.print_metadata_policy }}
          </span>
        </div>

        <div class="flex items-center gap-3">
          <ToggleSwitch
            v-model="printDraft.print_grayscale_single_channel"
            aria-label="Encode neutral monochrome as a single channel"
            data-test="export-print-grayscale"
          />
          <span class="text-sm">
            Encode neutral monochrome as a single channel
            <span class="block text-xs text-muted-color">
              Same quality, no chroma planes. Toned images stay in colour.
            </span>
          </span>
        </div>

        <div
          v-if="printDirty"
          class="flex items-center gap-3 border-t border-surface-200 pt-3 dark:border-surface-700"
          role="group"
          aria-label="Unsaved print profile"
          data-test="export-print-save-bar"
        >
          <Button
            label="Save"
            :disabled="!canSavePrint"
            :loading="savingPrint"
            data-test="export-print-save"
            @click="onSavePrint"
          />
          <Button label="Discard" text data-test="export-print-discard" @click="discardPrint" />
          <span
            v-if="printError"
            class="text-sm text-red-600"
            role="alert"
            data-test="export-print-error"
          >
            {{ printError }}
          </span>
        </div>
      </section>

      <!-- Mail profile -->
      <section v-if="mailDraft" class="flex flex-col gap-3" data-test="export-mail">
        <div>
          <h2 class="text-lg font-medium">Mail profile</h2>
          <p class="text-sm text-muted-color">The images embedded in the digest e-mail itself.</p>
        </div>

        <div class="flex flex-col gap-1 text-sm">
          <span id="mail-long-edge-label">Long edge</span>
          <SelectButton
            v-model="mailDraft.mail_long_edge"
            :options="MAIL_LONG_EDGE_OPTIONS"
            option-label="label"
            option-value="value"
            :allow-empty="false"
            aria-labelledby="mail-long-edge-label"
            data-test="export-mail-long-edge"
          />
          <span
            v-if="mailErrors.mail_long_edge"
            class="text-xs text-red-600"
            role="alert"
            data-test="export-error-mail_long_edge"
          >
            {{ mailErrors.mail_long_edge }}
          </span>
        </div>

        <label class="flex flex-col gap-1 text-sm max-w-48">
          JPEG quality ({{ MAIL_QUALITY_RANGE[0] }}–{{ MAIL_QUALITY_RANGE[1] }})
          <InputText
            type="number"
            :model-value="String(mailDraft.mail_jpeg_quality)"
            :invalid="!!mailErrors.mail_jpeg_quality"
            data-test="export-mail-quality"
            @update:model-value="setMailQuality"
          />
          <span
            v-if="mailErrors.mail_jpeg_quality"
            class="text-xs text-red-600"
            role="alert"
            data-test="export-error-mail_jpeg_quality"
          >
            {{ mailErrors.mail_jpeg_quality }}
          </span>
        </label>

        <div class="flex flex-col gap-1 text-sm">
          <span id="mail-subsampling-label">Chroma subsampling</span>
          <SelectButton
            v-model="mailDraft.mail_subsampling"
            :options="SUBSAMPLING_OPTIONS"
            option-label="label"
            option-value="value"
            :allow-empty="false"
            aria-labelledby="mail-subsampling-label"
            data-test="export-mail-subsampling"
          />
          <span
            v-if="mailErrors.mail_subsampling"
            class="text-xs text-red-600"
            role="alert"
            data-test="export-error-mail_subsampling"
          >
            {{ mailErrors.mail_subsampling }}
          </span>
        </div>

        <div class="flex items-center gap-3">
          <ToggleSwitch
            v-model="mailDraft.mail_progressive"
            aria-label="Progressive JPEG"
            data-test="export-mail-progressive"
          />
          <span class="text-sm">
            Progressive JPEG
            <span class="block text-xs text-muted-color">
              Off by default — older Outlook renders baseline only.
            </span>
          </span>
        </div>

        <div
          v-if="mailDirty"
          class="flex items-center gap-3 border-t border-surface-200 pt-3 dark:border-surface-700"
          role="group"
          aria-label="Unsaved mail profile"
          data-test="export-mail-save-bar"
        >
          <Button
            label="Save"
            :disabled="!canSaveMail"
            :loading="savingMail"
            data-test="export-mail-save"
            @click="onSaveMail"
          />
          <Button label="Discard" text data-test="export-mail-discard" @click="discardMail" />
          <span
            v-if="mailError"
            class="text-sm text-red-600"
            role="alert"
            data-test="export-mail-error"
          >
            {{ mailError }}
          </span>
        </div>
      </section>

      <!-- how to query -->
      <section class="flex flex-col gap-3" data-test="export-howto">
        <div>
          <h2 class="text-lg font-medium">How to query</h2>
          <p class="text-sm text-muted-color">
            A date range, exported with the print profile above:
            <span data-test="export-profile-line">{{ profileLine }}</span>
          </p>
        </div>

        <div class="grid gap-3 sm:grid-cols-2 max-w-md">
          <label class="flex flex-col gap-1 text-sm">
            From
            <InputText
              v-model="query.from"
              type="date"
              :invalid="!!queryErrors.from"
              data-test="export-query-from"
            />
            <span
              v-if="queryErrors.from"
              class="text-xs text-red-600"
              role="alert"
              data-test="export-error-from"
            >
              {{ queryErrors.from }}
            </span>
          </label>
          <label class="flex flex-col gap-1 text-sm">
            To
            <InputText
              v-model="query.to"
              type="date"
              :invalid="!!queryErrors.to"
              data-test="export-query-to"
            />
            <span
              v-if="queryErrors.to"
              class="text-xs text-red-600"
              role="alert"
              data-test="export-error-to"
            >
              {{ queryErrors.to }}
            </span>
          </label>
        </div>

        <!-- wrapped, not scrolled: a command half of which is off the right edge is a command
             nobody can check before running it. Copying a wrapped `pre` still yields the newlines
             the shell needs, because the line breaks it adds are visual only. -->
        <pre
          class="whitespace-pre-wrap break-all rounded bg-surface-100 p-3 text-xs dark:bg-surface-800"
          data-test="export-curl"
        >{{ curl }}</pre>

        <div class="flex items-center gap-3">
          <Button
            label="Copy"
            icon="pi pi-copy"
            size="small"
            outlined
            data-test="export-copy"
            @click="copyCommand"
          />
          <span
            v-if="copied"
            class="text-sm text-green-600"
            role="status"
            aria-live="polite"
            data-test="export-copied"
          >
            Copied.
          </span>
        </div>
        <p class="text-xs text-muted-color">
          <code>$MEMOS_PAT</code> is your own access token — create one under General → Access
          tokens. It is never filled in here.
        </p>
      </section>
    </template>
  </div>
</template>
