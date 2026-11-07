<script setup lang="ts">
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import ToggleSwitch from 'primevue/toggleswitch'
import { computed, ref } from 'vue'

import {
  DEFAULT_DIGEST_LANGUAGE,
  DIGEST_LANGUAGES,
  DIGEST_LANGUAGE_LABELS,
  type DigestLanguage,
  type DigestRecipient,
  checkNewRecipient,
  checkRecipientLanguage,
  recipientChangeGuard,
} from '@/lib/digest'

const props = defineProps<{
  recipients: DigestRecipient[] | undefined
  digestEnabled: boolean
  busy: number | 'add' | null
  error: string | null
}>()

const emit = defineEmits<{
  add: [{ email: string; display_name: string | null; language: DigestLanguage }]
  toggle: [{ id: number; enabled: boolean }]
  language: [{ id: number; language: DigestLanguage }]
  remove: [id: number]
}>()

const email = ref('')
const displayName = ref('')
const language = ref<DigestLanguage>(DEFAULT_DIGEST_LANGUAGE)
const languageOptions = DIGEST_LANGUAGES.map((value) => ({
  value,
  label: DIGEST_LANGUAGE_LABELS[value],
}))
const localError = ref<string | null>(null)

// Each row carries the client-side answer to "may this recipient change?" — the same rule the API
// enforces under a row lock. Shown as the reason on a disabled control, so the admin reads it
// before acting instead of meeting a bare 422 afterwards.
const rows = computed(() => {
  const all = props.recipients ?? []
  return all.map((r) => {
    const guard = recipientChangeGuard(r, all, props.digestEnabled)
    return { ...r, blocked: !guard.allowed, reason: guard.allowed ? null : guard.reason }
  })
})

function submit() {
  const verdict = checkNewRecipient(
    email.value,
    rows.value.map((r) => r.email),
  )
  if (!verdict.ok) {
    localError.value = verdict.reason
    return
  }
  const spoken = checkRecipientLanguage(language.value)
  if (!spoken.ok) {
    localError.value = spoken.reason
    return
  }
  localError.value = null
  emit('add', {
    email: verdict.email,
    display_name: displayName.value.trim() || null,
    language: spoken.language,
  })
  email.value = ''
  displayName.value = ''
  language.value = DEFAULT_DIGEST_LANGUAGE
}

// A row's language is checked by the same pure rule before it is sent; anything else is refused
// here rather than met as a 422.
function changeLanguage(id: number, value: unknown) {
  const spoken = checkRecipientLanguage(value)
  if (!spoken.ok) {
    localError.value = spoken.reason
    return
  }
  localError.value = null
  emit('language', { id, language: spoken.language })
}
</script>

<template>
  <div class="flex flex-col gap-3" data-test="digest-recipients">
    <div>
      <h3 class="font-medium">Recipients</h3>
      <p class="text-sm text-muted-color">Everyone the digest is mailed to, one address each.</p>
    </div>

    <p v-if="!rows.length" class="text-sm text-muted-color" data-test="digest-recipients-empty">
      No recipients yet — the digest has nobody to send to.
    </p>

    <ul v-else class="flex flex-col divide-y divide-surface-200 dark:divide-surface-700">
      <li
        v-for="r in rows"
        :key="r.id"
        class="flex items-center gap-3 py-2"
        data-test="digest-recipient-row"
      >
        <div class="flex-1 min-w-0">
          <p class="truncate text-sm" data-test="digest-recipient-email">{{ r.email }}</p>
          <p v-if="r.display_name" class="truncate text-xs text-muted-color">
            {{ r.display_name }}
          </p>
          <p v-if="r.reason" class="text-xs text-amber-600" data-test="digest-recipient-guard">
            {{ r.reason }}
          </p>
        </div>
        <Select
          :model-value="r.language"
          :options="languageOptions"
          option-label="label"
          option-value="value"
          size="small"
          :disabled="busy !== null"
          :aria-label="`Language: ${r.email}`"
          data-test="digest-recipient-language"
          @update:model-value="changeLanguage(r.id, $event)"
        />
        <ToggleSwitch
          :model-value="r.enabled"
          :disabled="busy !== null || (r.enabled && r.blocked)"
          :aria-label="`Enabled: ${r.email}`"
          data-test="digest-recipient-toggle"
          @update:model-value="emit('toggle', { id: r.id, enabled: !r.enabled })"
        />
        <Button
          text
          size="small"
          icon="pi pi-trash"
          :disabled="busy !== null || r.blocked"
          :aria-label="`Remove ${r.email}`"
          data-test="digest-recipient-remove"
          @click="emit('remove', r.id)"
        />
      </li>
    </ul>

    <div class="flex flex-col gap-2 sm:flex-row sm:items-start">
      <InputText
        v-model="email"
        class="flex-1"
        placeholder="name@example.test"
        aria-label="Recipient address"
        data-test="digest-recipient-input"
        @keyup.enter="submit"
      />
      <InputText
        v-model="displayName"
        class="flex-1"
        placeholder="Display name (optional)"
        aria-label="Recipient display name"
        data-test="digest-recipient-name"
        @keyup.enter="submit"
      />
      <Select
        v-model="language"
        :options="languageOptions"
        option-label="label"
        option-value="value"
        aria-label="Recipient language"
        data-test="digest-recipient-new-language"
      />
      <Button
        label="Add"
        icon="pi pi-plus"
        :loading="busy === 'add'"
        :disabled="busy !== null"
        data-test="digest-recipient-add"
        @click="submit"
      />
    </div>

    <p
      v-if="localError"
      class="text-sm text-red-600"
      role="alert"
      data-test="digest-recipient-local-error"
    >
      {{ localError }}
    </p>
    <p v-if="error" class="text-sm text-red-600" role="alert" data-test="digest-recipient-error">
      {{ error }}
    </p>
  </div>
</template>
