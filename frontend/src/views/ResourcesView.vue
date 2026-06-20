<script setup lang="ts">
import { useQuery } from '@tanstack/vue-query'
import SelectButton from 'primevue/selectbutton'
import { computed, ref } from 'vue'

import MediaTile from '@/components/MediaTile.vue'
import { useApi } from '@/composables/useApi'
import type { MemoQuery } from '@/composables/useTimeline'

const api = useApi()
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
</script>

<template>
  <section class="max-w-3xl mx-auto p-4 flex flex-col gap-3" data-test="view-resources">
    <h1 class="text-2xl font-semibold">Resources</h1>
    <SelectButton
      v-model="filter"
      :options="options"
      option-label="label"
      option-value="value"
      :allow-empty="false"
      data-test="type-filter"
    />
    <p v-if="isPending" class="text-muted-color text-sm">Loading…</p>
    <p v-else-if="!data?.length" class="text-muted-color text-sm" data-test="resources-empty">
      No attachments yet.
    </p>
    <div v-else class="grid grid-cols-3 sm:grid-cols-4 gap-2" data-test="resource-grid">
      <div v-for="a in data" :key="a.uid" class="relative">
        <MediaTile :att="a" />
        <span
          v-if="a.orphan"
          class="absolute top-1 right-1 text-xs bg-amber-500/80 text-white px-1 rounded"
          data-test="orphan-badge"
          >orphan</span
        >
      </div>
    </div>
  </section>
</template>
