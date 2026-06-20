<script setup lang="ts">
import { useQuery } from '@tanstack/vue-query'
import Card from 'primevue/card'
import Tag from 'primevue/tag'

import { useApi } from '@/composables/useApi'

const version = __APP_VERSION__
const api = useApi()

const { data, isPending, isError } = useQuery({
  queryKey: ['health'],
  queryFn: async () => {
    const { data, error } = await api.GET('/health')
    if (error) throw error
    return data
  },
})
</script>

<template>
  <main class="min-h-screen flex items-center justify-center p-6">
    <Card class="w-full max-w-md">
      <template #title>memos-ng</template>
      <template #subtitle>Memos rebuilt on the default web stack — v{{ version }}</template>
      <template #content>
        <div class="flex items-center gap-2">
          <span>Backend health:</span>
          <Tag v-if="isPending" value="checking…" severity="info" data-test="health" />
          <Tag v-else-if="isError" value="unreachable" severity="danger" data-test="health" />
          <Tag v-else :value="data?.status ?? 'unknown'" severity="success" data-test="health" />
        </div>
      </template>
    </Card>
  </main>
</template>
