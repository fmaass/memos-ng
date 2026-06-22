<script setup lang="ts">
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import SelectButton from 'primevue/selectbutton'
import { computed, onUnmounted, ref } from 'vue'

import JustifiedGallery from '@/components/JustifiedGallery.vue'
import { useApi } from '@/composables/useApi'
import { useMediaLightbox } from '@/composables/useMediaLightbox'
import type { MemoQuery } from '@/composables/useTimeline'

const api = useApi()
const queryClient = useQueryClient()
const confirm = useConfirm()
const toast = useToast()
const filter = ref<'all' | 'image' | 'video'>('all')
const options = [
  { label: 'All', value: 'all' },
  { label: 'Images', value: 'image' },
  { label: 'Videos', value: 'video' },
]

const { data, isPending } = useQuery({
  queryKey: computed(() => ['attachments', filter.value]),
  queryFn: async () => {
    const query: MemoQuery = {}
    if (filter.value !== 'all') query.type = filter.value
    const { data, error } = await api.GET('/api/attachments', { params: { query } })
    if (error) throw error
    return data
  },
})

const items = computed(() => data.value ?? [])

const lightbox = useMediaLightbox()
function openAt(index: number) {
  lightbox.open(items.value, index, {
    onDelete,
    onAddPrint: (uid, file) => onAddPrint({ uid, file }),
  })
}
onUnmounted(() => lightbox.destroy())

function onDelete(uid: string) {
  confirm.require({
    header: 'Delete media',
    message: 'Delete this media permanently? This cannot be undone.',
    icon: 'pi pi-exclamation-triangle',
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    acceptProps: { label: 'Delete', severity: 'danger' },
    accept: async () => {
      const { error } = await api.DELETE('/api/attachments/{uid}', { params: { path: { uid } } })
      if (error) {
        toast.add({ severity: 'error', summary: 'Delete failed', life: 4000 })
        return
      }
      await queryClient.invalidateQueries({ queryKey: ['attachments'] })
    },
  })
}

async function onAddPrint({ uid, file }: { uid: string; file: File }) {
  const fd = new FormData()
  fd.append('file', file)
  const res = await fetch(`/api/attachments/${uid}/print`, {
    method: 'POST',
    body: fd,
    credentials: 'include',
  })
  if (res.ok) {
    toast.add({ severity: 'success', summary: 'Print version attached', life: 2500 })
    await queryClient.invalidateQueries({ queryKey: ['attachments'] })
  } else {
    toast.add({ severity: 'error', summary: 'Print upload failed', life: 4000 })
  }
}
</script>

<template>
  <section class="mx-auto flex max-w-6xl flex-col gap-4 p-4" data-test="view-resources">
    <h1 class="text-2xl font-semibold">Resources</h1>
    <SelectButton
      v-model="filter"
      :options="options"
      option-label="label"
      option-value="value"
      :allow-empty="false"
      aria-label="Filter media type"
      data-test="type-filter"
    />
    <p v-if="isPending" class="text-sm text-muted-color">Loading…</p>
    <p v-else-if="!items.length" class="text-sm text-muted-color" data-test="resources-empty">
      No attachments yet.
    </p>
    <JustifiedGallery v-else :items="items" :target-height="200" @open="openAt" />
  </section>
</template>
