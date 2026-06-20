<script setup lang="ts">
const version = __APP_VERSION__
</script>

<template>
  <section class="p-6" data-test="view-settings">
    <h1 class="text-2xl font-semibold">Settings</h1>
    <p class="text-muted-color mt-2">Profile, access tokens, members — coming in T3.5.</p>
    <p class="text-muted-color mt-4 text-sm" data-test="version">memos-ng v{{ version }}</p>
  </section>
</template>
