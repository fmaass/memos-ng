<script setup lang="ts">
import MemoFeed from '@/components/MemoFeed.vue'
import { CONTENT_COLUMN } from '@/lib/layout'
</script>

<template>
  <section class="mx-auto p-4 flex flex-col gap-3" :class="CONTENT_COLUMN" data-test="view-explore">
    <h1 class="text-2xl font-semibold">Explore</h1>
    <p class="text-muted-color text-sm">Public memos shared across the household.</p>
    <MemoFeed :query="{ visibility: 'PUBLIC' }" />
  </section>
</template>
