import { describe, expect, it } from 'vitest'

import { toggleTask } from '@/lib/tasks'

describe('toggleTask', () => {
  it('checks an unchecked task by index', () => {
    expect(toggleTask('- [ ] a\n- [ ] b', 0)).toBe('- [x] a\n- [ ] b')
  })

  it('unchecks a checked task by index', () => {
    expect(toggleTask('- [ ] a\n- [x] b', 1)).toBe('- [ ] a\n- [ ] b')
  })

  it('counts only task lines, leaving prose untouched', () => {
    const md = 'intro\n- [ ] one\nmiddle\n- [ ] two'
    expect(toggleTask(md, 1)).toBe('intro\n- [ ] one\nmiddle\n- [x] two')
  })

  it('is a no-op when the index is out of range', () => {
    expect(toggleTask('- [ ] a', 5)).toBe('- [ ] a')
  })
})
