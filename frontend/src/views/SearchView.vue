<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import FilterBar from '@/components/FilterBar.vue'
import MemoFeed from '@/components/MemoFeed.vue'
import {
  filterFromSearchParams,
  filterToQuery,
  filterToRouteQuery,
  isEmptyFilter,
  type MemoFilter,
} from '@/lib/memoFilter'

const route = useRoute()
const router = useRouter()

// Seed from the URL so a linked/reloaded filtered search restores exactly.
const filter = ref<MemoFilter>(filterFromSearchParams(route.query))

// Reflect the active filter into the URL (linkable + reload-stable), same serialization A6 stores.
watch(
  filter,
  (f) => {
    void router.replace({ query: filterToRouteQuery(f) })
  },
  { deep: true },
)

const active = computed(() => !isEmptyFilter(filter.value))
const query = computed(() => filterToQuery(filter.value))
</script>

<template>
  <section class="max-w-2xl mx-auto p-4 flex flex-col gap-3" data-test="view-search">
    <h1 class="text-2xl font-semibold">Search</h1>
    <FilterBar v-model="filter" />
    <MemoFeed v-if="active" :query="query" />
    <p v-else class="text-muted-color text-sm" data-test="search-hint">
      Type or pick a filter to search your memos.
    </p>
  </section>
</template>
