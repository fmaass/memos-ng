<script setup lang="ts">
import SelectButton from 'primevue/selectbutton'
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { type LibraryView, viewFromQuery, withView } from '@/lib/libraryFilter'

// The Library's segment in the dock: Years | Months | All. The dock is the shell's, so the pill
// shares no component state with the Library view: the route query's `view` is the one place the
// segment lives, read here and written back with the filter fields kept.
const OPTIONS: { label: string; value: LibraryView }[] = [
  { label: 'Years', value: 'years' },
  { label: 'Months', value: 'months' },
  { label: 'All', value: 'all' },
]
const route = useRoute()
const router = useRouter()
const view = computed<LibraryView>({
  get: () => viewFromQuery(route.query),
  set: (next) => {
    if (next !== viewFromQuery(route.query)) void router.replace({ query: withView(route.query, next) })
  },
})
</script>

<template>
  <SelectButton
    v-model="view"
    :options="OPTIONS"
    option-label="label"
    option-value="value"
    :allow-empty="false"
    aria-label="Library view"
    data-test="library-segment"
  />
</template>
