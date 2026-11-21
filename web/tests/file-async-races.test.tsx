import React from 'react'
import assert from 'node:assert/strict'
import TestRenderer, { act } from 'react-test-renderer'
import App from '../src/App'
import { api } from '../src/api'
import { LanguageProvider } from '../src/i18n'
import { FilesPane } from '../src/components/FilesPane'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((ok, bad) => { resolve = ok; reject = bad })
  return { promise, resolve, reject }
}
async function main() {
  const listeners: Record<string, () => void> = {}
  Object.assign(globalThis, {
    window: {location: {search:''}, matchMedia: () => ({matches:true}), addEventListener: (name: string, fn: () => void) => { listeners[name] = fn }, removeEventListener: () => {}},
    location: {search:''},
    document: {documentElement: {lang:''}}, localStorage: {getItem: () => 'en', setItem: () => {}},
    prompt: () => 'created.txt', confirm: () => true,
  })
  const reads: Record<string, ReturnType<typeof deferred<any>>> = {}
  let pendingSave = deferred<any>()
  let pendingCreate = deferred<any>()
  let pendingDelete = deferred<any>()
  let saveCalls = 0
  const saveVersions: string[] = []
  const deletes: unknown[][] = []
  const fake = api as any
  fake.authMe = async () => ({user:{id:'u',role:'member',email:'u@test',name:'User'},google_enabled:false,registration_enabled:false,needs_setup:false})
  fake.health = async () => { throw new Error('no settings') }
  fake.models = async () => ({models:[]})
  fake.listSessions = async () => [{id:'s',title:'Test',model:'demo',created_at:'',updated_at:''}]
  fake.getSession = async () => ({id:'s',model:'demo',messages:[]})
  fake.context = async () => { throw new Error('no context') }
  fake.tree = async () => []
  fake.readFile = (_sid: string, path: string) => { const d = deferred<any>(); reads[path] = d; return d.promise }
  fake.saveFile = (_sid: string, _path: string, _content: string, version: string) => { saveCalls++; saveVersions.push(version); return pendingSave.promise }
  fake.createFile = () => pendingCreate.promise
  fake.deleteFile = (...args: unknown[]) => { deletes.push(args); return pendingDelete.promise }
  let renderer!: TestRenderer.ReactTestRenderer
  await act(async () => { renderer = TestRenderer.create(<LanguageProvider><App /></LanguageProvider>); await new Promise(r => setTimeout(r,0)) })
  const pane = () => renderer.root.findByType(FilesPane)
  const resolveRead = async (path:string, content:string) => { await act(async () => { reads[path].resolve({path,content,binary:false,version:'0'.repeat(64)}); await Promise.resolve() }) }
  await act(async () => { pane().props.onOpen('no-version.txt') })
  await act(async () => { reads['no-version.txt'].resolve({path:'no-version.txt',content:'read only',binary:false}); await Promise.resolve() })
  assert.equal(pane().props.missingVersion, true)
  await act(async () => { pane().props.onChange('blocked'); pane().props.onSave() })
  assert.equal(pane().props.content, 'read only')
  assert.equal(saveCalls, 0, 'server without version cannot cause an unconditional editor save')
  await act(async () => { pane().props.onDelete() })
  assert.equal(deletes.length, 0, 'server without version cannot cause an unconditional editor delete')
  await act(async () => { pane().props.onOpen('slow.txt'); pane().props.onOpen('fast.txt') })
  await resolveRead('fast.txt', 'fast')
  await resolveRead('slow.txt', 'slow')
  assert.equal(pane().props.selected, 'fast.txt', 'stale read must not replace newer selection')
  assert.equal(pane().props.content, 'fast')
  await act(async () => { pane().props.onChange('version 1') })
  await act(async () => { pane().props.onSave() })
  await act(async () => { pane().props.onChange('version 2'); pane().props.onSave() })
  assert.equal(saveCalls, 1, 'must not start overlapping editor saves')
  assert.equal(pane().props.saving, true)
  await act(async () => { pendingSave.resolve({ok:true,version:'1'.repeat(64),checkpoint_status:'created'}); await Promise.resolve() })
  assert.equal(pane().props.content, 'version 2')
  assert.equal(pane().props.dirty, true, 'save of old version must not mark newer edit saved')
  assert.equal(pane().props.saving, false)
  await act(async () => { pane().props.onOpen('edit-race.txt'); pane().props.onChange('typing during read') })
  await resolveRead('edit-race.txt', 'discard this')
  assert.equal(pane().props.selected, 'fast.txt')
  assert.equal(pane().props.content, 'typing during read')
  pendingSave = deferred<any>()
  await act(async () => { pane().props.onSave(); pane().props.onOpen('other.txt') })
  assert.deepEqual(saveVersions, ['0'.repeat(64), '1'.repeat(64)], 'next save uses confirmed version despite an edit during the previous save')
  await resolveRead('other.txt', 'other')
  await act(async () => { pane().props.onChange('other edited') })
  await act(async () => { pendingSave.resolve({ok:true,version:'1'.repeat(64),checkpoint_status:'created'}); await Promise.resolve() })
  assert.equal(pane().props.selected, 'other.txt')
  assert.equal(pane().props.dirty, true, 'save from another file must not clear dirty')
  pendingSave = deferred<any>()
  await act(async () => { pane().props.onSave() })
  assert.equal(saveVersions.at(-1), '0'.repeat(64), 'late save response must not replace another file version')
  await act(async () => { pendingSave.resolve({ok:true,checkpoint_status:'created'}); await Promise.resolve() })
  assert.equal(pane().props.missingVersion, true, 'missing save version must fail closed')
  await act(async () => { pane().props.onChange('should not replace content'); pane().props.onSave() })
  assert.equal(pane().props.content, 'other edited')
  assert.equal(saveCalls, 3)
  await act(async () => { pane().props.onNewFile(); pane().props.onOpen('later.txt') })
  await resolveRead('later.txt', 'later')
  await act(async () => { pendingCreate.resolve({ok:true,checkpoint_status:'created'}); await Promise.resolve() })
  assert.equal(pane().props.selected, 'later.txt', 'late create must not start a new stale open')
  assert.equal(reads['created.txt'], undefined)
  await act(async () => { pane().props.onDelete(); pane().props.onOpen('after-delete.txt') })
  assert.deepEqual(deletes, [['s', 'later.txt', '0'.repeat(64)]])
  await resolveRead('after-delete.txt', 'new selection')
  await act(async () => { pane().props.onChange('new selection edited') })
  await act(async () => { pendingDelete.resolve({ok:true,checkpoint_status:'created'}); await Promise.resolve() })
  assert.equal(pane().props.selected, 'after-delete.txt', 'late deletion must not close new selection')
  assert.equal(pane().props.dirty, true)
  await act(async () => { pane().props.onOpen('bad-old.txt'); pane().props.onOpen('good.txt') })
  await resolveRead('good.txt', 'good')
  await act(async () => { reads['bad-old.txt'].reject(new Error('stale read error')); await Promise.resolve() })
  assert.doesNotMatch(JSON.stringify(renderer.toJSON()), /stale read error/)
  await act(async () => { pane().props.onOpen('pending.txt') })
  await act(async () => { listeners['forge:unauthorized']() })
  await resolveRead('pending.txt','private old session')
  assert.doesNotMatch(JSON.stringify(renderer.toJSON()), /private old session/)
  await act(async () => { renderer.unmount() })
  console.log('workspace editor stale open/save/create and logout race tests: passed')
}
main().then(() => process.exit(0), e => { console.error(e); process.exit(1) })
