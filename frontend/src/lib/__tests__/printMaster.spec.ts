import { describe, expect, it } from 'vitest'

import {
  DEFAULT_PRINT_VARIANT_KEY,
  formatFileSize,
  formatMasterFormat,
  isPrintVariant,
  printBadgeLabel,
  printChipState,
  printMasterCount,
  printMasterMeta,
  printRowState,
  printUploadVariantKey,
  selectPrintMaster,
  toPrintMasterInfo,
} from '@/lib/printMaster'

function rendition(variantKey: string, over: Record<string, unknown> = {}) {
  return {
    variant_key: variantKey,
    label: variantKey.includes(':') ? variantKey.split(':')[1] : null,
    mime: 'image/tiff',
    size: 8_598_323,
    downloadUrl: `/api/attachments/a1/rendition/${variantKey}`,
    ...over,
  }
}

function photo(over: Record<string, unknown> = {}) {
  return { mime: 'image/jpeg', renditions: [], ...over }
}

describe('isPrintVariant', () => {
  it.each(['print', 'print:glossy', 'print:matte'])('accepts %s', (key) => {
    expect(isPrintVariant(key)).toBe(true)
  })

  // Matched, not prefixed — mirrors printready.is_print_variant, so a future rung with a
  // print-ish name is not silently adopted as somebody's archive master.
  it.each(['printready', 'printable', 'web', 'screen', 'PRINT'])('rejects %s', (key) => {
    expect(isPrintVariant(key)).toBe(false)
  })
})

describe('selectPrintMaster', () => {
  it('is the first print variant in server (id) order — the row the photo book derives from', () => {
    const chosen = selectPrintMaster([rendition('print:glossy'), rendition('print:matte')])
    expect(chosen?.variant_key).toBe('print:glossy')
  })

  it('skips non-print renditions rather than treating them as masters', () => {
    expect(selectPrintMaster([rendition('printready'), rendition('print')])?.variant_key).toBe('print')
  })

  it.each([[[]], [null], [undefined]])('is null when there is nothing to select (%s)', (input) => {
    expect(selectPrintMaster(input as never)).toBeNull()
  })
})

describe('printUploadVariantKey', () => {
  it('keeps the key of the master being replaced (production masters are print:glossy)', () => {
    expect(printUploadVariantKey(rendition('print:glossy'))).toBe('print:glossy')
  })

  it('falls back to the bare print key for a first attach', () => {
    expect(printUploadVariantKey(null)).toBe(DEFAULT_PRINT_VARIANT_KEY)
    expect(printUploadVariantKey(null)).toBe('print')
  })
})

describe('printMasterCount / printBadgeLabel', () => {
  it('counts PHOTOS, ignoring videos (a clip has no print master)', () => {
    const attachments = [
      photo({ renditions: [rendition('print')] }),
      photo(),
      { mime: 'video/mp4', renditions: [] },
    ]
    expect(printMasterCount(attachments)).toEqual({ photos: 2, withMaster: 1 })
    expect(printBadgeLabel(attachments)).toBe('🖨 1 of 2')
  })

  it('says nothing at all when no photo carries a master', () => {
    expect(printBadgeLabel([photo(), photo()])).toBeNull()
    expect(printBadgeLabel([])).toBeNull()
  })
})

describe('printChipState', () => {
  it('marks a stored master', () => {
    expect(printChipState(photo({ renditions: [rendition('print:glossy')] }))).toEqual({
      kind: 'present',
      label: 'PRINT',
    })
  })

  it('shows the running upload with its percentage', () => {
    expect(printChipState(photo(), { status: 'uploading', progress: 62 })).toEqual({
      kind: 'uploading',
      label: 'UPLOADING 62%',
      progress: 62,
    })
  })

  it('shows a failure with its retry affordance', () => {
    expect(printChipState(photo(), { status: 'error', progress: 30 })).toEqual({
      kind: 'failed',
      label: 'PRINT FAILED · RETRY',
    })
  })

  it('lets an in-flight REPLACE outrank the master it is about to overwrite', () => {
    const replacing = printChipState(photo({ renditions: [rendition('print')] }), {
      status: 'uploading',
      progress: 5,
    })
    expect(replacing?.kind).toBe('uploading')
  })

  it('chips nothing for a photo with no master and nothing happening, or for a video', () => {
    expect(printChipState(photo())).toBeNull()
    expect(printChipState({ mime: 'video/mp4', renditions: [rendition('print')] })).toBeNull()
  })
})

describe('formatFileSize', () => {
  it.each([
    [0, '0 bytes'],
    [512, '512 bytes'],
    [2048, '2.0 KB'],
    [8_598_323, '8.2 MB'],
    [3_221_225_472, '3.0 GB'],
  ])('formats %i as %s', (bytes, expected) => {
    expect(formatFileSize(bytes)).toBe(expected)
  })
})

describe('formatMasterFormat', () => {
  it.each([
    ['image/tiff', undefined, 'TIFF'],
    ['image/jpeg', undefined, 'JPEG'],
    ['image/x-adobe-dng', undefined, 'DNG'],
    ['image/svg+xml', undefined, 'XML'],
  ])('reads %s as %s', (mime, filename, expected) => {
    expect(formatMasterFormat(mime, filename)).toBe(expected)
  })

  it('falls back to the filename when the mime says nothing (a RAW upload from a plain browser)', () => {
    expect(formatMasterFormat('application/octet-stream', 'shot.NEF')).toBe('NEF')
    expect(formatMasterFormat('application/octet-stream', null)).toBe('FILE')
  })
})

describe('printMasterMeta', () => {
  it('states dimensions, size and format', () => {
    expect(
      printMasterMeta({ width: 4032, height: 3024, size: 8_598_323, mime: 'image/tiff' }),
    ).toBe('4032 × 3024 · 8.2 MB · TIFF')
  })

  it('omits dimensions the server could not measure instead of printing nulls', () => {
    expect(printMasterMeta({ width: null, height: null, size: 2048, mime: 'image/tiff' })).toBe(
      '2.0 KB · TIFF',
    )
  })
})

describe('printRowState', () => {
  const info = { variantKey: 'print:glossy', meta: '4032 × 3024 · 8.2 MB · TIFF', downloadUrl: '/dl', width: 4032, height: 3024 }
  const base = { isPhoto: true, resolved: true, mine: true, master: info, canAttach: true, canRemove: true }

  it('offers the owner all four verbs on a stored master', () => {
    const state = printRowState(base)
    expect(state.visible).toBe(true)
    expect(state.actions).toEqual(['view', 'download', 'replace', 'remove'])
    expect(state.title).toBe('Print master')
    expect(state.meta).toBe('4032 × 3024 · 8.2 MB · TIFF')
    expect(state.faceToggle).toBe(true)
  })

  it('lets the household look and download but never replace or remove someone else’s master', () => {
    const state = printRowState({ ...base, mine: false })
    expect(state.visible).toBe(true)
    expect(state.actions).toEqual(['view', 'download'])
  })

  it('offers a single explicit attach when the owner’s photo has no master', () => {
    const state = printRowState({ ...base, master: null })
    expect(state.visible).toBe(true)
    expect(state.actions).toEqual(['attach'])
    expect(state.meta).toBe('Not attached yet')
    expect(state.faceToggle).toBe(false) // nothing to flip to
  })

  it('stays hidden until the lookup has answered — no "not attached yet" flash', () => {
    expect(printRowState({ ...base, resolved: false, master: null }).visible).toBe(false)
    expect(printRowState({ ...base, resolved: false }).visible).toBe(false)
  })

  it('stays hidden for a video, and for a stranger’s photo with nothing to show', () => {
    expect(printRowState({ ...base, isPhoto: false }).visible).toBe(false)
    expect(printRowState({ ...base, mine: false, master: null }).visible).toBe(false)
  })

  it('drops the verbs whose handlers the host did not wire', () => {
    expect(printRowState({ ...base, canAttach: false }).actions).toEqual([
      'view',
      'download',
      'remove',
    ])
    expect(printRowState({ ...base, canRemove: false }).actions).toEqual([
      'view',
      'download',
      'replace',
    ])
  })
})

describe('toPrintMasterInfo', () => {
  it('carries the key, the download URL and the measured pixel size into the viewer', () => {
    const info = toPrintMasterInfo({ ...rendition('print:glossy'), width: 4032, height: 3024 })
    expect(info).toEqual({
      variantKey: 'print:glossy',
      meta: '4032 × 3024 · 8.2 MB · TIFF',
      downloadUrl: '/api/attachments/a1/rendition/print:glossy',
      width: 4032,
      height: 3024,
    })
  })

  it('is null for a photo with no master', () => {
    expect(toPrintMasterInfo(null)).toBeNull()
  })
})
