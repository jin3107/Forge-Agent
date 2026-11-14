import React from 'react'
import assert from 'node:assert/strict'
import TestRenderer, { act } from 'react-test-renderer'
import App from '../src/App'
import { api } from '../src/api'
import { LanguageProvider } from '../src/i18n'
import { FilesPane } from '../src/components/FilesPane'

async function main() {
  Object.assign(globalThis, {
    window:{matchMedia:()=>({matches:true}),addEventListener:()=>{},removeEventListener:()=>{}},
    document:{documentElement:{lang:''}}, localStorage:{getItem:()=> 'en',setItem:()=>{}},
  })
  const writes: unknown[][] = []
  const fake = api as any
  fake.authMe = async () => ({user:{id:'u',role:'member',email:'u@test',name:'User'},google_enabled:false,registration_enabled:false,needs_setup:false})
  fake.health = async () => { throw new Error('no settings') }
  fake.models = async () => ({models:[]})
  fake.listSessions = async () => [{id:'s',title:'Test',model:'demo',updated_at:0,message_count:0}]
  fake.getSession = async () => ({id:'s',model:'demo',messages:[]})
  fake.context = async () => { throw new Error('no context') }
  fake.tree = async () => []
  const contents: Record<string,string> = {'windows':'a\r\nb\r\n', 'oldmac':'a\rb\r', 'unix':'a\nb\n', 'mixed':'a\r\nb\nc'}
  fake.readFile = async (_sid:string,path:string) => ({path,binary:false,version:"0".repeat(64),content:contents[path]})
  fake.saveFile = async (...args:unknown[]) => { writes.push(args); return {ok:true,version:'1'.repeat(64),checkpoint_status:'created'} }
  let renderer!: TestRenderer.ReactTestRenderer
  await act(async () => { renderer=TestRenderer.create(<LanguageProvider><App /></LanguageProvider>); await new Promise(r=>setTimeout(r,0)) })
  const pane=()=>renderer.root.findByType(FilesPane)
  for (const [name, separator] of [['windows','\r\n'],['oldmac','\r'],['unix','\n']]) {
    await act(async () => { await pane().props.onOpen(name) })
    assert.equal(pane().props.content,'a\nb\n')
    assert.equal(pane().props.mixedNewlines,false)
    await act(async () => { pane().props.onChange('a\nchanged\n') })
    await act(async () => { await pane().props.onSave() })
    assert.deepEqual(writes.at(-1), ['s',name,`a${separator}changed${separator}`, '0'.repeat(64)])
  }
  // A CRLF file can exceed the limit only after textarea newlines are restored.
  await act(async () => { await pane().props.onOpen('windows') })
  const largeDraft = 'a\n'.repeat(2 * 1024 * 1024)
  await act(async () => { pane().props.onChange(largeDraft) })
  await act(async () => { await pane().props.onSave() })
  assert.equal(writes.length, 3, 'oversized draft must not be sent')
  assert.equal(pane().props.content, largeDraft, 'draft remains editable')
  assert.equal(pane().props.dirty, true)
  assert.match(JSON.stringify(renderer.toJSON()), /exceeds 4 MiB/)
  await act(async () => { pane().props.onChange('shortened') })
  await act(async () => { await pane().props.onSave() })
  assert.equal(writes.length, 4)
  assert.equal(pane().props.dirty, false)
  await act(async () => { pane().props.onChange('draft\0after') })
  await act(async () => { await pane().props.onSave() })
  assert.equal(writes.length, 4, 'NUL-containing draft must not be sent')
  assert.equal(pane().props.content, 'draft\0after')
  assert.equal(pane().props.dirty, true)
  assert.match(JSON.stringify(renderer.toJSON()), /contains a NUL character/)
  await act(async () => { pane().props.onChange('draft after') })
  await act(async () => { await pane().props.onSave() })
  assert.equal(writes.length, 5)
  assert.equal(pane().props.dirty, false)
  await act(async () => { await pane().props.onOpen('mixed') })
  assert.equal(pane().props.mixedNewlines,true)
  await act(async () => { pane().props.onChange('corrupt'); await pane().props.onSave() })
  assert.equal(writes.length,5)
  assert.equal(pane().props.dirty,false)
  assert.equal(pane().props.content,'a\nb\nc')
  await act(async () => { renderer.unmount() })
  console.log('App preserves uniform line endings and blocks mixed file saves: passed')
}
main().then(()=>process.exit(0),e=>{console.error(e);process.exit(1)})
