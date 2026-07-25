<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import FilterBar from '@/components/FilterBar.vue'
import MemoFeed from '@/components/MemoFeed.vue'
import { CONTENT_COLUMN } from '@/lib/layout'
import {
  filterFromSearchParams,
  filterToQuery,
  filterToRouteQuery,
  filterToSearchParams,
  isEmptyFilter,
  type MemoFilter,
} from '@/lib/memoFilter'

const route = useRoute()
const router = useRouter()

// Seed from the URL so a linked/reloaded filtered search restores exactly.
const filter = ref<MemoFilter>(filterFromSearchParams(route.query))

// Canonical string form of a filter — the loop guard for the two-way URL sync (equal filters
// serialize identically; a no-op change never triggers a router.replace or a re-seed).
function canon(f: MemoFilter): string {
  return filterToSearchParams(f).toString()
}

// filter -> URL: reflect the active filter into the address bar (linkable, reload-stable; the same
// serialization A6 stores). Guarded so an already-represented filter doesn't churn replace().
watch(
  filter,
  (f) => {
    if (canon(f) !== canon(filterFromSearchParams(route.query))) {
      void router.replace({ query: filterToRouteQuery(f) })
    }
  },
  { deep: true },
)

// URL -> filter: Vue Router reuses this component across query-only navigations (e.g. a sidebar
// tag link, back/forward, or the header search link while already on Search), so re-seed the
// filter whenever the URL diverges from it. Same guard prevents a feedback loop with the watcher
// above.
watch(
  () => route.query,
  () => {
    const fromUrl = filterFromSearchParams(route.query)
    if (canon(fromUrl) !== canon(filter.value)) filter.value = fromUrl
  },
)

const active = computed(() => !isEmptyFilter(filter.value))
const query = computed(() => filterToQuery(filter.value))
</script>

<template>
  <section class="mx-auto p-4 flex flex-col gap-3" :class="CONTENT_COLUMN" data-test="view-search">
    <h1 class="text-2xl font-semibold">Search</h1>
    <FilterBar v-model="filter" />
    <MemoFeed v-if="active" :query="query" />
    <p v-else class="text-muted-color text-sm" data-test="search-hint">
      Type or pick a filter to search your memos.
    </p>
  </section>
</template>
