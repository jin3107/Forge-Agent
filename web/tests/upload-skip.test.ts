import assert from 'node:assert/strict'
import { localizeUploadSkip } from '../src/workspace-editor'
const en = (_vi:string, english:string) => english
const vi = (vietnamese:string, _en:string) => vietnamese
assert.equal(localizeUploadSkip('a.txt (already exists)', vi), 'a.txt (đã tồn tại)')
assert.equal(localizeUploadSkip('a.txt (already exists)', en), 'a.txt (already exists)')
assert.equal(localizeUploadSkip('big (>20MB)', vi), 'big (>20MB)')
assert.equal(localizeUploadSkip('#21: extra.txt (over 20-file limit)', vi), '#21: extra.txt (vượt giới hạn 20 file)')
assert.equal(localizeUploadSkip('#21: extra.txt (over 20-file limit)', en), '#21: extra.txt (over 20-file limit)')
console.log('upload skipped-file translations: passed')
