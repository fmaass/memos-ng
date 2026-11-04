<script setup lang="ts">
import Button from 'primevue/button'
import DatePicker from 'primevue/datepicker'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import SelectButton from 'primevue/selectbutton'
import Tag from 'primevue/tag'
import { computed, ref } from 'vue'

import TripPassDevices from '@/components/TripPassDevices.vue'
import { useTripPasses, useTripQuarantine } from '@/composables/useTripPasses'
import {
  buildPassCreate,
  DEFAULT_MAX_DEVICES,
  LABEL_MAX,
  MAX_DEVICES_MAX,
  MAX_DEVICES_MIN,
  OFFERED_SCOPES,
  type PassOut,
  type PassScope,
  sanitizeQrSvg,
  SCOPE_LABELS,
} from '@/lib/tripPasses'

// No owner-only gate here on purpose: any member of the family may invite guests, and the server
// is what decides who may (app/routers/passes.py, `auth.require_user`).
const props = defineProps<{ collectionUid: string }>()

const {
  passes,
  issuedHere,
  error,
  issue,
  revokePass,
  unsuspendPass,
  revokeDevice,
  dismissIssued,
  issuing,
  revokingPass,
  revokingDevice,
} = useTripPasses(() => props.collectionUid)
const { quarantine, resolveBatch, clearUnknown } = useTripQuarantine()

const label = ref('')
const scope = ref<PassScope>('contribute')
const expiresOn = ref<Date | null>(null)
const maxDevices = ref<number | null>(DEFAULT_MAX_DEVICES)
const formError = ref<string | null>(null)
const copied = ref(false)
const openDevices = ref(new Set<string>())
const confirming = ref<string | null>(null)

const scopeOptions = OFFERED_SCOPES.map((value) => ({
  value,
  label: SCOPE_LABELS[value],
}))
const minDay = new Date()

const qrMarkup = computed(() => (issuedHere.value ? sanitizeQrSvg(issuedHere.value.qrSvg) : ''))

async function submit() {
  const built = buildPassCreate({
    label: label.value,
    scope: scope.value,
    expiresOn: expiresOn.value,
    maxDevices: maxDevices.value,
  })
  if (!built.ok) {
    formError.value = built.error
    return
  }
  formError.value = null
  copied.value = false
  const result = await issue(built.body)
  if (result) label.value = ''
}

async function copyLink() {
  if (issuedHere.value && navigator.clipboard) {
    await navigator.clipboard.writeText(issuedHere.value.url)
    copied.value = true
  }
}

function toggleDevices(uid: string) {
  const next = new Set(openDevices.value)
  if (next.has(uid)) next.delete(uid)
  else next.add(uid)
  openDevices.value = next
}

async function confirmRevoke(uid: string) {
  confirming.value = null
  await revokePass(uid)
}

const severity = (p: PassOut) =>
  p.state === 'active' ? 'success' : p.state === 'expired' ? 'warn' : 'secondary'
const day = (iso: string) => new Date(iso).toLocaleDateString()
</script>

<template>
  <div class="flex flex-col gap-4" data-test="trip-pass-manager">
    <Message v-if="error" severity="error" :closable="false" data-test="pass-error">
      {{ error }}
    </Message>

    <div
      v-if="issuedHere"
      class="flex flex-col items-center gap-2 border border-surface-200 dark:border-surface-700 rounded-lg p-3"
      data-test="pass-issued"
    >
      <p class="text-sm text-amber-600">
        Show this code to your guests now. The link is not shown again.
      </p>
      <!-- The SVG has no background of its own; white keeps it scannable in dark mode. -->
      <!-- eslint-disable-next-line vue/no-v-html -- sanitized to SVG by sanitizeQrSvg -->
      <div class="bg-white p-2 rounded" data-test="pass-qr" v-html="qrMarkup" />
      <div class="flex items-center gap-2 w-full">
        <code
          class="flex-1 break-all text-xs bg-surface-100 dark:bg-surface-800 p-2 rounded"
          data-test="pass-issued-url"
          >{{ issuedHere.url }}</code
        >
        <Button icon="pi pi-copy" aria-label="Copy link" data-test="pass-copy" @click="copyLink" />
      </div>
      <span v-if="copied" class="text-xs text-green-600">Copied</span>
      <Button
        label="Done"
        size="small"
        severity="secondary"
        data-test="pass-issued-done"
        @click="dismissIssued"
      />
    </div>

    <form class="flex flex-col gap-3" data-test="pass-form" @submit.prevent>
      <h3 class="font-medium">Invite guests</h3>
      <label class="flex flex-col gap-1 text-sm">
        Label
        <InputText
          v-model="label"
          :maxlength="LABEL_MAX"
          placeholder="Who is it for, e.g. Grandparents"
          fluid
          data-test="pass-label"
        />
      </label>
      <div class="flex flex-col gap-1 text-sm">
        Guests may
        <SelectButton
          v-model="scope"
          :options="scopeOptions"
          option-label="label"
          option-value="value"
          :allow-empty="false"
          data-test="pass-scope"
        />
      </div>
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <label class="flex flex-col gap-1 text-sm">
          Expires after
          <DatePicker
            v-model="expiresOn"
            :min-date="minDay"
            show-button-bar
            placeholder="Trip end (or today, if later) + 14 days"
            fluid
            data-test="pass-expiry"
          />
        </label>
        <label class="flex flex-col gap-1 text-sm">
          Device limit
          <InputNumber
            v-model="maxDevices"
            :min="MAX_DEVICES_MIN"
            :max="MAX_DEVICES_MAX"
            show-buttons
            fluid
            data-test="pass-max-devices"
          />
        </label>
      </div>
      <p v-if="formError" class="text-sm text-red-600" data-test="pass-form-error">
        {{ formError }}
      </p>
      <Button
        label="Issue pass"
        icon="pi pi-qrcode"
        :loading="issuing"
        data-test="pass-issue"
        @click="submit"
      />
    </form>

    <div class="flex flex-col gap-2" data-test="pass-list">
      <h3 class="font-medium">Passes</h3>
      <p v-if="passes.isPending.value" class="text-muted-color text-sm">Loading passes…</p>
      <p v-else-if="passes.isError.value" class="text-red-600 text-sm" data-test="pass-list-error">
        {{ passes.error.value?.message }}
      </p>
      <p v-else-if="!passes.data.value?.length" class="text-muted-color text-sm" data-test="pass-empty">
        No pass for this trip yet.
      </p>
      <ul v-else class="flex flex-col gap-2">
        <li
          v-for="p in passes.data.value"
          :key="p.uid"
          class="flex flex-col gap-1 border border-surface-200 dark:border-surface-700 rounded-lg p-2"
          data-test="pass-row"
          :data-uid="p.uid"
        >
          <div class="flex items-center justify-between gap-2">
            <span class="min-w-0">
              <span class="font-medium text-sm">{{ p.label }}</span>
              <span class="text-muted-color text-xs">
                · {{ SCOPE_LABELS[p.scope] }} · {{ p.deviceCount }}/{{ p.maxDevices }} devices ·
                until {{ day(p.expiresAt) }}
              </span>
            </span>
            <Tag :value="p.state" :severity="severity(p)" data-test="pass-state" />
            <Tag
              v-if="p.doorSuspended === true"
              value="suspended"
              severity="danger"
              data-test="pass-suspended"
            />
            <Button
              v-if="p.doorSuspended === true"
              label="Unsuspend"
              size="small"
              severity="secondary"
              data-test="pass-unsuspend"
              @click="unsuspendPass(p.uid)"
            />
          </div>
          <div class="flex flex-wrap items-center gap-2">
            <Button
              text
              size="small"
              :label="openDevices.has(p.uid) ? 'Hide devices' : 'Devices'"
              data-test="pass-devices-toggle"
              @click="toggleDevices(p.uid)"
            />
            <template v-if="p.state !== 'revoked'">
              <Button
                v-if="confirming !== p.uid"
                text
                size="small"
                severity="danger"
                label="Revoke pass"
                :loading="revokingPass(p.uid)"
                data-test="pass-revoke"
                @click="confirming = p.uid"
              />
              <template v-else>
                <span class="text-xs">Every device on it stops working.</span>
                <Button
                  size="small"
                  severity="danger"
                  label="Revoke all"
                  data-test="pass-revoke-confirm"
                  @click="confirmRevoke(p.uid)"
                />
                <Button
                  text
                  size="small"
                  label="Cancel"
                  data-test="pass-revoke-cancel"
                  @click="confirming = null"
                />
              </template>
            </template>
          </div>
          <TripPassDevices
            v-if="openDevices.has(p.uid)"
            :pass-uid="p.uid"
            :live="p.state === 'active'"
            :revoke="(d: string) => revokeDevice(p.uid, d)"
            :busy="(d: string) => revokingDevice(p.uid, d)"
          />
        </li>
      </ul>
    </div>
    <section
      v-if="quarantine.data.value && (quarantine.data.value.batches.length || quarantine.data.value.unknownOpen)"
      class="flex flex-col gap-2"
      data-test="quarantine"
    >
      <h3 class="text-sm font-semibold">Quarantined guest uploads</h3>
      <p class="text-xs">
        Marking one as seen files nothing and deletes nothing, and the guest's bytes stay on the door.
      </p>
      <div
        v-for="b in quarantine.data.value.batches"
        :key="b.uid"
        class="flex items-center justify-between gap-2"
      >
        <span class="text-sm">{{ b.fileCount }} files from {{ day(b.createdAt) }}</span>
        <Button
          label="Mark seen"
          size="small"
          severity="secondary"
          data-test="quarantine-resolve"
          @click="resolveBatch(b.uid)"
        />
      </div>
      <div v-if="quarantine.data.value.unknownOpen" class="flex items-center justify-between gap-2">
        <span class="text-sm">{{ quarantine.data.value.unknownOpen }} from a pass I never issued</span>
        <Button
          label="Mark seen"
          size="small"
          severity="secondary"
          data-test="quarantine-clear-unknown"
          @click="clearUnknown(quarantine.data.value!.unknownTotal)"
        />
      </div>
    </section>
  </div>
</template>
