<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import { createGuestApi, type Limits, type Receipt } from './api'
import { memoryBindings, openBindings, type BindingStore } from './bindings'
import { CredentialStore } from './credential'
import { browserHasher } from './hasher'
import { acceptList, appUrl, extensionAccepted, isIOS } from './platform'
import { GuestSession } from './session'
import { GuestUploader, type UploadItem } from './uploader'

const tick = ref(0)
const bump = () => tick.value++
const api = createGuestApi()
const credentials = new CredentialStore(safeStorage())
const ios = isIOS(navigator.userAgent, navigator.maxTouchPoints ?? 0)
const session = new GuestSession(
  api,
  credentials,
  { path: () => location.pathname, replace: (path) => history.replaceState(null, '', path) },
  ios,
  bump,
)
const installUrl = appUrl(document)
const accept = acceptList(document)
const hasher = browserHasher()
const uploader = shallowRef<GuestUploader | null>(null)
const name = ref('')
const interrupted = ref(0)
const lastReceipt = ref<Receipt | null>(null)
const thumbs = new Map<number, string>()
let wakeLock: { release(): Promise<void> } | null = null

function safeStorage(): Storage | null {
  try {
    return window.localStorage
  } catch {
    return null
  }
}

const phase = computed(() => (tick.value, session.phase))
const items = computed<UploadItem[]>(() => (tick.value, uploader.value ? uploader.value.items.map((i) => ({ ...i })) : []))
const total = computed(() => items.value.length)
const received = computed(() => items.value.filter((i) => i.state === 'received').length)
const busy = computed(() => (tick.value, uploader.value?.inFlight ?? false))

let starting: { epoch: number; run: Promise<void> } | null = null

// ONE uploader per credential: a second Join, or a boot racing a join, JOINS the start in flight
// for the same credential epoch. A new credential retires the old uploader before its own starts,
// and a start that a newer credential overtook installs nothing.
function startUploader(): Promise<void> {
  const credential = credentials.get()
  if (!credential || !session.limits || !session.deviceId) return Promise.resolve()
  if (starting?.epoch === credential.epoch) return starting.run
  uploader.value?.reset()
  uploader.value = null
  const run = buildUploader(credential.epoch, session.limits, session.deviceId)
  starting = { epoch: credential.epoch, run }
  return run
}

async function buildUploader(epoch: number, limits: Limits, deviceId: string): Promise<void> {
  const current = () => starting?.epoch === epoch && credentials.get()?.epoch === epoch
  let opened: BindingStore
  try {
    opened = await openBindings()
  } catch {
    opened = memoryBindings()
  }
  if (!current()) return
  const count = (await opened.list(deviceId)).length
  if (!current()) return
  interrupted.value = count
  uploader.value = new GuestUploader({
    api,
    credentials,
    bindings: opened,
    hash: (file, onProgress) => hasher.hash(file, onProgress),
    limits,
    deviceId,
    onChange: () => {
      bump()
      void holdWakeLock()
    },
    onRefused: (status) => {
      session.phase = 'ended'
      session.message = status === 410 ? 'This pass is no longer open.' : 'This device is no longer part of the pass.'
      bump()
    },
    onReceipt: (receipt) => {
      lastReceipt.value = receipt
      interrupted.value = 0
    },
  })
  await uploader.value.resumePending()
  bump()
}

async function join(): Promise<void> {
  await session.redeem(name.value)
  if (session.phase === 'ready') await startUploader()
}

function pick(event: Event): void {
  const input = event.target as HTMLInputElement
  const files = [...(input.files ?? [])]
  input.value = ''
  if (!uploader.value || !files.length) return
  uploader.value.add(files)
  for (const item of uploader.value.items) {
    if (!thumbs.has(item.id) && item.file.type.startsWith('image/') && !/hei[cf]/i.test(item.file.type)) {
      thumbs.set(item.id, URL.createObjectURL(item.file))
    }
  }
  bump()
}

function label(item: UploadItem): string {
  switch (item.state) {
    case 'waiting':
      return 'Waiting'
    case 'hashing':
      return `Checking ${pct(item.hashed, item.size)}`
    case 'checking':
      return item.recognised ? 'Recognised, checking' : 'Checking'
    case 'sending':
      return `${item.resumed ? 'Resuming' : 'Sending'} ${pct(item.sent, item.size)}`
    case 'uploaded':
      return 'Sent, confirming'
    case 'received':
      return 'Received'
    case 'unsupported':
      return 'This file type is not accepted'
    case 'too-large':
      return 'Too large'
    case 'damaged':
      return 'Arrived damaged twice; not sent'
    case 'quota':
      return 'The pass is full'
    default:
      return item.message ?? 'Failed'
  }
}

function pct(done: number, size: number): string {
  return `${size ? Math.floor((100 * done) / size) : 100} %`
}

async function holdWakeLock(): Promise<void> {
  const nav = navigator as Navigator & { wakeLock?: { request(type: 'screen'): Promise<{ release(): Promise<void> }> } }
  if (busy.value && !wakeLock && nav.wakeLock && document.visibilityState === 'visible') {
    try {
      wakeLock = await nav.wakeLock.request('screen')
    } catch {
      wakeLock = null // refused (battery saver): uploading goes on without it
    }
  } else if (!busy.value && wakeLock) {
    const held = wakeLock
    wakeLock = null
    await held.release().catch(() => {})
  }
}

function onVisible(): void {
  wakeLock = null // the system drops the lock when the page hides
  void holdWakeLock()
}

function onBeforeUnload(event: BeforeUnloadEvent): void {
  if (!busy.value) return
  event.preventDefault()
  event.returnValue = ''
}

onMounted(async () => {
  window.addEventListener('beforeunload', onBeforeUnload)
  document.addEventListener('visibilitychange', onVisible)
  await session.boot()
  if (session.phase === 'ready') await startUploader()
})

onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', onBeforeUnload)
  document.removeEventListener('visibilitychange', onVisible)
  for (const url of thumbs.values()) URL.revokeObjectURL(url)
})
</script>

<template>
  <main class="guest">
    <h1>Trip Pass</h1>

    <p v-if="phase === 'boot'" class="muted">Loading…</p>

    <section v-else-if="phase === 'landing'">
      <p>Add your pictures to the shared trip.</p>
      <a v-if="installUrl" class="button primary" :href="installUrl" rel="noreferrer noopener">Get memos</a>
      <p v-if="installUrl" class="muted">Then open this link again on this iPhone.</p>
      <button type="button" class="button" :class="{ primary: !installUrl }" @click="session.chooseBrowser()">
        Upload in the browser instead
      </button>
      <p class="muted small">
        In the browser, an iPhone may send converted copies rather than the originals. Originals are
        promised on Android for now.
      </p>
    </section>

    <form v-else-if="phase === 'name'" @submit.prevent="join">
      <label for="guest-name">Your name, so the family knows who sent what</label>
      <input id="guest-name" v-model="name" maxlength="64" autocomplete="nickname" required />
      <button type="submit" class="button primary" :disabled="!name.trim()">Join</button>
      <p v-if="session.message" class="error">{{ session.message }}</p>
    </form>

    <section v-else-if="phase === 'ready'">
      <p>
        <strong>{{ session.pass?.label }}</strong> — sending as {{ session.displayName }}
      </p>
      <p v-if="interrupted && !items.length" class="notice">
        {{ interrupted }} picture(s) were interrupted. Select the same pictures again to continue
        where they stopped.
      </p>
      <p v-if="!uploader" class="muted">Preparing…</p>
      <label v-else class="button primary picker">
        Choose pictures and videos
        <input type="file" multiple :accept="accept || undefined" @change="pick" />
      </label>
      <p v-if="!session.durable" class="notice">
        This browser cannot remember this pass. If this tab closes, open the pass link again;
        pictures that are not received yet will start over.
      </p>
      <p class="muted small">Keep this page open while it sends.</p>
      <p v-if="total" class="summary">{{ received }} of {{ total }} received</p>
      <p v-if="lastReceipt?.status === 'held'" class="notice">The last batch is held for review.</p>
      <ul class="files">
        <li v-for="item in items" :key="item.id" :class="item.state">
          <img v-if="thumbs.get(item.id)" :src="thumbs.get(item.id)" alt="" />
          <span v-else class="placeholder" aria-hidden="true"></span>
          <span class="name">{{ item.name }}</span>
          <span class="state">{{ label(item) }}</span>
          <progress v-if="item.state === 'sending'" :max="item.size" :value="item.sent"></progress>
          <button v-if="item.state === 'failed'" type="button" class="button small" @click="uploader?.retry(uploader.items.find((i) => i.id === item.id)!)">
            Retry
          </button>
        </li>
      </ul>
      <p v-if="items.some((i) => extensionAccepted(i.name, accept) === false)" class="muted small">
        Some files are of a type the trip does not accept.
      </p>
      <button v-if="session.otherSecret" type="button" class="button" @click="session.joinOther()">
        Join the link you just opened instead
      </button>
    </section>

    <section v-else>
      <p class="error">{{ session.message ?? 'Open the pass link you were given to join.' }}</p>
    </section>
  </main>
</template>
