<script setup lang="ts">
import Button from 'primevue/button'
import Tag from 'primevue/tag'

import { usePassDevices } from '@/composables/useTripPasses'

const props = defineProps<{
  passUid: string
  /** False once the pass is revoked or expired: its devices can no longer be revoked one by one. */
  live: boolean
  revoke: (deviceUid: string) => unknown
  busy: (deviceUid: string) => boolean
}>()

const { data: devices, isPending, isError } = usePassDevices(() => props.passUid)

function megabytes(bytes: number): string {
  return `${(bytes / 1_000_000).toFixed(1)} MB`
}
function when(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : 'never'
}
</script>

<template>
  <div class="flex flex-col gap-1 pl-2" data-test="pass-devices">
    <p v-if="isPending" class="text-muted-color text-xs">Loading devices…</p>
    <p v-else-if="isError" class="text-red-600 text-xs">Could not load the devices.</p>
    <p v-else-if="!devices?.length" class="text-muted-color text-xs" data-test="device-empty">
      No device has joined yet.
    </p>
    <ul v-else class="flex flex-col gap-1">
      <li
        v-for="d in devices"
        :key="d.uid"
        class="flex items-center justify-between gap-2 text-xs"
        data-test="device-row"
        :data-uid="d.uid"
      >
        <span class="min-w-0">
          <span class="font-medium">{{ d.displayName }}</span>
          <span class="text-muted-color">
            · {{ d.platform }} · {{ d.uploadsCount }} uploads, {{ megabytes(d.bytesTotal) }} · last
            seen {{ when(d.lastSeen) }}
          </span>
        </span>
        <Tag v-if="d.revoked" value="Revoked" severity="secondary" />
        <Button
          v-else-if="live"
          text
          size="small"
          severity="danger"
          label="Revoke"
          :loading="busy(d.uid)"
          data-test="device-revoke"
          :data-uid="d.uid"
          @click="revoke(d.uid)"
        />
      </li>
    </ul>
  </div>
</template>
