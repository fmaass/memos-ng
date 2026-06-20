<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import { ref } from 'vue'

import { useApi } from '@/composables/useApi'

const api = useApi()
const queryClient = useQueryClient()

const { data: pats } = useQuery({
  queryKey: ['pats'],
  queryFn: async () => {
    const { data, error } = await api.GET('/api/pats')
    if (error) throw error
    return data
  },
})

const open = ref(false)
const name = ref('')
const createdToken = ref<string | null>(null)
const copied = ref(false)

const create = useMutation({
  mutationFn: async (patName: string) => {
    const { data, error } = await api.POST('/api/pats', { body: { name: patName } })
    if (error) throw error
    return data
  },
  onSuccess: (data) => {
    createdToken.value = data?.token ?? null // shown exactly once
    name.value = ''
    queryClient.invalidateQueries({ queryKey: ['pats'] })
  },
})

const revoke = useMutation({
  mutationFn: async (id: number) => {
    const { error } = await api.DELETE('/api/pats/{pat_id}', { params: { path: { pat_id: id } } })
    if (error) throw error
  },
  onSuccess: () => queryClient.invalidateQueries({ queryKey: ['pats'] }),
})

function openCreate() {
  createdToken.value = null
  copied.value = false
  name.value = ''
  open.value = true
}
function submit() {
  if (name.value.trim()) create.mutate(name.value.trim())
}
async function copyToken() {
  if (createdToken.value && navigator.clipboard) {
    await navigator.clipboard.writeText(createdToken.value)
    copied.value = true
  }
}
</script>

<template>
  <div class="flex flex-col gap-2" data-test="pat-manager">
    <div class="flex items-center justify-between">
      <h2 class="text-lg font-medium">Access tokens</h2>
      <Button
        label="New token"
        icon="pi pi-plus"
        size="small"
        data-test="pat-new"
        @click="openCreate"
      />
    </div>

    <p v-if="!pats?.length" class="text-muted-color text-sm" data-test="pat-empty">
      No access tokens yet.
    </p>
    <ul v-else class="flex flex-col gap-1">
      <li
        v-for="p in pats"
        :key="p.id"
        class="flex items-center justify-between text-sm"
        data-test="pat-row"
      >
        <span>{{ p.name }}</span>
        <Button
          text
          size="small"
          icon="pi pi-trash"
          aria-label="Revoke"
          data-test="pat-revoke"
          @click="revoke.mutate(p.id)"
        />
      </li>
    </ul>

    <div
      v-if="open"
      class="border border-surface-200 dark:border-surface-700 rounded-lg p-3 flex flex-col gap-2"
      data-test="pat-create-panel"
    >
      <template v-if="!createdToken">
        <InputText v-model="name" placeholder="Token name" data-test="pat-name" />
        <Button label="Create" data-test="pat-create" @click="submit" />
      </template>
      <template v-else>
        <p class="text-sm text-amber-600" data-test="pat-warning">
          Copy this token now — it won't be shown again.
        </p>
        <div class="flex items-center gap-2">
          <code
            class="flex-1 break-all bg-surface-100 dark:bg-surface-800 p-2 rounded"
            data-test="pat-token"
            >{{ createdToken }}</code
          >
          <Button icon="pi pi-copy" aria-label="Copy" data-test="pat-copy" @click="copyToken" />
        </div>
        <span v-if="copied" class="text-xs text-green-600" data-test="pat-copied">Copied!</span>
      </template>
    </div>
  </div>
</template>
