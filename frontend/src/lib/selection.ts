// The Library's multi-select as a pure reducer over an IMMUTABLE set of attachment uids: every
// operation returns a new set (or the same one when nothing changed), so a caller holding the set in
// a shallowRef sees each change and a batch captured from it can never move under its reader.

export type Selection = ReadonlySet<string>

export function toggle(sel: Selection, uid: string): Selection {
  const next = new Set(sel)
  if (next.has(uid)) next.delete(uid)
  else next.add(uid)
  return next
}

export function clear(): Selection {
  return new Set()
}

// A refetch can drop an item (deleted elsewhere, filtered out): a uid that is no longer loaded is no
// longer selected, so the count and the batch only ever name what is on screen.
export function prune(sel: Selection, loadedUids: Iterable<string>): Selection {
  const loaded = new Set(loadedUids)
  let dropped = false
  const next = new Set<string>()
  for (const uid of sel) {
    if (loaded.has(uid)) next.add(uid)
    else dropped = true
  }
  return dropped ? next : sel
}

// Only the owner's own items can be deleted (the server refuses the rest), so only they are
// selectable. An item that does not say it is mine is not.
export function selectableFilter<T extends { mine?: boolean }>(items: readonly T[]): T[] {
  return items.filter((item) => item.mine === true)
}

// The header toggle: select every loaded mine uid, or — when every one of them already is — clear.
export function selectAll(sel: Selection, loadedMineUids: readonly string[]): Selection {
  if (loadedMineUids.every((uid) => sel.has(uid))) return clear()
  return new Set([...sel, ...loadedMineUids])
}
