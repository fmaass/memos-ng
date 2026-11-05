import { describe, expect, it } from 'vitest'

import {
  applyUploadProgress,
  parseUploadProgress,
  type ProgressTile,
  uploadPercent,
} from '@/lib/uploadProgress'

const ev = (bytes_received: number, declared_size = 400, upload_uid = 'u1') => ({
  upload_uid,
  bytes_received,
  declared_size,
})

const tile = (over: Partial<ProgressTile> = {}): ProgressTile => ({
  status: 'uploading',
  progress: 10,
  uploadUid: 'u1',
  ...over,
})

describe('parseUploadProgress', () => {
  it('reads a well-formed frame', () => {
    expect(
      parseUploadProgress('{"upload_uid":"u1","bytes_received":5,"declared_size":10}'),
    ).toEqual(ev(5, 10))
  })

  it.each([
    ['not json', '{'],
    ['not an object', '42'],
    ['null', 'null'],
    ['missing uid', '{"bytes_received":5,"declared_size":10}'],
    ['string count', '{"upload_uid":"u1","bytes_received":"5","declared_size":10}'],
    ['negative count', '{"upload_uid":"u1","bytes_received":-1,"declared_size":10}'],
    ['a media-status frame', '{"uid":"a1","status":"processing","progress_pct":0}'],
  ])('refuses %s', (_label, data) => {
    expect(parseUploadProgress(data)).toBeNull()
  })
})

describe('uploadPercent', () => {
  it.each([
    [0, 400, 0],
    [1, 400, 0],
    [2, 400, 1],
    [200, 400, 50],
    [399, 400, 100],
    [400, 400, 100],
    [500, 400, 100],
    [0, 0, 100],
  ])('%i of %i bytes is %i %%', (received, size, pct) => {
    expect(uploadPercent(ev(received, size))).toBe(pct)
  })
})

describe('applyUploadProgress', () => {
  it('moves the matching uploading tile forward', () => {
    expect(applyUploadProgress(tile(), ev(200))).toBe(50)
  })

  it('ignores another upload', () => {
    expect(applyUploadProgress(tile(), ev(200, 400, 'u2'))).toBeNull()
  })

  it('ignores a tile that does not know its upload yet', () => {
    expect(applyUploadProgress(tile({ uploadUid: undefined }), ev(200))).toBeNull()
  })

  it.each(['queued', 'done', 'error'])('ignores a %s tile', (status) => {
    expect(applyUploadProgress(tile({ status }), ev(200))).toBeNull()
  })

  it('never moves a tile backwards or repeats what it shows', () => {
    expect(applyUploadProgress(tile({ progress: 60 }), ev(200))).toBeNull()
    expect(applyUploadProgress(tile({ progress: 50 }), ev(200))).toBeNull()
  })
})
