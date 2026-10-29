import { relative, sep } from 'node:path'
import type { Plugin } from 'vite'

/**
 * The guest page's build-isolation check (trip pass P4). Records every module id the guest build
 * loads — the entry's graph AND each worker's, because the plugin is registered in both — and
 * fails the build the moment an id falls under the member app: anything in `src/` outside
 * `src/guest/` (the router, views, stores, the TripPass owner components, useResumableUpload, the
 * member api client, the home entry) or a package only the member app may use. An allowlist of
 * the tree rather than a list of files, so a module added to the member app later is covered too.
 *
 * Registered with `enforce: 'pre'`, its `load` hook sees each id before any other plugin can
 * answer it; `buildEnd` refuses a graph that never contained the guest entry, so a hook that
 * silently stopped firing cannot pass as "nothing forbidden".
 */
export interface GuestIsolationOptions {
  /** The frontend root (the directory holding `src/`). */
  root: string
  /** A module id the graph must contain, or the check itself is broken. */
  mustSee: string
  /** Receives every id this build loaded, after a passing build (tests and the report). */
  onGraph?: (ids: string[]) => void
}

export const FORBIDDEN_PACKAGES = [
  'vite-plugin-pwa',
  'virtual:pwa-register',
  'workbox-',
  'vue-router',
  '@tanstack',
  'primevue',
  '@primeuix',
  'openapi-fetch',
] as const

export function forbiddenReason(id: string, root: string): string | null {
  const clean = id.replace(/^\0/, '').split('?')[0]
  for (const pkg of FORBIDDEN_PACKAGES) {
    if (clean.startsWith(pkg) || clean.includes(`/node_modules/${pkg}`)) {
      return `member-app package ${pkg}`
    }
  }
  const rel = relative(root, clean).split(sep).join('/')
  if (rel.startsWith('src/') && !rel.startsWith('src/guest/')) {
    return `member-app module ${rel}`
  }
  return null
}

export function guestIsolation(options: GuestIsolationOptions): Plugin {
  const seen = new Set<string>()
  return {
    name: 'memos-guest-isolation',
    enforce: 'pre',
    buildStart() {
      seen.clear()
    },
    load(id) {
      seen.add(id)
      const reason = forbiddenReason(id, options.root)
      if (reason !== null) {
        this.error(`guest build imports ${reason} (${id}); the guest page may not reach it`)
      }
      return null
    },
    buildEnd(error) {
      if (error) return
      const ids = [...seen]
      if (!ids.some((id) => id.split('?')[0] === options.mustSee)) {
        this.error(`guest isolation saw no ${options.mustSee}; the check did not run`)
      }
      options.onGraph?.(ids)
    },
  }
}
