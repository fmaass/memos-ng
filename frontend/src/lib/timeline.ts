// Group memos into day buckets (preserving feed order) with friendly headers. Pure so the
// grouping is unit-tested; `now` is injectable for stable Today/Yesterday labels in tests.
export interface DayGroup<T> {
  key: string
  label: string
  items: T[]
}

function dayKey(d: Date): string {
  return d.toISOString().slice(0, 10)
}

export function groupByDay<T extends { created_at: string }>(
  memos: T[],
  now: Date = new Date(),
): DayGroup<T>[] {
  const todayKey = dayKey(now)
  const yesterdayKey = dayKey(new Date(now.getTime() - 86_400_000))
  const groups: DayGroup<T>[] = []
  const byKey = new Map<string, DayGroup<T>>()
  for (const memo of memos) {
    const key = dayKey(new Date(memo.created_at))
    let group = byKey.get(key)
    if (!group) {
      const label =
        key === todayKey
          ? 'Today'
          : key === yesterdayKey
            ? 'Yesterday'
            : new Date(memo.created_at).toLocaleDateString(undefined, {
                weekday: 'short',
                month: 'short',
                day: 'numeric',
                year: 'numeric',
              })
      group = { key, label, items: [] }
      byKey.set(key, group)
      groups.push(group)
    }
    group.items.push(memo)
  }
  return groups
}
