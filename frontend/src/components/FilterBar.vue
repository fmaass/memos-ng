<script setup lang="ts">
import Button from 'primevue/button'
import DatePicker from 'primevue/datepicker'
import InputText from 'primevue/inputtext'
import MultiSelect from 'primevue/multiselect'
import Select from 'primevue/select'
import SelectButton from 'primevue/selectbutton'
import { computed, onUnmounted, ref, watch } from 'vue'

import { useTags } from '@/composables/useTags'
import {
  isEmptyFilter,
  type MemoFilter,
  pillsFor,
  removeFilterKey,
  type Visibility,
} from '@/lib/memoFilter'

const filter = defineModel<MemoFilter>({ required: true })

const { data: tags } = useTags()
const tagOptions = computed(() => (tags.value ?? []).map((t) => t.name))

const visibilityOptions: { label: string; value: Visibility }[] = [
  { label: 'Private', value: 'PRIVATE' },
  { label: 'Protected', value: 'PROTECTED' },
  { label: 'Public', value: 'PUBLIC' },
]
const archivedOptions = [
  { label: 'Active', value: 'exclude' },
  { label: 'Include archived', value: 'include' },
  { label: 'Archived only', value: 'only' },
]

// The pure filter state prefers `undefined` for the archived default; the SelectButton needs a
// concrete value, so map undefined→'exclude' for display and back on write.
const archivedValue = computed({
  get: () => filter.value.archived ?? 'exclude',
  set: (v: MemoFilter['archived']) => patch({ archived: v === 'exclude' ? undefined : v }),
})

function patch(part: Partial<MemoFilter>) {
  filter.value = { ...filter.value, ...part }
}

// A local Date → 'YYYY-MM-DD' (local calendar day, matching how memoFilter reads the string).
function toDateStr(d: Date): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

// Debounced free-text (preserves the prior 300 ms search behaviour) so keystrokes don't refire
// the query on every character.
const text = ref(filter.value.text ?? '')
let timer: ReturnType<typeof setTimeout> | undefined
watch(
  () => filter.value.text,
  (v) => {
    if ((v ?? '') !== text.value) text.value = v ?? ''
  },
)
function onText() {
  clearTimeout(timer)
  timer = setTimeout(() => patch({ text: text.value.trim() || undefined }), 300)
}
onUnmounted(() => clearTimeout(timer))

const pills = computed(() => pillsFor(filter.value))
function clearAll() {
  text.value = ''
  filter.value = { tags: [] }
}
</script>

<template>
  <div class="flex flex-col gap-2" data-test="filter-bar">
    <InputText
      v-model="text"
      placeholder="Search memos…"
      data-test="filter-text"
      @input="onText"
    />
    <div class="flex flex-wrap items-center gap-2">
      <MultiSelect
        :model-value="filter.tags"
        :options="tagOptions"
        filter
        placeholder="Tags"
        size="small"
        data-test="filter-tags"
        class="min-w-40"
        @update:model-value="(v) => patch({ tags: v as string[] })"
      />
      <Select
        :model-value="filter.visibility ?? null"
        :options="visibilityOptions"
        option-label="label"
        option-value="value"
        show-clear
        placeholder="Visibility"
        size="small"
        data-test="filter-visibility"
        @update:model-value="(v) => patch({ visibility: (v as Visibility) ?? undefined })"
      />
      <DatePicker
        :model-value="filter.dateFrom ? new Date(filter.dateFrom + 'T00:00:00') : null"
        placeholder="From"
        date-format="yy-mm-dd"
        show-icon
        size="small"
        data-test="filter-date-from"
        @update:model-value="(d) => patch({ dateFrom: d ? toDateStr(d as Date) : undefined })"
      />
      <DatePicker
        :model-value="filter.dateTo ? new Date(filter.dateTo + 'T00:00:00') : null"
        placeholder="To"
        date-format="yy-mm-dd"
        show-icon
        size="small"
        data-test="filter-date-to"
        @update:model-value="(d) => patch({ dateTo: d ? toDateStr(d as Date) : undefined })"
      />
      <SelectButton
        v-model="archivedValue"
        :options="archivedOptions"
        option-label="label"
        option-value="value"
        :allow-empty="false"
        size="small"
        data-test="filter-archived"
      />
      <Button
        v-if="!isEmptyFilter(filter)"
        label="Clear"
        severity="secondary"
        text
        size="small"
        data-test="filter-clear"
        @click="clearAll"
      />
    </div>
    <div v-if="pills.length" class="flex flex-wrap gap-1.5" data-test="filter-pills">
      <button
        v-for="p in pills"
        :key="p.key"
        type="button"
        class="inline-flex items-center gap-1 rounded-full bg-surface-100 dark:bg-surface-800 px-2.5 py-0.5 text-xs"
        :data-test="`pill-${p.key}`"
        @click="filter = removeFilterKey(filter, p.key)"
      >
        {{ p.label }}
        <i class="pi pi-times text-[10px]" aria-hidden="true" />
      </button>
    </div>
  </div>
</template>
