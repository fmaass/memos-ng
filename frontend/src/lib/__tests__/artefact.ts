import { readFileSync } from 'node:fs'
import { join } from 'node:path'

/**
 * The ONE normative artefact (`fixtures/auto-memo-date.json`), for the specs that need a capture
 * instant, an order or a civil time. Not a test itself — a reader, so that no spec restates a rule
 * the artefact already owns and no two specs can drift apart on the same case.
 */

export interface VectorItem {
  id: string
  instant: string | null
  civil: string | null
}

export interface Artefact {
  vectors: {
    autoMemoDate: {
      id: string
      zone: string
      items: VectorItem[]
      expect: { instant: string | null; approximate: boolean; span: number }
    }[]
    captureOrder: { id: string; zone: string; items: VectorItem[]; expect: string[] }[]
    span: { id: string; zone: string; instants: string[]; expect: number }[]
    civilResolution: { id: string; civil: string; zone: string; expect: string | null }[]
  }
  binaries: Record<
    string,
    { mime: string; sha256: string; expect: Record<string, string | null> }
  >
}

export const artefact: Artefact = JSON.parse(
  readFileSync(join(import.meta.dirname, '../../../../fixtures/auto-memo-date.json'), 'utf8'),
)

export const dateVector = (id: string) => {
  const found = artefact.vectors.autoMemoDate.find((v) => v.id === id)
  if (!found) throw new Error(`no autoMemoDate vector "${id}" in the artefact`)
  return found
}

export const orderVector = (id: string) => {
  const found = artefact.vectors.captureOrder.find((v) => v.id === id)
  if (!found) throw new Error(`no captureOrder vector "${id}" in the artefact`)
  return found
}

export const civilVector = (id: string) => {
  const found = artefact.vectors.civilResolution.find((v) => v.id === id)
  if (!found) throw new Error(`no civilResolution vector "${id}" in the artefact`)
  return found
}

/** One item's instant, by vector id and item id — the specs' source for "a dated file". */
export function instantOf(vectorId: string, itemId: string): string {
  const item = dateVector(vectorId).items.find((i) => i.id === itemId)
  if (!item?.instant) throw new Error(`vector ${vectorId} has no dated item "${itemId}"`)
  return item.instant
}

/** A vector item as a file name — the specs attach files named after the item they stand for. */
export const fileNameOf = (item: VectorItem) => `${item.id}.jpg`
