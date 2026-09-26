import { describe, expect, it } from 'vitest'

import { clear, prune, selectAll, selectableFilter, toggle } from '@/lib/selection'

const set = (...uids: string[]): ReadonlySet<string> => new Set(uids)
const sorted = (s: ReadonlySet<string>) => [...s].sort()

describe('selection reducer', () => {
  describe('toggle', () => {
    it('adds an unselected uid and removes a selected one', () => {
      expect(sorted(toggle(set(), 'a'))).toEqual(['a'])
      expect(sorted(toggle(set('a', 'b'), 'a'))).toEqual(['b'])
    })

    it('never mutates the set it was given', () => {
      const before = set('a')
      toggle(before, 'b')
      toggle(before, 'a')
      expect(sorted(before)).toEqual(['a'])
    })
  })

  it('clear is the empty selection', () => {
    expect(clear().size).toBe(0)
  })

  describe('prune', () => {
    it('drops a selected uid a refetch no longer loads, keeping the rest', () => {
      expect(sorted(prune(set('a', 'b', 'c'), ['a', 'c', 'd']))).toEqual(['a', 'c'])
    })

    it('returns the SAME set when nothing was dropped, so a watcher does not churn', () => {
      const s = set('a')
      expect(prune(s, ['a', 'b'])).toBe(s)
    })

    it('empties a selection when nothing is loaded', () => {
      expect(prune(set('a'), []).size).toBe(0)
    })
  })

  describe('selectableFilter', () => {
    it('keeps only the items that are mine', () => {
      const items = [
        { uid: 'a', mine: true },
        { uid: 'b', mine: false },
        { uid: 'c', mine: true },
      ]
      expect(selectableFilter(items).map((i) => i.uid)).toEqual(['a', 'c'])
    })

    it('treats an item without the flag as not mine', () => {
      const flagless: { uid: string; mine?: boolean }[] = [{ uid: 'a' }]
      expect(selectableFilter(flagless)).toEqual([])
    })

    it('is empty for an empty list', () => {
      expect(selectableFilter([])).toEqual([])
    })
  })

  describe('selectAll', () => {
    it('selects every loaded mine uid, adding to a partial selection', () => {
      expect(sorted(selectAll(set('a'), ['a', 'b', 'c']))).toEqual(['a', 'b', 'c'])
    })

    it('clears when every loaded mine uid is already selected', () => {
      expect(selectAll(set('a', 'b'), ['a', 'b']).size).toBe(0)
    })

    it('with one loaded mine uid selects it, then clears it', () => {
      const once = selectAll(set(), ['a'])
      expect(sorted(once)).toEqual(['a'])
      expect(selectAll(once, ['a']).size).toBe(0)
    })

    it('with nothing loaded stays empty', () => {
      expect(selectAll(set(), []).size).toBe(0)
    })
  })
})
