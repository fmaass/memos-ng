<script setup lang="ts">
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import SelectButton from 'primevue/selectbutton'
import ToggleSwitch from 'primevue/toggleswitch'
import { computed, nextTick, ref, watch } from 'vue'

import DigestPreviewDrawer from '@/components/DigestPreviewDrawer.vue'
import DigestRecipients from '@/components/DigestRecipients.vue'
import DigestRuns from '@/components/DigestRuns.vue'
import { useDigest } from '@/composables/useDigest'
import { useDigestActions } from '@/composables/useDigestActions'
import {
  type DigestConfigDraft,
  describeNextSend,
  dirtyConfigFields,
  draftFromSettings,
  formatMib,
  lastRunLabel,
  nextSendLabel,
  summariseLastRun,
  validateConfigDraft,
} from '@/lib/digest'

const {
  settings,
  recipients,
  runs,
  hosting,
  savingConfig,
  togglingEnabled,
  recipientBusy,
  configError,
  enabledError,
  recipientError,
  notice,
  clearNotice,
  saveConfig,
  setEnabled,
  addRecipient,
  setRecipientEnabled,
  removeRecipient,
  refreshRuns,
} = useDigest()

const {
  preview,
  previewing,
  previewError,
  sendingTest,
  runningNow,
  actionError,
  actionNotice,
  clearActionMessages,
  loadPreview,
  sendTest,
  runNow,
} = useDigestActions()

// The grouped draft is seeded ONCE, when the settings first arrive, and re-seeded only by Save and
// Discard. A background refetch must not silently overwrite what the admin is typing.
const draft = ref<DigestConfigDraft | null>(null)
watch(
  settings,
  (value) => {
    if (value && draft.value === null) draft.value = draftFromSettings(value)
  },
  { immediate: true },
)

const errors = computed(() => (draft.value ? validateConfigDraft(draft.value) : {}))
const dirty = computed(() =>
  draft.value && settings.value ? dirtyConfigFields(draft.value, settings.value).length > 0 : false,
)
const canSave = computed(() => dirty.value && Object.keys(errors.value).length === 0)

// `now` is read once per render pass of the status row rather than on a timer: this line answers
// "what did I just configure", not "how long until the next send".
const now = new Date()
const nextSend = computed(() =>
  settings.value ? nextSendLabel(describeNextSend(settings.value, now, runs.value)) : '',
)
const lastRun = computed(() =>
  summariseLastRun(runs.value, settings.value?.timezone ?? 'UTC', now),
)

const previewOpen = ref(false)
const previewButton = ref<{ $el?: HTMLElement } | null>(null)

/** Escape and the drawer's own Close button both arrive here. Focus must go back to the control
 * that opened the drawer — otherwise a keyboard user is dropped at the top of the document with no
 * idea where they are. `nextTick` because the drawer is still tearing its focus trap down. */
function setPreviewOpen(open: boolean) {
  previewOpen.value = open
  if (!open) void nextTick(() => previewButton.value?.$el?.focus())
}

function setNumber(field: keyof DigestConfigDraft, value: string | undefined) {
  if (!draft.value) return
  const parsed = Number((value ?? '').trim())
  // NaN is deliberate: an emptied or garbled number field must fail validation rather than quietly
  // become 0, which for `max_memos_per_run` would be a legal-looking setting that sends nothing.
  ;(draft.value[field] as number) = (value ?? '').trim() === '' ? NaN : parsed
}

function discard() {
  if (settings.value) draft.value = draftFromSettings(settings.value)
  configError.value = null
}

async function save() {
  if (!draft.value || !canSave.value) return
  const ok = await saveConfig(draft.value)
  if (ok) discard() // re-seed from what the server actually stored
}

async function openPreview() {
  clearActionMessages()
  setPreviewOpen(true)
  await loadPreview()
}

async function onRunNow() {
  const started = await runNow()
  if (started) await refreshRuns()
}

const scopeOptions = [
  { label: 'Public only', value: 'PUBLIC' },
  { label: 'Public + protected', value: 'PUBLIC_PROTECTED' },
]
</script>

<template>
  <div class="flex flex-col gap-6" data-test="digest-panel">
    <div v-if="!settings" class="text-sm text-muted-color" data-test="digest-loading">
      Loading the digest settings…
    </div>

    <template v-else-if="draft">
      <!-- status row -->
      <div class="flex flex-col gap-2" data-test="digest-status">
        <div class="flex items-center gap-3">
          <h2 class="text-lg font-medium">Digest e-mail</h2>
          <ToggleSwitch
            :model-value="settings.enabled"
            :disabled="togglingEnabled"
            aria-label="Digest enabled"
            data-test="digest-enabled"
            @update:model-value="setEnabled(!settings.enabled)"
          />
          <span class="text-sm text-muted-color" data-test="digest-enabled-state">
            {{ settings.enabled ? 'On' : 'Off' }}
          </span>
        </div>

        <p class="text-sm text-muted-color" data-test="digest-next-send">{{ nextSend }}</p>
        <p class="text-sm" data-test="digest-last-run">{{ lastRunLabel(lastRun) }}</p>
        <p
          v-if="lastRun.kind === 'run' && lastRun.needsSameDayRecovery"
          class="text-sm text-amber-600"
          data-test="digest-recovery"
        >
          Today's run failed and nothing retries before tomorrow — use Send now to recover today.
        </p>

        <div class="flex flex-wrap gap-2">
          <Button
            ref="previewButton"
            label="Preview"
            icon="pi pi-eye"
            size="small"
            outlined
            :loading="previewing"
            data-test="digest-preview-open"
            @click="openPreview"
          />
          <Button
            label="Send test to me"
            icon="pi pi-send"
            size="small"
            outlined
            :loading="sendingTest"
            data-test="digest-test-send"
            @click="sendTest(false)"
          />
          <Button
            label="Send now"
            icon="pi pi-play"
            size="small"
            severity="secondary"
            :loading="runningNow"
            data-test="digest-run-now"
            @click="onRunNow"
          />
        </div>

        <p
          v-if="enabledError"
          class="text-sm text-red-600"
          role="alert"
          data-test="digest-enabled-error"
        >
          {{ enabledError }}
        </p>
        <p
          v-if="actionError"
          class="text-sm text-red-600"
          role="alert"
          data-test="digest-action-error"
        >
          {{ actionError }}
        </p>
        <p
          v-if="actionNotice"
          class="text-sm text-green-600"
          role="status"
          aria-live="polite"
          data-test="digest-action-notice"
        >
          {{ actionNotice }}
        </p>
        <p
          v-if="notice"
          class="text-sm text-green-600"
          role="status"
          aria-live="polite"
          data-test="digest-notice"
          @click="clearNotice"
        >
          {{ notice }}
        </p>
      </div>

      <!-- schedule & scope -->
      <div class="flex flex-col gap-3" data-test="digest-schedule">
        <h3 class="font-medium">Schedule &amp; scope</h3>
        <div class="grid gap-3 sm:grid-cols-2">
          <label class="flex flex-col gap-1 text-sm">
            Send at
            <InputText
              v-model="draft.send_time"
              type="time"
              data-test="digest-send-time"
              :invalid="!!errors.send_time"
            />
            <span v-if="errors.send_time" class="text-xs text-red-600" data-test="digest-error-send_time">
              {{ errors.send_time }}
            </span>
          </label>
          <label class="flex flex-col gap-1 text-sm">
            Time zone
            <InputText
              v-model="draft.timezone"
              data-test="digest-timezone"
              :invalid="!!errors.timezone"
            />
            <span v-if="errors.timezone" class="text-xs text-red-600" data-test="digest-error-timezone">
              {{ errors.timezone }}
            </span>
          </label>
          <label class="flex flex-col gap-1 text-sm">
            First day considered
            <InputText
              v-model="draft.start_date"
              type="date"
              data-test="digest-start-date"
              :invalid="!!errors.start_date"
            />
            <span v-if="errors.start_date" class="text-xs text-red-600" data-test="digest-error-start_date">
              {{ errors.start_date }}
            </span>
          </label>
          <label class="flex flex-col gap-1 text-sm">
            Grace window (minutes)
            <InputText
              type="number"
              :model-value="String(draft.grace_minutes)"
              data-test="digest-grace-minutes"
              :invalid="!!errors.grace_minutes"
              @update:model-value="setNumber('grace_minutes', $event)"
            />
            <span v-if="errors.grace_minutes" class="text-xs text-red-600" data-test="digest-error-grace_minutes">
              {{ errors.grace_minutes }}
            </span>
          </label>
        </div>
        <div class="flex flex-col gap-1 text-sm">
          <span>Which memos</span>
          <SelectButton
            v-model="draft.visibility_scope"
            :options="scopeOptions"
            option-label="label"
            option-value="value"
            :allow-empty="false"
            data-test="digest-visibility-scope"
          />
        </div>
      </div>

      <!-- recipients -->
      <DigestRecipients
        :recipients="recipients"
        :digest-enabled="settings.enabled"
        :busy="recipientBusy"
        :error="recipientError"
        @add="addRecipient($event)"
        @toggle="setRecipientEnabled($event.id, $event.enabled)"
        @remove="removeRecipient($event)"
      />

      <!-- advanced -->
      <div class="flex flex-col gap-3" data-test="digest-advanced">
        <h3 class="font-medium">Advanced</h3>
        <div class="grid gap-3 sm:grid-cols-2">
          <label class="flex flex-col gap-1 text-sm">
            Subject
            <InputText
              v-model="draft.mail_title"
              data-test="digest-mail-title"
              :invalid="!!errors.mail_title"
            />
            <span v-if="errors.mail_title" class="text-xs text-red-600" data-test="digest-error-mail_title">
              {{ errors.mail_title }}
            </span>
          </label>
          <label class="flex flex-col gap-1 text-sm">
            Sender name
            <InputText
              v-model="draft.from_name"
              data-test="digest-from-name"
              :invalid="!!errors.from_name"
            />
            <span v-if="errors.from_name" class="text-xs text-red-600" data-test="digest-error-from_name">
              {{ errors.from_name }}
            </span>
          </label>
          <label class="flex flex-col gap-1 text-sm sm:col-span-2">
            Sender address
            <InputText
              v-model="draft.from_email"
              placeholder="empty = send as the SMTP user"
              data-test="digest-from-email"
              :invalid="!!errors.from_email"
            />
            <span v-if="errors.from_email" class="text-xs text-red-600" data-test="digest-error-from_email">
              {{ errors.from_email }}
            </span>
          </label>
          <label class="flex flex-col gap-1 text-sm">
            Maximum mail size (bytes)
            <InputText
              type="number"
              :model-value="String(draft.max_email_bytes)"
              data-test="digest-max-email-bytes"
              :invalid="!!errors.max_email_bytes"
              @update:model-value="setNumber('max_email_bytes', $event)"
            />
            <span class="text-xs text-muted-color" data-test="digest-max-email-mib">
              {{ formatMib(draft.max_email_bytes) }}
            </span>
            <span v-if="errors.max_email_bytes" class="text-xs text-red-600" data-test="digest-error-max_email_bytes">
              {{ errors.max_email_bytes }}
            </span>
          </label>
          <label class="flex flex-col gap-1 text-sm">
            Videos per memo
            <InputText
              type="number"
              :model-value="String(draft.max_videos_per_memo)"
              data-test="digest-max-videos"
              :invalid="!!errors.max_videos_per_memo"
              @update:model-value="setNumber('max_videos_per_memo', $event)"
            />
            <span v-if="errors.max_videos_per_memo" class="text-xs text-red-600" data-test="digest-error-max_videos_per_memo">
              {{ errors.max_videos_per_memo }}
            </span>
          </label>
          <label class="flex flex-col gap-1 text-sm">
            Memos per run
            <InputText
              type="number"
              :model-value="String(draft.max_memos_per_run)"
              data-test="digest-max-memos"
              :invalid="!!errors.max_memos_per_run"
              @update:model-value="setNumber('max_memos_per_run', $event)"
            />
            <span v-if="errors.max_memos_per_run" class="text-xs text-red-600" data-test="digest-error-max_memos_per_run">
              {{ errors.max_memos_per_run }}
            </span>
          </label>
          <label class="flex flex-col gap-1 text-sm">
            Keep hosted media for (days)
            <InputText
              type="number"
              :model-value="String(draft.retention_days)"
              data-test="digest-retention-days"
              :invalid="!!errors.retention_days"
              @update:model-value="setNumber('retention_days', $event)"
            />
            <span v-if="errors.retention_days" class="text-xs text-red-600" data-test="digest-error-retention_days">
              {{ errors.retention_days }}
            </span>
          </label>
        </div>
        <div class="flex items-center gap-3 text-sm">
          <ToggleSwitch
            v-model="draft.link_images_full"
            aria-label="Link images to the full-size file"
            data-test="digest-link-images-full"
          />
          <span>Link inline images to the full-size file</span>
        </div>
      </div>

      <!-- hosting -->
      <div class="flex flex-col gap-3" data-test="digest-hosting">
        <h3 class="font-medium">Hosting (Dropbox)</h3>
        <p class="text-sm" data-test="digest-hosting-status">
          <template v-if="hosting?.configured">
            Configured{{ hosting.account ? ` — ${hosting.account}` : '' }}.
          </template>
          <template v-else>Not configured — mails go out inline-only.</template>
        </p>
        <div class="flex items-center gap-3 text-sm">
          <ToggleSwitch
            v-model="draft.hosting_enabled"
            aria-label="Host videos on Dropbox"
            data-test="digest-hosting-enabled"
          />
          <span>Upload videos and link them</span>
        </div>
        <label class="flex flex-col gap-1 text-sm">
          Dropbox folder
          <InputText
            v-model="draft.dropbox_folder"
            data-test="digest-dropbox-folder"
            :invalid="!!errors.dropbox_folder"
          />
          <span v-if="errors.dropbox_folder" class="text-xs text-red-600" data-test="digest-error-dropbox_folder">
            {{ errors.dropbox_folder }}
          </span>
        </label>
      </div>

      <!-- runs -->
      <DigestRuns :runs="runs" />

      <!-- one bar for the grouped config; recipients and the switch save themselves -->
      <div
        v-if="dirty"
        class="sticky bottom-0 flex items-center gap-3 border-t border-surface-200 bg-surface-0 py-3 dark:border-surface-700 dark:bg-surface-900"
        role="group"
        aria-label="Unsaved digest settings"
        data-test="digest-save-bar"
      >
        <Button
          label="Save"
          :disabled="!canSave"
          :loading="savingConfig"
          data-test="digest-save"
          @click="save"
        />
        <Button label="Discard" text data-test="digest-discard" @click="discard" />
        <span
          v-if="configError"
          class="text-sm text-red-600"
          role="alert"
          data-test="digest-config-error"
        >
          {{ configError }}
        </span>
      </div>
    </template>

    <DigestPreviewDrawer
      :visible="previewOpen"
      :preview="preview"
      :loading="previewing"
      :error="previewError"
      @update:visible="setPreviewOpen"
    />
  </div>
</template>
