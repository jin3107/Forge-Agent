import assert from 'node:assert/strict'
import { saveCheckpointNotice, saveCheckpointWarning } from '../src/checkpoint'
const en = (_vi: string, english: string) => english
const vi = (vietnamese: string, _en: string) => vietnamese
assert.equal(saveCheckpointWarning('created', en), null)
for (const status of ['failed', 'disabled', 'unavailable', undefined] as const) {
  assert.match(saveCheckpointWarning(status, en) ?? '', /File saved.*Undo/)
  assert.match(saveCheckpointWarning(status, vi) ?? '', /Đã lưu.*Undo/)
}
assert.match(saveCheckpointNotice(en), /excluded or Git-ignored/)
assert.match(saveCheckpointNotice(vi), /loại trừ hoặc Git bỏ qua/)
console.log('save checkpoint warning and notice translations: passed')
