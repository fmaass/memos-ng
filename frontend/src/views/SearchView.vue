<script setup lang="ts">
import InputText from 'primevue/inputtext'
import { computed, onUnmounted, ref } from 'vue'

import MemoFeed from '@/components/MemoFeed.vue'
import type { MemoQuery } from '@/composables/useTimeline'

const term = ref('')
const debounced = ref('')
let timer: ReturnType<typeof setTimeout> | undefined

function onInput() {
  clearTimeout(timer)
  timer = setTimeout(() => {
    debounced.value = term.value.trim()
  }, 300)
}
onUnmounted(() => clearTimeout(timer))

const query = computed<MemoQuery>(() => {
  const q: MemoQuery = {}
  if (debounced.value) q.q = debounced.value
  return q
})
</script>

<template>
  <section class="max-w-2xl mx-auto p-4 flex flex-col gap-3" data-test="view-search">
    <h1 class="text-2xl font-semibold">Search</h1>
    <InputText
      v-model="term"
      placeholder="Search memos…"
      data-test="search-input"
      @input="onInput"
    />
    <MemoFeed v-if="debounced" :query="query" />
    <p v-else class="text-muted-color text-sm" data-test="search-hint">Type to search your memos.</p>
  </section>
</template>
