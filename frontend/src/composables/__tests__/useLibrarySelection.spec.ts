import { effectScope, nextTick, ref } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'

import { useLibrarySelection } from '@/composables/useLibrarySelection'

type Item = { uid: string; mine: boolean }
const item = (uid: string, mine = true): Item => ({ uid, mine })

let scope = effectScope()
function setup(initial: Item[]) {
  scope = effectScope()
  const items = ref<Item[]>(initial)
  const selection = scope.run(() => useLibrarySelection(items))!
  return { items, selection }
}
const selected = (s: ReturnType<typeof setup>['selection']) => [...s.selected.value].sort()

describe('useLibrarySelection', () => {
  afterEach(() => scope.stop())

  it('starts outside select mode with nothing selected', () => {
    const { selection } = setup([item('a')])
    expect(selection.selecting.value).toBe(false)
    expect(selection.count.value).toBe(0)
  })

  it('toggles only while selecting', () => {
    const { selection } = setup([item('a')])
    selection.toggle('a')
    expect(selection.count.value).toBe(0)
    selection.enter()
    selection.toggle('a')
    expect(selected(selection)).toEqual(['a'])
    selection.toggle('a')
    expect(selection.count.value).toBe(0)
  })

  it('refuses a uid that is not a loaded item of mine, whatever string the caller hands it', () => {
    const { selection } = setup([item('a'), item('b', false)])
    selection.enter()
    selection.toggle('b')
    selection.toggle('zzz')
    expect(selection.count.value).toBe(0)
  })

  it('exit leaves select mode and clears the selection', () => {
    const { selection } = setup([item('a')])
    selection.enter()
    selection.toggle('a')
    selection.exit()
    expect(selection.selecting.value).toBe(false)
    expect(selection.count.value).toBe(0)
  })

  it('select-all takes every loaded mine uid, never a foreign one, and clears on the second call', () => {
    const { selection } = setup([item('a'), item('b', false), item('c')])
    selection.enter()
    expect(selection.allSelected.value).toBe(false)
    selection.selectAllLoaded()
    expect(selected(selection)).toEqual(['a', 'c'])
    expect(selection.allSelected.value).toBe(true)
    selection.selectAllLoaded()
    expect(selection.count.value).toBe(0)
    expect(selection.allSelected.value).toBe(false)
  })

  it('allSelected is false when nothing of mine is loaded', () => {
    const { selection } = setup([item('b', false)])
    selection.enter()
    selection.selectAllLoaded()
    expect(selection.allSelected.value).toBe(false)
  })

  it('a refetch that drops a selected uid drops it from the selection', async () => {
    const { items, selection } = setup([item('a'), item('b')])
    selection.enter()
    selection.toggle('a')
    selection.toggle('b')
    items.value = [item('b'), item('c')]
    await nextTick()
    expect(selected(selection)).toEqual(['b'])
  })

  it('capture returns a frozen copy in loaded order that later toggles do not move', () => {
    const { selection } = setup([item('a'), item('b'), item('c')])
    selection.enter()
    selection.toggle('c')
    selection.toggle('a')
    const batch = selection.capture()
    selection.toggle('b')
    expect(batch).toEqual(['a', 'c'])
    expect(Object.isFrozen(batch)).toBe(true)
  })
})
