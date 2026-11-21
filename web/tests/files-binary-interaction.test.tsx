import React from 'react'
import assert from 'node:assert/strict'
import TestRenderer, { act } from 'react-test-renderer'
import App from '../src/App'
import { api } from '../src/api'
import { LanguageProvider } from '../src/i18n'
import { FilesPane } from '../src/components/FilesPane'

async function main() {
  Object.assign(globalThis, {
    window: { matchMedia: () => ({matches: true}), addEventListener: () => {}, removeEventListener: () => {} },
    document: { documentElement: { lang: '' } },
    localStorage: { getItem: () => 'en', setItem: () => {} },
  })
  const writes: unknown[][] = []
  const creates: unknown[][] = []
  let promptPath = 'existing.bin'
  Object.assign(globalThis, { prompt: () => promptPath })
  const fake = api as any
  fake.authMe = async () => ({user:{id:'u', role:'member', email:'u@fixture', name:'User'}, google_enabled:false, registration_enabled:false, needs_setup:false})
  fake.health = async () => { throw new Error('fixture no settings') }
  fake.models = async () => ({models:[]})
  fake.listSessions = async () => [{id:'s', title:'Test', model:'demo', created_at:'', updated_at:''}]
  fake.getSession = async () => ({id:'s', model:'demo', messages:[]})
  fake.context = async () => { throw new Error('fixture no context') }
  fake.tree = async () => []
  fake.readFile = async (_sid: string, path: string) => path === 'image.bin'
    ? {path, binary:true, version:'0'.repeat(64), content:''} : {path, binary:false,version:"0".repeat(64), content:'text before'}
  fake.saveFile = async (...args: unknown[]) => { writes.push(args); return {ok:true, version:'1'.repeat(64), checkpoint_status:'created'} }
  fake.createFile = async (...args: unknown[]) => {
    creates.push(args)
    if (args[1] === 'existing.bin') throw new Error('HTTP 409 File already exists')
    return {ok:true, checkpoint_status:'failed'}
  }

  let renderer!: TestRenderer.ReactTestRenderer
  await act(async () => { renderer = TestRenderer.create(<LanguageProvider><App /></LanguageProvider>); await new Promise(r => setTimeout(r, 0)) })
  const pane = () => renderer.root.findByType(FilesPane)
  await act(async () => { await pane().props.onOpen('image.bin') })
  assert.equal(pane().props.binary, true)
  assert.match(pane().props.content, /binary/i)
  await act(async () => { pane().props.onChange('corrupt'); await pane().props.onSave() })
  assert.equal(writes.length, 0, 'binary preview must not call save API')
  assert.equal(pane().props.dirty, false)
  assert.match(pane().props.content, /binary/i)
  await act(async () => { await pane().props.onOpen('text.txt') })
  assert.equal(pane().props.binary, false)
  await act(async () => { pane().props.onChange('edited') })
  assert.equal(pane().props.dirty, true)
  await act(async () => { await pane().props.onSave() })
  assert.deepEqual(writes, [['s', 'text.txt', 'edited', '0'.repeat(64)]])
  assert.equal(pane().props.dirty, false)
  await act(async () => { await pane().props.onOpen('image.bin') })
  assert.equal(pane().props.binary, true)
  await act(async () => { await pane().props.onNewFile() })
  assert.deepEqual(creates, [['s', 'existing.bin']])
  assert.equal(writes.length, 1, 'New File must not call overwrite save API')
  assert.match(JSON.stringify(renderer.toJSON()), /HTTP 409 File already exists/)
  assert.equal(pane().props.selected, 'image.bin')
  promptPath = 'new.txt'
  await act(async () => { await pane().props.onNewFile() })
  assert.deepEqual(creates[1], ['s', 'new.txt'])
  assert.match(JSON.stringify(renderer.toJSON()), /checkpoint creation failed/)
  assert.equal(pane().props.selected, 'new.txt')
  await act(async () => { renderer.unmount() })
  console.log('App binary save guard, text save and exclusive New File interaction: passed')
}
main().then(() => process.exit(0), err => { console.error(err); process.exit(1) })
