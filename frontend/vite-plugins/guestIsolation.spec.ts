// @vitest-environment node
import vue from '@vitejs/plugin-vue'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'vite'
import { afterAll, describe, expect, it } from 'vitest'
import { guestIsolation } from './guestIsolation'

// The guest build's module-graph isolation, proven both ways: the real entry (and its worker)
// passes and its recorded graph is real; an entry or a worker that reaches a member-app module
// fails the build.
const root = fileURLToPath(new URL('../', import.meta.url))
mkdirSync(join(root, 'node_modules/.tmp'), { recursive: true })
const scratch = mkdtempSync(join(root, 'node_modules/.tmp/guest-isolation-'))
afterAll(() => rmSync(scratch, { recursive: true, force: true }))

async function buildEntry(entry: string, worker = false) {
  const graph: string[] = []
  const workerGraph: string[] = []
  await build({
    configFile: false,
    root,
    logLevel: 'silent',
    publicDir: false,
    plugins: [vue(), guestIsolation({ root, mustSee: entry, onGraph: (ids) => graph.push(...ids) })],
    worker: {
      format: 'iife',
      plugins: () => [guestIsolation({ root, mustSee: worker ? join(scratch, 'w.ts') : `${root}src/guest/hashWorker.ts`, onGraph: (ids) => workerGraph.push(...ids) })],
    },
    build: { write: false, rollupOptions: { input: entry }, modulePreload: { polyfill: false } },
  })
  return { graph, workerGraph }
}

describe('guest build isolation', () => {
  it('passes the real entry and records its graph, the worker included', async () => {
    const { graph, workerGraph } = await buildEntry(`${root}src/guest/main.ts`)
    expect(graph).toContain(`${root}src/guest/uploader.ts`)
    expect(graph.some((id) => id.includes('/node_modules/vue/') || id.includes('/node_modules/@vue/'))).toBe(true)
    expect(workerGraph).toContain(`${root}src/guest/digest.ts`)
    expect(workerGraph.some((id) => id.includes('@noble/hashes'))).toBe(true)
    expect(graph.some((id) => id.startsWith(`${root}src/`) && !id.startsWith(`${root}src/guest/`))).toBe(false)
  }, 120_000)

  it('fails an entry that imports a member-app module', async () => {
    const entry = join(scratch, 'entry.ts')
    writeFileSync(entry, `import '${root}src/composables/useResumableUpload.ts'\nconsole.log(1)\n`)
    await expect(buildEntry(entry)).rejects.toThrow(/member-app module src\/composables\/useResumableUpload\.ts/)
  }, 120_000)

  it('fails a worker that imports a member-app module', async () => {
    const entry = join(scratch, 'entry2.ts')
    writeFileSync(join(scratch, 'w.ts'), `import '${root}src/router.ts'\nself.postMessage(1)\n`)
    writeFileSync(entry, `new Worker(new URL('./w.ts', import.meta.url), { type: 'module' })\n`)
    await expect(buildEntry(entry, true)).rejects.toThrow(/member-app module src\/router\.ts/)
  }, 120_000)

  it('fails an entry that imports a member-only package', async () => {
    const entry = join(scratch, 'entry3.ts')
    writeFileSync(entry, `import 'vue-router'\nconsole.log(1)\n`)
    await expect(buildEntry(entry)).rejects.toThrow(/member-app package vue-router/)
  }, 120_000)
})
