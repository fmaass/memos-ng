<script setup lang="ts">
import { computed, ref } from 'vue'

import type { DigestRun } from '@/lib/digest'

const props = defineProps<{ runs: DigestRun[] | undefined }>()

const rows = computed(() => props.runs ?? [])
// One open detail row at a time — the errors are long sentences, not chips.
const openId = ref<number | null>(null)

function toggle(id: number) {
  openId.value = openId.value === id ? null : id
}

function statusLabel(run: DigestRun): string {
  return run.status ?? 'running'
}

function statusClass(run: DigestRun): string {
  if (run.status === null) return 'text-sky-600'
  if (run.status === 'sent') return 'text-green-600'
  if (run.status === 'failed') return 'text-red-600'
  return 'text-muted-color'
}

function clock(value: string | null): string {
  if (!value) return '—'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString()
}
</script>

<template>
  <div class="flex flex-col gap-2" data-test="digest-runs">
    <div>
      <h3 class="font-medium">Runs</h3>
      <p class="text-sm text-muted-color">
        The last {{ rows.length }} runs, newest first. Open a row for what it recorded.
      </p>
    </div>

    <p v-if="!rows.length" class="text-sm text-muted-color" data-test="digest-runs-empty">
      No runs recorded yet.
    </p>

    <div v-else class="overflow-x-auto">
      <table class="w-full text-sm">
        <thead class="text-left text-muted-color">
          <tr>
            <th class="py-1 pr-3 font-normal">Day</th>
            <th class="py-1 pr-3 font-normal">Status</th>
            <th class="py-1 pr-3 font-normal">Trigger</th>
            <th class="py-1 pr-3 font-normal">Memos</th>
            <th class="py-1 pr-3 font-normal">Parts</th>
            <th class="py-1 font-normal">To</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="run in rows" :key="run.id">
            <tr
              class="cursor-pointer border-t border-surface-200 dark:border-surface-700"
              data-test="digest-run-row"
              @click="toggle(run.id)"
            >
              <td class="py-1 pr-3">{{ run.local_date }}</td>
              <td class="py-1 pr-3" :class="statusClass(run)" data-test="digest-run-status">
                {{ statusLabel(run) }}
              </td>
              <td class="py-1 pr-3">{{ run.trigger }}</td>
              <td class="py-1 pr-3">{{ run.memos_sent }}</td>
              <td class="py-1 pr-3">{{ run.parts_sent }}/{{ run.parts }}</td>
              <td class="py-1">{{ run.recipients }}</td>
            </tr>
            <tr v-if="openId === run.id" data-test="digest-run-detail">
              <td colspan="6" class="py-2 text-xs text-muted-color">
                <p>Started {{ clock(run.started_at) }} · finished {{ clock(run.finished_at) }}</p>
                <p>Parts claimed {{ run.parts_claimed }} of {{ run.parts }}</p>
                <p v-if="run.error" class="text-red-600 break-words" data-test="digest-run-error">
                  {{ run.error }}
                </p>
                <p v-else data-test="digest-run-no-error">No error recorded.</p>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
  </div>
</template>
