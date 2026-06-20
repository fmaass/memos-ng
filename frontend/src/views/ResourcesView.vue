<script setup lang="ts">
import { useQuery } from '@tanstack/vue-query'
import SelectButton from 'primevue/selectbutton'
import { computed, ref } from 'vue'

import JustifiedGallery from '@/components/JustifiedGallery.vue'
import MediaLightbox from '@/components/MediaLightbox.vue'
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

const items = computed(() => data.value ?? [])
const lightboxIndex = ref<number | null>(null)
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
      data-test="type-filter"
    />
    <p v-if="isPending" class="text-sm text-muted-color">Loading…</p>
    <p v-else-if="!items.length" class="text-sm text-muted-color" data-test="resources-empty">
      No attachments yet.
    </p>
    <JustifiedGallery v-else :items="items" :target-height="200" @open="lightboxIndex = $event" />
    <MediaLightbox v-model="lightboxIndex" :items="items" />
  </section>
</template>
