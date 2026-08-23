// @vitest-environment jsdom
// jsdom, not the project-default happy-dom: the parity projection has to agree byte-for-byte
// with what the generator produces, and the generator runs under jsdom (DOMPurify supports it).
import { describe, expect, it } from 'vitest'

import { projectRenderedMarkdown } from '@/lib/markdownParity'
import { TASK_CHECKBOX_CLASS } from '@/lib/tasks'

describe('projectRenderedMarkdown', () => {
  it('keeps the checkbox class in step with tasks.ts', () => {
    // markdownParity.ts holds its own literal so it stays import-free for the node generator.
    const blocks = projectRenderedMarkdown(
      `<ul><li><input class="${TASK_CHECKBOX_CLASS}"> item</li></ul>`,
    )
    expect(blocks[0]?.type).toBe('task')
  })

  it('projects headings with their level', () => {
    expect(projectRenderedMarkdown('<h1>One</h1><h3>Three</h3>')).toEqual([
      { type: 'heading', level: 1, text: 'One', links: [], inline: [] },
      { type: 'heading', level: 3, text: 'Three', links: [], inline: [] },
    ])
  })

  it('keeps <br> as a newline and collapses the whitespace around it', () => {
    const [block] = projectRenderedMarkdown('<p>line one<br>\nline two</p>')
    expect(block?.text).toBe('line one\nline two')
  })

  it('collapses whitespace runs, trims, and normalizes to NFC', () => {
    const nfd = 'cafe\u0301' // e + combining acute (escaped: an editor must not normalize it)
    const [block] = projectRenderedMarkdown(`<p>  a   b\t\tc ${nfd}  </p>`)
    expect(block?.text).toBe('a b c caf\u00e9')
    expect(block?.text.normalize('NFD')).not.toBe(block?.text)
  })

  it('keeps http(s) and mailto links and drops every other scheme', () => {
    const [block] = projectRenderedMarkdown(
      '<p><a href="https://example.com/a">site</a> ' +
        '<a href="mailto:a@b.com">mail</a> ' +
        '<a href="javascript:alert(1)">js</a> ' +
        '<a href="data:text/html,x">data</a> ' +
        '<a>bare</a></p>',
    )
    expect(block?.links).toEqual([
      { href: 'https://example.com/a', text: 'site' },
      { href: 'mailto:a@b.com', text: 'mail' },
    ])
    // The rejected anchors still contribute their text — they render as plain text.
    expect(block?.text).toBe('site mail js data bare')
  })

  it('projects styled spans in document order, outer span first', () => {
    const [block] = projectRenderedMarkdown(
      '<p><strong>bold <em>inner</em></strong> <em>it</em> <s>struck</s> <del>gone</del> <code>x  y</code></p>',
    )
    expect(block?.inline).toEqual([
      { type: 'strong', text: 'bold inner' },
      { type: 'em', text: 'inner' },
      { type: 'em', text: 'it' },
      { type: 'strike', text: 'struck' },
      { type: 'strike', text: 'gone' },
      { type: 'code', text: 'x y' },
    ])
    expect(block?.text).toBe('bold inner it struck gone x y')
  })

  it('projects a fenced code block verbatim', () => {
    const html = '<pre><code class="language-js">const a = 1\n  indented\n&lt;div&gt;\n</code></pre>'
    expect(projectRenderedMarkdown(html)).toEqual([
      { type: 'code', text: 'const a = 1\n  indented\n<div>\n', links: [], inline: [] },
    ])
  })

  it('projects an unordered list as one block of item texts', () => {
    expect(projectRenderedMarkdown('<ul><li>one</li><li><a href="https://e.com">two</a></li></ul>')).toEqual([
      {
        type: 'list',
        level: 0,
        ordered: false,
        text: '',
        items: ['one', 'two'],
        links: [{ href: 'https://e.com', text: 'two' }],
        inline: [],
      },
    ])
  })

  it('projects a sublist one level deeper, in document order after the item holding it', () => {
    const blocks = projectRenderedMarkdown('<ul><li>a<ul><li>a1</li></ul></li><li>b</li></ul>')
    expect(blocks).toEqual([
      { type: 'list', level: 0, ordered: false, text: '', items: ['a'], links: [], inline: [] },
      { type: 'list', level: 1, ordered: false, text: '', items: ['a1'], links: [], inline: [] },
      { type: 'list', level: 0, ordered: false, text: '', items: ['b'], links: [], inline: [] },
    ])
  })

  it('projects task items as their own blocks with the checked state', () => {
    const blocks = projectRenderedMarkdown(
      `<ul><li><input class="${TASK_CHECKBOX_CLASS}"> todo</li>` +
        `<li><input class="${TASK_CHECKBOX_CLASS}" checked=""> done</li></ul>`,
    )
    expect(blocks).toEqual([
      { type: 'task', level: 0, ordered: false, checked: false, text: 'todo', links: [], inline: [] },
      { type: 'task', level: 0, ordered: false, checked: true, text: 'done', links: [], inline: [] },
    ])
  })

  it('finds the checkbox of a loose task item (nested in a <p>) and of a sublist item', () => {
    const blocks = projectRenderedMarkdown(
      `<ul><li><p><input class="${TASK_CHECKBOX_CLASS}" checked=""> outer</p>` +
        `<ul><li><input class="${TASK_CHECKBOX_CLASS}"> inner</li></ul></li></ul>`,
    )
    expect(blocks).toEqual([
      { type: 'task', level: 0, ordered: false, checked: true, text: 'outer', links: [], inline: [] },
      { type: 'task', level: 1, ordered: false, checked: false, text: 'inner', links: [], inline: [] },
    ])
  })

  it('splits a list that mixes plain items and tasks, keeping document order', () => {
    const blocks = projectRenderedMarkdown(
      `<ul><li>plain</li><li><input class="${TASK_CHECKBOX_CLASS}"> task</li><li>after</li></ul>`,
    )
    expect(blocks.map((block) => [block.type, block.items ?? block.text])).toEqual([
      ['list', ['plain']],
      ['task', 'task'],
      ['list', ['after']],
    ])
  })

  it('flags an ordered list, and an unordered one, with `ordered`', () => {
    expect(projectRenderedMarkdown('<ol><li>first</li><li>second</li></ol>')).toEqual([
      { type: 'list', level: 0, ordered: true, text: '', items: ['first', 'second'], links: [], inline: [] },
    ])
    expect(projectRenderedMarkdown('<ul><li>first</li><li>second</li></ul>')).toEqual([
      { type: 'list', level: 0, ordered: false, text: '', items: ['first', 'second'], links: [], inline: [] },
    ])
  })

  it('carries `ordered` on task items and on each nesting level separately', () => {
    const blocks = projectRenderedMarkdown(
      `<ol><li><input class="${TASK_CHECKBOX_CLASS}" checked=""> numbered task` +
        `<ul><li>bulleted child</li></ul></li></ol>`,
    )
    expect(blocks).toEqual([
      { type: 'task', level: 0, ordered: true, checked: true, text: 'numbered task', links: [], inline: [] },
      { type: 'list', level: 1, ordered: false, text: '', items: ['bulleted child'], links: [], inline: [] },
    ])
  })

  it('projects a table as rows of cell texts, header row first', () => {
    const html =
      '<table><thead><tr><th>a</th><th>b</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table>'
    expect(projectRenderedMarkdown(html)).toEqual([
      { type: 'table', text: '', rows: [['a', 'b'], ['1', '2']], links: [], inline: [] },
    ])
  })

  it('projects blockquotes as flat text and hr as an empty block', () => {
    expect(projectRenderedMarkdown('<blockquote><p>q one<br>\nq two</p></blockquote><hr>')).toEqual([
      { type: 'blockquote', text: 'q one\nq two', links: [], inline: [] },
      { type: 'hr', text: '', links: [], inline: [] },
    ])
  })

  it('projects a paragraph the sanitizer emptied (an image) as an empty paragraph', () => {
    expect(projectRenderedMarkdown('<p></p>\n<p>tail</p>')).toEqual([
      { type: 'paragraph', text: '', links: [], inline: [] },
      { type: 'paragraph', text: 'tail', links: [], inline: [] },
    ])
  })
})
