// Build a GitHub-style activity grid, each day with its memo count and a 0–4 intensity level.
// The window spans from the FIRST day with activity up to today, capped at `weeks` weeks — so a
// young household isn't shown weeks of empty cells before its first memo (the grid grows over time
// and pins to `weeks` once history is long enough). With no activity at all, the full `weeks`
// window is shown (the cold-start grid). Pure so the bucketing/window are unit-tested; `today`
// is injectable. Cells are ordered oldest→newest (column-major weeks).
export interface HeatCell {
  date: string // YYYY-MM-DD
  count: number
  level: 0 | 1 | 2 | 3 | 4
}

function dayKey(d: Date): string {
  return d.toISOString().slice(0, 10)
}

function levelFor(count: number): HeatCell['level'] {
  if (count <= 0) return 0
  if (count === 1) return 1
  if (count <= 3) return 2
  if (count <= 6) return 3
  return 4
}

export function buildHeatmap(
  counts: Record<string, number>,
  today: Date = new Date(),
  weeks = 12,
): HeatCell[] {
  const DAY = 86_400_000
  const endMs = Date.parse(dayKey(today) + 'T00:00:00Z') // today at UTC midnight
  // The furthest back we ever reach: `weeks` weeks ago. Then clamp the start FORWARD to the first
  // day that has activity, so leading empty weeks (before the household's first memo) aren't shown.
  let startMs = endMs - (weeks * 7 - 1) * DAY
  const keys = Object.keys(counts)
  if (keys.length > 0) {
    const firstMs = Date.parse(keys.reduce((a, b) => (a < b ? a : b)) + 'T00:00:00Z')
    if (firstMs > startMs) startMs = firstMs
  }
  const cells: HeatCell[] = []
  for (let ms = startMs; ms <= endMs; ms += DAY) {
    const key = dayKey(new Date(ms))
    const count = counts[key] ?? 0
    cells.push({ date: key, count, level: levelFor(count) })
  }
  return cells
}
