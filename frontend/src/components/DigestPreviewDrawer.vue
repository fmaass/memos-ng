<script setup lang="ts">
import Drawer from 'primevue/drawer'
import SelectButton from 'primevue/selectbutton'
import { ref } from 'vue'

import type { DigestPreview } from '@/composables/useDigestActions'

defineProps<{
  visible: boolean
  preview: DigestPreview | null
  loading: boolean
  error: string | null
}>()

const emit = defineEmits<{ 'update:visible': [value: boolean] }>()

// The two shapes the mail is actually read in. A width, not a device: the preview is the same
// HTML either way, and narrowing it is what shows whether the layout survives a phone.
const widths = [
  { label: 'Desktop', value: 720 },
  { label: 'Mobile', value: 390 },
]
const width = ref(720)
</script>

<template>
  <Drawer
    :visible="visible"
    position="right"
    header="Preview"
    class="w-full sm:w-[46rem]"
    data-test="digest-preview-drawer"
    @update:visible="emit('update:visible', $event)"
  >
    <div class="flex flex-col gap-3">
      <div class="flex items-center justify-between gap-3">
        <SelectButton
          v-model="width"
          :options="widths"
          option-label="label"
          option-value="value"
          :allow-empty="false"
          aria-label="Preview width"
          data-test="digest-preview-width"
        />
        <p v-if="preview" class="text-sm text-muted-color" data-test="digest-preview-summary">
          {{ preview.memo_count }} memo{{ preview.memo_count === 1 ? '' : 's' }} ·
          {{ preview.parts }} part{{ preview.parts === 1 ? '' : 's' }}
        </p>
      </div>

      <p
        v-if="loading"
        class="text-sm text-muted-color"
        role="status"
        aria-live="polite"
        data-test="digest-preview-loading"
      >
        Rendering…
      </p>
      <p v-else-if="error" class="text-sm text-red-600" role="alert" data-test="digest-preview-error">
        {{ error }}
      </p>

      <template v-else-if="preview">
        <!-- sandbox="" : the rendered digest is memo content, so it is displayed with no script,
             no form and no same-origin access to this app. -->
        <iframe
          :srcdoc="preview.html"
          :style="{ width: `${width}px` }"
          class="h-[60vh] max-w-full border border-surface-200 dark:border-surface-700 rounded bg-white"
          sandbox=""
          title="Digest preview"
          data-test="digest-preview-frame"
        ></iframe>

        <div data-test="digest-preview-deferred">
          <h4 class="font-medium text-sm">Deferred</h4>
          <p
            v-if="!preview.deferred.length"
            class="text-sm text-muted-color"
            data-test="digest-preview-deferred-empty"
          >
            Nothing was held back.
          </p>
          <ul v-else class="text-sm flex flex-col gap-1 mt-1">
            <li
              v-for="item in preview.deferred"
              :key="item.uid"
              data-test="digest-preview-deferred-row"
            >
              <span class="font-mono text-xs">{{ item.uid }}</span>
              <span class="text-muted-color"> — {{ item.reason }}</span>
            </li>
          </ul>
        </div>

        <div data-test="digest-preview-selected">
          <h4 class="font-medium text-sm">Selected</h4>
          <p v-if="!preview.selected.length" class="text-sm text-muted-color">
            No memos would be sent.
          </p>
          <ul v-else class="text-sm flex flex-col gap-1 mt-1">
            <li
              v-for="item in preview.selected"
              :key="item.uid"
              data-test="digest-preview-selected-row"
            >
              <span class="font-mono text-xs">{{ item.uid }}</span>
              <span class="text-muted-color"> — {{ item.local_day }}</span>
            </li>
          </ul>
        </div>
      </template>
    </div>
  </Drawer>
</template>
