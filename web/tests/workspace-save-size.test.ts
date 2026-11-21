import assert from 'node:assert/strict'
import { EDITOR_MAX_UTF8_BYTES as limit, fitsEditorSaveLimit, restoreLineEndings } from '../src/workspace-editor'
assert.equal(fitsEditorSaveLimit('a'.repeat(limit)), true)
assert.equal(fitsEditorSaveLimit('a'.repeat(limit + 1)), false)
assert.equal(fitsEditorSaveLimit('é'.repeat(limit / 2)), true)
assert.equal(fitsEditorSaveLimit('é'.repeat(limit / 2 + 1)), false)
assert.equal(fitsEditorSaveLimit('😀'.repeat(limit / 4)), true)
const normalized = 'a\n'.repeat(limit / 2)
assert.equal(fitsEditorSaveLimit(normalized), true)
assert.equal(fitsEditorSaveLimit(restoreLineEndings(normalized, 'crlf')), false)
console.log('editor UTF-8 save size and restored line endings: passed')
