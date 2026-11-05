import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { computed, type MaybeRefOrGetter, ref, shallowRef, toValue } from 'vue'

import { useApi } from '@/composables/useApi'
import {
  detailOf,
  PassError,
  type PassCreate,
  type PassDevice,
  type PassIssued,
  type PassOut,
} from '@/lib/tripPasses'

export const passesKey = (collectionUid: string) => ['trip-passes', collectionUid] as const
export const devicesKey = (passUid: string) => ['pass-devices', passUid] as const

/**
 * The passes of one trip and the three things a member does to them: issue, revoke one device,
 * revoke the whole pass.
 *
 * Each operation serializes itself by JOINING: a second call for the same target while the first
 * is on the wire gets the first one's promise, so a double click is one request. The issued
 * secret lives only in this instance's memory and is shown only for the trip that asked for it —
 * an answer that lands after the sheet moved to another trip is not shown there.
 */
export function useTripPasses(collectionUid: MaybeRefOrGetter<string>) {
  const api = useApi()
  const queryClient = useQueryClient()
  const uid = computed(() => toValue(collectionUid))

  const passes = useQuery({
    queryKey: computed(() => passesKey(uid.value)),
    queryFn: async (): Promise<PassOut[]> => {
      const { data, error, response } = await api.GET('/api/collections/{uid}/passes', {
        params: { path: { uid: uid.value } },
      })
      if (error || !data) throw new PassError(response.status, detailOf(error))
      return data
    },
  })

  const issued = shallowRef<{ for: string; value: PassIssued } | null>(null)
  /** The just-issued pass with its secret URL and QR, for the trip currently shown, else null. */
  const issuedHere = computed(() =>
    issued.value && issued.value.for === uid.value ? issued.value.value : null,
  )
  const error = ref<string | null>(null)
  const inflight = ref(new Map<string, Promise<unknown>>())

  function joined<T>(key: string, run: () => Promise<T>): Promise<T> {
    const existing = inflight.value.get(key)
    if (existing) return existing as Promise<T>
    const p = run().finally(() => {
      const next = new Map(inflight.value)
      next.delete(key)
      inflight.value = next
    })
    inflight.value = new Map(inflight.value).set(key, p)
    return p
  }

  const isBusy = (key: string) => inflight.value.has(key)
  const issueKey = () => `issue:${uid.value}`
  const passKey = (passUid: string) => `pass:${passUid}`
  const deviceKey = (passUid: string, deviceUid: string) => `device:${passUid}/${deviceUid}`

  function refresh(collection: string, passUid?: string) {
    void queryClient.invalidateQueries({ queryKey: passesKey(collection) })
    if (passUid) void queryClient.invalidateQueries({ queryKey: devicesKey(passUid) })
  }

  function fail(forUid: string, err: unknown) {
    if (forUid !== uid.value) return
    error.value = err instanceof Error ? err.message : String(err)
  }

  /** Issue a pass; resolves to the issued pass, or null when it was refused. */
  function issue(body: PassCreate): Promise<PassIssued | null> {
    const forUid = uid.value
    return joined(issueKey(), async () => {
      error.value = null
      try {
        const { data, error: e, response } = await api.POST('/api/collections/{uid}/passes', {
          params: { path: { uid: forUid } },
          body,
        })
        if (e || !data) throw new PassError(response.status, detailOf(e))
        if (forUid === uid.value) issued.value = { for: forUid, value: data }
        refresh(forUid)
        return data
      } catch (err) {
        fail(forUid, err)
        return null
      }
    })
  }

  /** The sheet must not keep offering a QR whose pass no longer works. */
  function dropIssued(passUid: string) {
    if (issued.value?.value.pass.uid === passUid) issued.value = null
  }

  /** Ask the door to lift a suspension; the sheet learns the result from a later sync. */
  function unsuspendPass(passUid: string): Promise<boolean> {
    const forUid = uid.value
    return joined(passKey(passUid), async () => {
      error.value = null
      try {
        const { error: e, response } = await api.POST('/api/passes/{uid}/unsuspend', {
          params: { path: { uid: passUid } },
        })
        if (e) throw new PassError(response.status, detailOf(e))
        refresh(forUid)
        return true
      } catch (err) {
        fail(forUid, err)
        return false
      }
    })
  }

  /** Revoke the pass and every device on it. Resolves true once the server says it is revoked. */
  function revokePass(passUid: string): Promise<boolean> {
    const forUid = uid.value
    return joined(passKey(passUid), async () => {
      error.value = null
      try {
        const { error: e, response } = await api.DELETE('/api/passes/{uid}', {
          params: { path: { uid: passUid } },
        })
        if (e) throw new PassError(response.status, detailOf(e))
        dropIssued(passUid)
        return true
      } catch (err) {
        // Revoked by someone else meanwhile: the pass is just as dead as if this call had won.
        if (err instanceof PassError && err.status === 410 && err.detail === 'pass_revoked') {
          dropIssued(passUid)
        }
        fail(forUid, err)
        return false
      } finally {
        refresh(forUid, passUid)
      }
    })
  }

  /** Revoke one device; the pass and its other devices keep working. */
  function revokeDevice(passUid: string, deviceUid: string): Promise<boolean> {
    const forUid = uid.value
    return joined(deviceKey(passUid, deviceUid), async () => {
      error.value = null
      try {
        const { error: e, response } = await api.DELETE('/api/passes/{uid}/devices/{device_uid}', {
          params: { path: { uid: passUid, device_uid: deviceUid } },
        })
        if (e) throw new PassError(response.status, detailOf(e))
        return true
      } catch (err) {
        fail(forUid, err)
        return false
      } finally {
        refresh(forUid, passUid)
      }
    })
  }

  function dismissIssued() {
    issued.value = null
  }

  return {
    passes,
    issuedHere,
    error,
    issue,
    revokePass,
    unsuspendPass,
    revokeDevice,
    dismissIssued,
    issuing: computed(() => isBusy(issueKey())),
    revokingPass: (passUid: string) => isBusy(passKey(passUid)),
    revokingDevice: (passUid: string, deviceUid: string) => isBusy(deviceKey(passUid, deviceUid)),
  }
}

/** The guest devices enrolled on one pass, revoked ones included. */
export function usePassDevices(passUid: MaybeRefOrGetter<string>) {
  const api = useApi()
  const uid = computed(() => toValue(passUid))
  return useQuery({
    queryKey: computed(() => devicesKey(uid.value)),
    queryFn: async (): Promise<PassDevice[]> => {
      const { data, error, response } = await api.GET('/api/passes/{uid}/devices', {
        params: { path: { uid: uid.value } },
      })
      if (error || !data) throw new PassError(response.status, detailOf(error))
      return data
    },
  })
}

/** Quarantined guest batches the owner has not looked at yet. Resolving files nothing, deletes
 * nothing and sends nothing to the door. */
export function useTripQuarantine() {
  const api = useApi()
  const queryClient = useQueryClient()
  const quarantine = useQuery({
    queryKey: ['trip-quarantine'],
    queryFn: async () => {
      const { data, error, response } = await api.GET('/api/trip-ingest/quarantine')
      if (error || !data) throw new PassError(response.status, detailOf(error))
      return data
    },
  })
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['trip-quarantine'] })
  async function resolveBatch(batchUid: string): Promise<void> {
    await api.POST('/api/trip-ingest/quarantine/{batch_uid}/resolve', {
      params: { path: { batch_uid: batchUid } },
    })
    await refresh()
  }
  async function clearUnknown(through: number): Promise<void> {
    await api.POST('/api/trip-ingest/quarantine/unknown/resolve', { body: { through } })
    await refresh()
  }
  return { quarantine, resolveBatch, clearUnknown }
}
