<script setup lang="ts">
import SelectButton from 'primevue/selectbutton'
import { ref } from 'vue'

// The Library's segment in the dock: Years | Months | All. Only All exists in this phase; Years and
// Months are shown, disabled, so the dock already has its final shape.
const OPTIONS = [
  { label: 'Years', value: 'years', disabled: true },
  { label: 'Months', value: 'months', disabled: true },
  { label: 'All', value: 'all', disabled: false },
]
const view = ref('all')
</script>

<template>
  <SelectButton
    v-model="view"
    :options="OPTIONS"
    option-label="label"
    option-value="value"
    option-disabled="disabled"
    :allow-empty="false"
    aria-label="Library view"
    data-test="library-segment"
  />
</template>
