import assert from 'node:assert/strict'
import { uploadCheckpointNotice, uploadCheckpointWarning } from '../src/checkpoint'
const en = (_vi: string, english: string) => english
const vi = (vietnamese: string, _en: string) => vietnamese
assert.equal(uploadCheckpointWarning('created', en), null)
for (const status of ['failed', 'disabled', 'unavailable', undefined] as const) {
  assert.match(uploadCheckpointWarning(status, en) ?? '', /Files uploaded.*Undo/)
  assert.match(uploadCheckpointWarning(status, vi) ?? '', /Đã tải file lên.*Undo/)
}
assert.match(uploadCheckpointNotice(en), /excluded or Git-ignored/)
assert.match(uploadCheckpointNotice(vi), /loại trừ hoặc Git bỏ qua/)
console.log('upload checkpoint warning and notice translations: passed')
