<script setup lang="ts">
import type { PrintChip } from '@/lib/printMaster'

// The per-photo print-master chip, shared by the memo-card tile and the Resources gallery tile.
// One component because the FAILED state is the whole point: it is the only place a failed upload
// can be retried, and a tile that cannot show it strands the failure in a toast that is already
// gone — which for an orphan attachment (Resources) means no way back at all.
defineProps<{ chip: PrintChip }>()
const emit = defineEmits<{ retry: [] }>()

// The colour carries the same three-way reading as the text: stored, on its way, wants a click.
const CHIP_CLASS = {
  present: 'bg-emerald-600/90',
  uploading: 'bg-amber-600/90',
  failed: 'bg-red-700/95',
} as const
const BASE =
  'absolute bottom-1 left-1 z-10 rounded-full px-1.5 py-0.5 text-[0.62rem] font-bold tracking-wide text-white'
</script>

<template>
  <!-- The failed chip is a real button (and a SIBLING of any tile-open button — a control nested in
       a button is invalid markup and un-clickable on touch). -->
  <button
    v-if="chip.kind === 'failed'"
    type="button"
    :class="[BASE, CHIP_CLASS[chip.kind]]"
    data-test="print-chip"
    data-chip-kind="failed"
    aria-label="Print master upload failed — retry"
    @click.stop="emit('retry')"
  >
    {{ chip.label }}
  </button>
  <span
    v-else
    :class="[BASE, CHIP_CLASS[chip.kind]]"
    data-test="print-chip"
    :data-chip-kind="chip.kind"
  >
    {{ chip.label }}
  </span>
</template>
