import { computed, type MaybeRefOrGetter, ref, shallowRef, toValue, watch } from 'vue'

import { clear, prune, type Selection, selectAll, selectableFilter, toggle as toggleUid } from '@/lib/selection'

// The Library's select mode over the LOADED list. What may be selected is derived here, from the
// loaded items that say they are mine — never from uids a caller assembled — and a refetch that
// drops a uid drops it from the selection (the watcher below), so neither the count nor a batch can
// name an item that is no longer on screen.
export function useLibrarySelection(items: MaybeRefOrGetter<readonly { uid: string; mine?: boolean }[]>) {
  const selecting = ref(false)
  const selected = shallowRef<Selection>(clear())

  const eligible = computed(() => selectableFilter(toValue(items)).map((i) => i.uid))
  const eligibleSet = computed(() => new Set(eligible.value))

  watch(eligible, (uids) => {
    selected.value = prune(selected.value, uids)
  })

  const count = computed(() => selected.value.size)
  const allSelected = computed(
    () => eligible.value.length > 0 && eligible.value.every((uid) => selected.value.has(uid)),
  )

  function enter() {
    selecting.value = true
  }
  function exit() {
    selecting.value = false
    selected.value = clear()
  }
  function toggle(uid: string) {
    if (!selecting.value || !eligibleSet.value.has(uid)) return
    selected.value = toggleUid(selected.value, uid)
  }
  function selectAllLoaded() {
    if (!selecting.value) return
    selected.value = selectAll(selected.value, eligible.value)
  }
  // The batch an action works on: a frozen copy in the list's order, taken once, so a toggle while
  // a confirm is open or a delete is running can never change what that action touches.
  function capture(): readonly string[] {
    const sel = selected.value
    return Object.freeze(eligible.value.filter((uid) => sel.has(uid)))
  }

  return {
    selecting: computed(() => selecting.value),
    selected: computed(() => selected.value),
    count,
    allSelected,
    enter,
    exit,
    toggle,
    selectAllLoaded,
    capture,
  }
}
