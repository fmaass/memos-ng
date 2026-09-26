<script setup lang="ts">
import Menu from 'primevue/menu'
import { computed, ref } from 'vue'

import { useAttachmentTags } from '@/composables/useAttachmentLibrary'
import type { LibraryFilter, LibraryKind, LibraryOrder } from '@/lib/libraryFilter'

// The Library's options (plan decision 12): three radio groups, a checkmark on the active row —
// the order, the kind, and one tag (or all) with its picture count. No "Mine": filtering it client
// side over pages would make the grid disagree with the month counts, and a server parameter is out
// of scope.
const props = defineProps<{ filter: LibraryFilter }>()
const emit = defineEmits<{ 'update:filter': [filter: LibraryFilter] }>()

// The counts are only worth a request once someone opens the menu; after that they stay live.
const opened = ref(false)
const tags = useAttachmentTags(() => props.filter.type, opened)

interface Row {
  key?: string
  label?: string
  checked?: boolean
  count?: number
  disabled?: boolean
  separator?: boolean
  command?: () => void
}

function pick(next: Partial<LibraryFilter>) {
  const merged: LibraryFilter = { ...props.filter, ...next }
  // An absent field is the default; never hand back `type: undefined` as a value.
  if (!merged.type) delete merged.type
  if (!merged.tag) delete merged.tag
  emit('update:filter', merged)
}

const ORDERS: { value: LibraryOrder; label: string }[] = [
  { value: 'newest', label: 'Sort by Newest First' },
  { value: 'oldest', label: 'Sort by Oldest First' },
]
const KINDS: { value: LibraryKind | undefined; key: string; label: string }[] = [
  { value: undefined, key: 'all', label: 'All Items' },
  { value: 'image', key: 'image', label: 'Photos' },
  { value: 'video', key: 'video', label: 'Videos' },
]

const tagRows = computed<Row[]>(() => {
  const active = props.filter.tag
  const rows: Row[] = [
    { key: 'tag-all', label: 'All tags', checked: !active, command: () => pick({ tag: undefined }) },
  ]
  // A failed load says so; it is never shown as "no tags" (LSN-0027).
  if (tags.isError.value) {
    rows.push({ key: 'tags-error', label: 'Couldn’t load tags', disabled: true })
  } else if (tags.isPending.value && opened.value) {
    rows.push({ key: 'tags-loading', label: 'Loading tags…', disabled: true })
  }
  const counts = tags.data.value ?? []
  for (const t of counts) {
    rows.push({
      key: `tag-${t.name}`,
      label: `#${t.name}`,
      count: t.count,
      checked: t.name === active,
      command: () => pick({ tag: t.name }),
    })
  }
  // The active tag stays reachable (and clearable) even when the counts do not name it — a link
  // to a tag with nothing of this kind, say.
  if (active && !counts.some((t) => t.name === active)) {
    rows.push({ key: `tag-${active}`, label: `#${active}`, checked: true, command: () => {} })
  }
  return rows
})

const model = computed<Row[]>(() => [
  ...ORDERS.map((o) => ({
    key: `order-${o.value}`,
    label: o.label,
    checked: props.filter.order === o.value,
    command: () => pick({ order: o.value }),
  })),
  { separator: true },
  ...KINDS.map((k) => ({
    key: `kind-${k.key}`,
    label: k.label,
    checked: props.filter.type === k.value,
    command: () => pick({ type: k.value }),
  })),
  { separator: true },
  ...tagRows.value,
])

const menu = ref<InstanceType<typeof Menu> | null>(null)
defineExpose({
  toggle(event: Event) {
    opened.value = true
    menu.value?.toggle(event)
  },
})
</script>

<template>
  <Menu
    id="library-options"
    ref="menu"
    :model="model"
    popup
    class="glass max-h-[70vh] overflow-y-auto"
  >
    <template #item="{ item, props: itemProps }">
      <a
        class="flex items-center gap-2 px-3 py-2"
        :data-test="`menu-${item.key}`"
        :data-checked="item.checked ? 'true' : 'false'"
        v-bind="itemProps.action"
      >
        <i class="w-4 text-sm" :class="item.checked ? 'pi pi-check' : ''" aria-hidden="true" />
        <span class="flex-1">{{ item.label }}</span>
        <span v-if="item.count !== undefined" class="text-sm text-muted-color" data-test="tag-count">{{
          item.count
        }}</span>
      </a>
    </template>
  </Menu>
</template>
