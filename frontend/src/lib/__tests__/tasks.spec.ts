import { describe, expect, it } from 'vitest'

import { renderMemoMarkdown } from '@/lib/markdown'
import { taskCheckboxLines, toggleTask } from '@/lib/tasks'

// A document whose rendered checkboxes and regex-matchable lines disagree in both directions:
// the fenced literal is matched by a naive regex but never rendered, the blockquoted and the
// ordered task are rendered but never matched.
const MIXED = [
  '```', // 0
  '- [ ] fenced', // 1 — code, not a checkbox
  '```', // 2
  '', // 3
  '> - [ ] quoted', // 4 — checkbox 0
  '', // 5
  '- [ ] plain', // 6 — checkbox 1
  '', // 7
  '1. [ ] ordered', // 8 — checkbox 2
].join('\n')

// The list marker sits on its own line; the checkbox lands on the item's first content line.
const MARKER_ALONE = '-\n  [ ] first\n- [ ] second'

describe('taskCheckboxLines', () => {
  it('skips fenced literals and counts blockquoted and ordered tasks', () => {
    expect(taskCheckboxLines(MIXED)).toEqual([4, 6, 8])
  })

  it('skips a tilde fence and an indented code block', () => {
    expect(taskCheckboxLines('~~~\n- [ ] fenced\n~~~\n- [ ] real')).toEqual([3])
    expect(taskCheckboxLines('    - [ ] indented\n\n- [ ] real')).toEqual([2])
  })

  it('skips list items the renderer does not turn into checkboxes', () => {
    // `[ ]` needs a trailing space to become a checkbox, and an empty box is just text.
    expect(taskCheckboxLines('- [ ]nospace\n- [] empty\n- [ ]\n- [ ] ok')).toEqual([3])
  })

  it('counts nested tasks and every bullet style', () => {
    expect(taskCheckboxLines('- [ ] parent\n  - [ ] child\n    - [ ] grandchild')).toEqual([0, 1, 2])
    expect(taskCheckboxLines('* [ ] star\n\n+ [ ] plus')).toEqual([0, 2])
  })

  it('reports lines of the ORIGINAL source, not of the tag-stripped one the renderer parses', () => {
    // The tag-only line is dropped before rendering (it shows as a chip), so the renderer's own
    // line numbers run one short from there on.
    expect(taskCheckboxLines('```\n#tag\n- [ ] fenced\n```\n- [ ] real')).toEqual([4])
    expect(taskCheckboxLines('#tag\n- [ ] after')).toEqual([1])
  })
})

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

  it('toggles past a fenced literal instead of editing the code block', () => {
    expect(toggleTask('```\n- [ ] fenced\n```\n- [ ] real', 0)).toBe(
      '```\n- [ ] fenced\n```\n- [x] real',
    )
  })

  it('toggles a blockquoted task, preserving the quote prefix', () => {
    expect(toggleTask('> - [ ] q1\n> - [x] q2', 0)).toBe('> - [x] q1\n> - [x] q2')
    expect(toggleTask('> - [ ] q1\n> - [x] q2', 1)).toBe('> - [ ] q1\n> - [ ] q2')
    expect(toggleTask('> > - [ ] deep', 0)).toBe('> > - [x] deep')
    expect(toggleTask('>- [ ] tight', 0)).toBe('>- [x] tight')
    expect(toggleTask('- > - [ ] quoted-in-list', 0)).toBe('- > - [x] quoted-in-list')
  })

  it('hits the right line in a fence + blockquote + list + ordered mix', () => {
    expect(toggleTask(MIXED, 0).split('\n')[4]).toBe('> - [x] quoted')
    expect(toggleTask(MIXED, 1).split('\n')[6]).toBe('- [x] plain')
    expect(toggleTask(MIXED, 2).split('\n')[8]).toBe('1. [x] ordered')
    // ...and nothing else moves.
    expect(toggleTask(MIXED, 0).split('\n')[1]).toBe('- [ ] fenced')
  })

  it('keeps indentation and bullet style when toggling nested and non-dash tasks', () => {
    expect(toggleTask('- [ ] parent\n  - [ ] child\n    - [ ] grandchild', 2)).toBe(
      '- [ ] parent\n  - [ ] child\n    - [x] grandchild',
    )
    expect(toggleTask('* [ ] star\n\n+ [ ] plus', 1)).toBe('* [ ] star\n\n+ [x] plus')
    expect(toggleTask('1. [ ] one\n\n2) [ ] two', 1)).toBe('1. [ ] one\n\n2) [x] two')
  })

  it('unchecks an uppercase mark', () => {
    expect(toggleTask('- [X] upper', 0)).toBe('- [ ] upper')
  })

  it('ignores list items that render as plain text', () => {
    expect(toggleTask('- [ ]nospace\n- [] empty\n- [ ] ok', 0)).toBe(
      '- [ ]nospace\n- [] empty\n- [x] ok',
    )
  })

  it('counts source lines around a stripped tag-only line', () => {
    expect(toggleTask('#tag\n- [ ] after', 0)).toBe('#tag\n- [x] after')
  })

  it('preserves CRLF line endings', () => {
    expect(toggleTask('- [ ] a\r\n- [ ] b', 1)).toBe('- [ ] a\r\n- [x] b')
  })

  // A bullet on its own line puts the checkbox on the NEXT line, indented into the item — the
  // renderer draws a checkbox there, so the toggle has to find it without a marker to anchor on.
  it('toggles a checkbox whose list marker sits on the preceding line', () => {
    expect(taskCheckboxLines(MARKER_ALONE)).toEqual([1, 2])
    expect(toggleTask(MARKER_ALONE, 0)).toBe('-\n  [x] first\n- [ ] second')
    expect(toggleTask(MARKER_ALONE, 1)).toBe('-\n  [ ] first\n- [x] second')
  })

  it('toggles a marker-alone checkbox through blockquote and ordered markers', () => {
    expect(toggleTask('> -\n>   [ ] first', 0)).toBe('> -\n>   [x] first')
    expect(toggleTask('> > -\n> >   [ ] first', 0)).toBe('> > -\n> >   [x] first')
    expect(toggleTask('1.\n   [ ] first', 0)).toBe('1.\n   [x] first')
    expect(toggleTask('-\n  [x] first', 0)).toBe('-\n  [ ] first')
  })

  it('leaves a would-be continuation the renderer does NOT draw as a checkbox alone', () => {
    // 6-space indent under a bare marker is an indented code block, and an unindented `[ ]` line
    // is a paragraph of its own — neither renders a checkbox, so neither is toggleable.
    expect(taskCheckboxLines('-\n      [ ] first')).toEqual([])
    expect(toggleTask('-\n      [ ] first', 0)).toBe('-\n      [ ] first')
    expect(taskCheckboxLines('-\n[ ] first')).toEqual([])
    expect(toggleTask('-\n[ ] first', 0)).toBe('-\n[ ] first')
  })
})

// The bug this guards: MemoCard indexes the rendered `.task-list-item-checkbox` inputs, so the
// source-side enumeration has to be the SAME list. Checking the rendered HTML back after each
// toggle is the only assertion that proves both sides agree, document by document.
function renderedCheckedStates(source: string): boolean[] {
  const html = renderMemoMarkdown(source)
  return [...html.matchAll(/<input\b[^>]*>/g)]
    .map((m) => m[0])
    .filter((tag) => tag.includes('task-list-item-checkbox'))
    .map((tag) => tag.includes('checked'))
}

const CORPUS: Record<string, string> = {
  plain: '- [ ] a\n- [x] b',
  fenceFirst: '```\n- [ ] fenced\n- [x] fenced2\n```\n- [ ] real\n- [x] real2',
  tildeFence: '~~~\n- [ ] fenced\n~~~\n- [ ] real',
  indentedCode: '    - [ ] indented\n\n- [ ] real',
  blockquote: 'intro\n\n> - [ ] q1\n> - [x] q2\n\n- [ ] plain',
  nestedQuote: '> > - [ ] deep\n\n- [x] after',
  quoteNoSpace: '>- [ ] tight\n\n- [ ] plain',
  quoteInList: '- > - [ ] weird\n\n- [ ] plain',
  listInQuote: '> - outer\n>   - [ ] inner\n\n- [ ] plain',
  nested: '- [ ] parent\n  - [x] child\n    - [ ] grandchild',
  bullets: '* [ ] star\n\n+ [x] plus\n\n- [ ] dash',
  ordered: '1. [ ] one\n\n2) [x] two\n\n- [ ] dash',
  nonTasks: '- [ ]nospace\n- [] empty\n- [ ]\n- [ ] ok',
  tagOnlyInFence: '```\n#tag\n- [ ] fenced\n```\n#tag\n- [ ] real',
  mixed: MIXED,
  crlf: '- [ ] a\r\n- [x] b',
  markerAlone: MARKER_ALONE,
  markerAloneQuoted: '> -\n>   [x] quoted\n\n- [ ] plain',
  markerAloneOrdered: '1.\n   [ ] first\n\n- [x] plain',
}

describe('rendered checkboxes and source enumeration agree', () => {
  for (const [name, source] of Object.entries(CORPUS)) {
    it(`toggles exactly the clicked checkbox — ${name}`, () => {
      const before = renderedCheckedStates(source)
      expect(before.length).toBeGreaterThan(0)
      expect(taskCheckboxLines(source)).toHaveLength(before.length)
      before.forEach((_, index) => {
        expect(renderedCheckedStates(toggleTask(source, index))).toEqual(
          before.map((state, i) => (i === index ? !state : state)),
        )
      })
    })
  }
})
