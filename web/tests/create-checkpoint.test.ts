import assert from 'node:assert/strict'
import { createCheckpointNotice, createCheckpointWarning } from '../src/checkpoint'
const en = (_vi: string, english: string) => english
const vi = (vietnamese: string, _en: string) => vietnamese
assert.equal(createCheckpointWarning('created', en), null)
for (const status of ['failed', 'disabled', 'unavailable', undefined] as const) {
  assert.match(createCheckpointWarning(status, en) ?? '', /File created.*Undo/)
  assert.match(createCheckpointWarning(status, vi) ?? '', /Đã tạo file.*Undo/)
}
assert.match(createCheckpointNotice(en), /excluded or Git-ignored/)
assert.match(createCheckpointNotice(vi), /loại trừ hoặc Git bỏ qua/)
console.log('create checkpoint warning and notice translations: passed')
