// Justified-rows gallery layout (Google Photos / Flickr): pack items into rows so each row's
// scaled height is close to a target while the row fills the container width exactly — preserving
// every item's aspect ratio (no square cropping). Pure + DOM-free so it is unit-tested directly.

export interface GalleryItem {
  width: number | null
  height: number | null
}

export interface LaidOut<T> {
  item: T
  width: number
  height: number
}

export interface GalleryRow<T> {
  items: LaidOut<T>[]
  height: number
}

const FALLBACK_AR = 1 // unknown dimensions → treat as square

export function aspectRatio(w: number | null | undefined, h: number | null | undefined): number {
  if (!w || !h || w <= 0 || h <= 0) return FALLBACK_AR
  return w / h
}

function buildRow<T extends GalleryItem>(items: T[], height: number): GalleryRow<T> {
  return {
    height,
    items: items.map((item) => ({
      item,
      width: aspectRatio(item.width, item.height) * height,
      height,
    })),
  }
}

/**
 * Lay items out in justified rows. A row commits once its items, scaled to fill
 * `containerWidth` (minus inter-item `gap`s), would be at/below `targetHeight`. The trailing
 * partial row keeps `targetHeight` (left-aligned, not stretched across the full width).
 */
export function justifyRows<T extends GalleryItem>(
  items: T[],
  containerWidth: number,
  targetHeight: number,
  gap: number,
): GalleryRow<T>[] {
  if (containerWidth <= 0 || targetHeight <= 0 || !items.length) return []
  const rows: GalleryRow<T>[] = []
  let row: T[] = []
  let arSum = 0
  for (const it of items) {
    row.push(it)
    arSum += aspectRatio(it.width, it.height)
    const avail = containerWidth - gap * (row.length - 1)
    const rowHeight = avail / arSum
    if (rowHeight <= targetHeight) {
      rows.push(buildRow(row, rowHeight))
      row = []
      arSum = 0
    }
  }
  if (row.length) rows.push(buildRow(row, targetHeight)) // trailing row at natural target height
  return rows
}
