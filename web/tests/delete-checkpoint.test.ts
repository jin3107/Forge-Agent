import assert from 'node:assert/strict'
import { deleteCheckpointNotice, deleteCheckpointWarning } from '../src/checkpoint'
const en = (_vi: string, english: string) => english
const vi = (vietnamese: string, _en: string) => vietnamese
assert.equal(deleteCheckpointWarning('created', en), null)
for (const status of ['failed', 'disabled', 'unavailable', undefined] as const) {
  assert.match(deleteCheckpointWarning(status, en) ?? '', /File deleted.*Undo/)
  assert.match(deleteCheckpointWarning(status, vi) ?? '', /Đã xóa.*Undo/)
}
assert.match(deleteCheckpointNotice(en), /excluded or Git-ignored/)
assert.match(deleteCheckpointNotice(vi), /loại trừ hoặc Git bỏ qua/)
console.log('delete checkpoint warning and notice translations: passed')
