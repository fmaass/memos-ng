// Build a GitHub-style activity grid: the last `weeks` weeks of days, each with its memo
// count and a 0–4 intensity level. Pure so the bucketing/levels are unit-tested; `today`
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
  const cells: HeatCell[] = []
  const totalDays = weeks * 7
  const start = new Date(today.getTime() - (totalDays - 1) * 86_400_000)
  for (let i = 0; i < totalDays; i++) {
    const d = new Date(start.getTime() + i * 86_400_000)
    const key = dayKey(d)
    const count = counts[key] ?? 0
    cells.push({ date: key, count, level: levelFor(count) })
  }
  return cells
}
