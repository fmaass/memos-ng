// @vitest-environment node
import { describe, expect, it } from 'vitest'

import { smokePort } from './server.mjs'

describe('smokePort', () => {
  it('asks the OS for a free port when PLACES_SMOKE_PORT is unset, so two runs cannot collide', () => {
    expect(smokePort({})).toBe(0)
  })

  it('treats an empty PLACES_SMOKE_PORT as unset', () => {
    expect(smokePort({ PLACES_SMOKE_PORT: '' })).toBe(0)
  })

  it('uses an explicit port as given', () => {
    expect(smokePort({ PLACES_SMOKE_PORT: '8741' })).toBe(8741)
    expect(smokePort({ PLACES_SMOKE_PORT: '65535' })).toBe(65535)
    expect(smokePort({ PLACES_SMOKE_PORT: '0' })).toBe(0)
  })

  it.each(['abc', '8741abc', ' 8741', '0x1F', '1e3', '87.5', '-1', '65536'])(
    'refuses %j with an error naming the variable',
    (value) => {
      expect(() => smokePort({ PLACES_SMOKE_PORT: value })).toThrow(/PLACES_SMOKE_PORT/)
    },
  )
})
