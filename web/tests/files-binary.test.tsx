import React from 'react'
import assert from 'node:assert/strict'
import { renderToStaticMarkup } from 'react-dom/server'
import { FilesPane } from '../src/components/FilesPane'
import { LanguageProvider } from '../src/i18n'
import { canSaveWorkspaceFile } from '../src/workspace-editor'

const noop = () => {}
const render = (binary: boolean, dirty: boolean, saving = false) => renderToStaticMarkup(
  <LanguageProvider><FilesPane sid="s" tree={[]} selected="file.bin" content="placeholder"
    binary={binary} mixedNewlines={false} missingVersion={false} dirty={dirty} saving={saving} deleting={false} deleted={false} locked={false} onOpen={noop} onChange={noop} onSave={noop}
    onDelete={noop} onRefresh={noop} onNewFile={noop} onUpload={noop} onExport={noop} /></LanguageProvider>,
)
const binary = render(true, true)
assert.match(binary, /<textarea[^>]*readonly=""/i)
assert.match(binary, /<button[^>]*class="btn-save[^>]*disabled=""/i)
assert.match(binary, /Chỉ đọc|Read only/i)
const text = render(false, true)
assert.doesNotMatch(text, /<textarea[^>]*readonly/i)
assert.doesNotMatch(text, /<button[^>]*class="btn-save[^>]*disabled/i)
const saving = render(false, true, true)
assert.match(saving, /<button[^>]*class="btn-save[^>]*disabled/i)
assert.match(saving, /Đang lưu|Saving/i)
assert.equal(canSaveWorkspaceFile('s', 'file.bin', 'placeholder', true), false)
assert.equal(canSaveWorkspaceFile('s', 'file.txt', '', false), true)
assert.equal(canSaveWorkspaceFile(null, 'file.txt', 'x', false), false)
assert.equal(canSaveWorkspaceFile('s', null, 'x', false), false)
assert.equal(canSaveWorkspaceFile('s', 'file.txt', null, false), false)
console.log('binary preview readonly, save guard, and text editor SSR: passed')
