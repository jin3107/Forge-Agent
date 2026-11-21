import assert from 'node:assert/strict'
import { inspectLineEndings, restoreLineEndings } from '../src/workspace-editor'
for (const [text, style, normalized] of [
  ['hello\nworld\n', 'lf', 'hello\nworld\n'],
  ['hello\r\nworld\r\n', 'crlf', 'hello\nworld\n'],
  ['hello\rworld\r', 'cr', 'hello\nworld\n'],
  ['', 'lf', ''], ['no newline', 'lf', 'no newline'],
  ['a\r\nb\nc', 'mixed', 'a\nb\nc'],
  ['a\rb\r\nc', 'mixed', 'a\nb\nc'],
  ['a\rb\nc', 'mixed', 'a\nb\nc'],
] as const) {
  const result = inspectLineEndings(text)
  assert.deepEqual(result, {style, normalized})
  if (style !== 'mixed') assert.equal(restoreLineEndings(normalized, style), text)
}
assert.equal(restoreLineEndings('first\nnew\nlast', 'crlf'), 'first\r\nnew\r\nlast')
assert.equal(restoreLineEndings('first\nnew\nlast', 'cr'), 'first\rnew\rlast')
assert.throws(() => restoreLineEndings('a\nb', 'mixed'), /mixed/i)
console.log('workspace uniform and mixed line endings: passed')
