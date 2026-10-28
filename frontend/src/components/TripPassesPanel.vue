<script setup lang="ts">
import { useQuery } from '@tanstack/vue-query'
import Button from 'primevue/button'
import Drawer from 'primevue/drawer'
import { computed, ref } from 'vue'

import TripPassManager from '@/components/TripPassManager.vue'
import { useApi } from '@/composables/useApi'
import { detailOf, PassError } from '@/lib/tripPasses'

// The home SPA has no trip page of its own (trips are browsed in the iOS app), so the guest-pass
// sheet is reached from here: one row per trip, each opening the same sheet.
const api = useApi()

const { data: trips, isPending, isError } = useQuery({
  queryKey: ['collections', 'trip'],
  queryFn: async () => {
    const { data, error, response } = await api.GET('/api/collections', {
      params: { query: { kind: 'trip' } },
    })
    if (error || !data) throw new PassError(response.status, detailOf(error))
    return data
  },
})

const openUid = ref<string | null>(null)
const openTrip = computed(() => trips.value?.find((t) => t.uid === openUid.value) ?? null)
const visible = computed({
  get: () => openUid.value !== null,
  set: (v: boolean) => {
    if (!v) openUid.value = null
  },
})
</script>

<template>
  <div class="flex flex-col gap-2" data-test="trip-passes-panel">
    <h2 class="text-lg font-medium">Guest passes</h2>
    <p class="text-muted-color text-sm">
      Let guests add their pictures to a trip with a QR code. No account needed.
    </p>
    <p v-if="isPending" class="text-muted-color text-sm">Loading trips…</p>
    <p v-else-if="isError" class="text-red-600 text-sm">Could not load the trips.</p>
    <p v-else-if="!trips?.length" class="text-muted-color text-sm" data-test="trip-empty">
      No trips yet.
    </p>
    <ul v-else class="flex flex-col gap-1">
      <li
        v-for="t in trips"
        :key="t.uid"
        class="flex items-center justify-between gap-2 text-sm"
        data-test="trip-row"
        :data-uid="t.uid"
      >
        <span class="min-w-0 truncate">{{ t.title }}</span>
        <Button
          text
          size="small"
          icon="pi pi-qrcode"
          label="Guest passes"
          data-test="trip-open-passes"
          @click="openUid = t.uid"
        />
      </li>
    </ul>

    <Drawer
      v-model:visible="visible"
      position="right"
      :header="openTrip ? `Guest passes · ${openTrip.title}` : 'Guest passes'"
      class="w-full sm:w-[32rem]"
      data-test="trip-passes-drawer"
    >
      <TripPassManager v-if="openUid" :key="openUid" :collection-uid="openUid" />
    </Drawer>
  </div>
</template>
