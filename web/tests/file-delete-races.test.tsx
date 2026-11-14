import React from 'react'
import assert from 'node:assert/strict'
import TestRenderer, {act} from 'react-test-renderer'
import App from '../src/App'
import {api} from '../src/api'
import {LanguageProvider} from '../src/i18n'
import {FilesPane} from '../src/components/FilesPane'
function deferred() { let resolve!: (x:any)=>void; let reject!: (e:Error)=>void; const promise = new Promise<any>((a,b)=>{resolve=a;reject=b}); return {resolve,reject,promise} }
async function main() {
  Object.assign(globalThis, { window:{matchMedia:()=>({matches:true}),addEventListener:()=>{},removeEventListener:()=>{}}, document:{documentElement:{lang:''}}, localStorage:{getItem:()=> 'en',setItem:()=>{}}, confirm:()=>true })
  const fake=api as any
  fake.authMe=async()=>({user:{id:'u',role:'member',email:'u@test',name:'User'},google_enabled:false,registration_enabled:false,needs_setup:false})
  fake.health=async()=>{throw new Error('settings')}; fake.models=async()=>({models:[]})
  fake.listSessions=async()=>[{id:'s',title:'Test',model:'demo'}]; fake.getSession=async()=>({id:'s',model:'demo',messages:[]})
  fake.context=async()=>{throw new Error('context')}; fake.tree=async()=>[]
  fake.readFile=async (_:string,path:string)=>({path,content:'original',binary:false,version:'0'.repeat(64)})
  let deletion=deferred(), save=deferred(), deletes=0, saves=0
  fake.deleteFile=()=>{deletes++;return deletion.promise}
  fake.saveFile=()=>{saves++;return save.promise}
  let renderer!: TestRenderer.ReactTestRenderer
  await act(async()=>{renderer=TestRenderer.create(<LanguageProvider><App /></LanguageProvider>);await new Promise(r=>setTimeout(r,0))})
  const pane=()=>renderer.root.findByType(FilesPane)
  await act(async()=>{await pane().props.onOpen('file.txt')})
  await act(async()=>{pane().props.onDelete();pane().props.onDelete()})
  assert.equal(deletes,1,'serialize deletion requests')
  assert.equal(pane().props.deleting,true)
  await act(async()=>{pane().props.onChange('draft during deletion')})
  await act(async()=>{pane().props.onSave()})
  assert.equal(saves,0,'cannot save while delete is in flight')
  await act(async()=>{deletion.resolve({ok:true,checkpoint_status:'created'});await Promise.resolve()})
  assert.equal(pane().props.content,'draft during deletion')
  assert.equal(pane().props.dirty,true)
  assert.equal(pane().props.missingVersion,true,'deleted file must not be saved as if it still existed')
  assert.match(JSON.stringify(renderer.toJSON()),/deleted.*copy/i)
  assert.equal(pane().props.deleting,false)
  await act(async()=>{await pane().props.onOpen('another.txt')})
  await act(async()=>{pane().props.onChange('save first')})
  await act(async()=>{pane().props.onSave();pane().props.onDelete()})
  assert.equal(saves,1)
  assert.equal(deletes,1,'cannot delete while save is in flight')
  await act(async()=>{save.resolve({ok:true,version:'1'.repeat(64),checkpoint_status:'created'});await Promise.resolve()})
  deletion=deferred()
  await act(async()=>{pane().props.onDelete()})
  assert.equal(deletes,2)
  await act(async()=>{deletion.reject(new Error('delete failed'));await Promise.resolve()})
  assert.equal(pane().props.deleting,false,'failure releases guard')
  assert.match(JSON.stringify(renderer.toJSON()),/delete failed/)
  assert.match(JSON.stringify(renderer.toJSON()),/may have been deleted/)
  deletion=deferred()
  await act(async()=>{pane().props.onDelete()})
  await act(async()=>{pane().props.onChange('new draft')})
  await act(async()=>{deletion.resolve({ok:true,checkpoint_status:'failed'});await Promise.resolve()})
  assert.match(JSON.stringify(renderer.toJSON()),/checkpoint creation failed/i)
  assert.match(JSON.stringify(renderer.toJSON()),/deleted.*copy/i)
  assert.equal(pane().props.content,'new draft')
  assert.equal(pane().props.missingVersion,true)
  await act(async()=>{renderer.unmount()})
  console.log('editor serializes save/delete and preserves drafts during deletion: passed')
}
main().then(()=>process.exit(0),e=>{console.error(e);process.exit(1)})
