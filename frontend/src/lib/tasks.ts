// Toggle the Nth markdown task checkbox in `content` (`- [ ]` ↔ `- [x]`), counting task
// lines in document order. Pure so the toggle is unit-tested; MemoCard PATCHes the result.
const TASK_RE = /^(\s*[-*+] \[)([ xX])(\])/

export function toggleTask(content: string, index: number): string {
  let seen = -1
  return content
    .split('\n')
    .map((line) => {
      if (!TASK_RE.test(line)) return line
      seen += 1
      if (seen !== index) return line
      return line.replace(TASK_RE, (_m, pre: string, mark: string, post: string) => {
        const flipped = mark === ' ' ? 'x' : ' '
        return `${pre}${flipped}${post}`
      })
    })
    .join('\n')
}
