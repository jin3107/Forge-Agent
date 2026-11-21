import React from 'react'
import assert from 'node:assert/strict'
import TestRenderer, { act } from 'react-test-renderer'
import App from '../src/App'
import { api } from '../src/api'
import { LanguageProvider } from '../src/i18n'
import { FilesPane } from '../src/components/FilesPane'
import { Sidebar } from '../src/components/Sidebar'

function deferred<T>() { let resolve!: (x:T) => void; let reject!: (e:Error) => void; const promise = new Promise<T>((r, j) => { resolve = r; reject = j }); return {resolve,reject,promise} }
async function main() {
  const listeners: Record<string, (event:any) => void> = {}
  let confirmed = false
  let prompts = 0
  let created = 0
  let reads: string[] = []
  let createSession = deferred<any>()
  let deleting = deferred<any>()
  const loggingOut = deferred<any>()
  let deleteCalls = 0, deleteFileCalls = 0, logoutCalls = 0, forkCalls = 0
  let lastConfirm = ''
  Object.assign(globalThis, {
    window: {location:{search:''}, matchMedia: () => ({matches:true}), addEventListener: (name:string,fn:any) => { listeners[name]=fn }, removeEventListener: () => {}},
    document: {documentElement:{lang:''}}, localStorage:{getItem: () => 'en',setItem: () => {}},
    confirm: (message:string) => { prompts++; lastConfirm = message; return confirmed }, prompt: () => 'new.txt',
  })
  const fake = api as any
  fake.authMe = async () => ({user:{id:'u',role:'member',email:'u@test',name:'User'},google_enabled:false,registration_enabled:false,needs_setup:false})
  fake.health = async () => ({model:'demo',demo:false})
  fake.models = async () => ({models:[]})
  fake.listSessions = async () => [{id:'s',title:'Test',model:'demo',updated_at:0,message_count:0}, {id:'other',title:'Other',model:'demo',updated_at:0,message_count:0}]
  fake.getSession = async (id:string) => ({id,model:'demo',messages:[]})
  fake.context = async () => { throw new Error('no context') }
  fake.tree = async () => []
  fake.readFile = async (_sid:string,path:string) => { reads.push(path); return {path,binary:false,version:"0".repeat(64),content:'original'} }
  fake.createFile = async () => { created++; return {ok:true,checkpoint_status:'created'} }
  fake.createSession = () => { created++; return createSession.promise }
  fake.deleteFile = async () => { deleteFileCalls++; return {ok:true,checkpoint_status:'created'} }
  fake.deleteSession = () => { deleteCalls++; return deleting.promise }
  fake.logout = () => { logoutCalls++; return loggingOut.promise }
  fake.fork = async () => { forkCalls++; return {session:{id:'fork',model:'demo'}} }
  let renderer!: TestRenderer.ReactTestRenderer
  await act(async () => { renderer = TestRenderer.create(<LanguageProvider><App /></LanguageProvider>); await new Promise(r=>setTimeout(r,0)) })
  const pane = () => renderer.root.findByType(FilesPane)
  const sidebar = () => renderer.root.findByType(Sidebar)
  await act(async () => { await pane().props.onOpen('current.txt') })
  await act(async () => { pane().props.onChange('unsaved') })
  await act(async () => { await pane().props.onOpen('other.txt') })
  assert.deepEqual(reads, ['current.txt'])
  assert.equal(pane().props.content, 'unsaved')
  assert.equal(pane().props.dirty, true)
  await act(async () => { await sidebar().props.onSelect('other') })
  assert.equal(pane().props.sid, 's')
  const newButton = () => sidebar().findAllByType('button').find(b => b.props.className === 'btn-primary')!
  await act(async () => { await newButton().props.onClick({type:'click'}) })
  await act(async () => { await pane().props.onNewFile() })
  await act(async () => { await pane().props.onDelete() })
  assert.match(lastConfirm, /Unsaved edits/)
  assert.equal(deleteFileCalls, 0)
  await act(async () => { await sidebar().props.onDelete('s'); await sidebar().props.onLogout() })
  const fork = () => renderer.root.findAllByType('button').find(b => String(b.props.title).startsWith('Fork this session'))!
  await act(async () => { await fork().props.onClick() })
  assert.equal(created, 0)
  assert.equal(deleteCalls, 0)
  assert.equal(logoutCalls, 0)
  assert.equal(forkCalls, 0)
  assert.ok(prompts >= 7)
  let prevented = false
  const event:any = {preventDefault: () => { prevented=true }, returnValue: undefined}
  listeners['beforeunload']?.(event)
  assert.equal(prevented, true, 'unload must request browser confirmation for dirty editor')
  confirmed = true
  await act(async () => { pane().props.onOpen('other.txt') })
  assert.equal(pane().props.selected, 'other.txt')
  assert.equal(pane().props.dirty, false)
  await act(async () => { await sidebar().props.onSelect('other') })
  assert.equal(pane().props.sid, 'other')
  await act(async () => { await sidebar().props.onSelect('s') })
  await act(async () => { await pane().props.onOpen('other.txt') })
  await act(async () => { pane().props.onChange('first edit') })
  await act(async () => { sidebar().props.onNew() })
  await act(async () => { pane().props.onChange('edit during session create') })
  await act(async () => { createSession.resolve({id:'new-session',model:'demo'}); await Promise.resolve() })
  assert.equal(pane().props.sid,'s', 'delayed session creation must not discard later edits')
  assert.equal(pane().props.content,'edit during session create')
  await act(async () => { sidebar().props.onDelete('s') })
  assert.equal(pane().props.locked, true)
  await act(async () => { pane().props.onChange('must be ignored'); sidebar().props.onSelect('other') })
  assert.equal(pane().props.content, 'edit during session create')
  assert.equal(pane().props.sid, 's')
  await act(async () => { deleting.reject(new Error('delete failed')); await Promise.resolve() })
  assert.equal(pane().props.locked, false)
  assert.equal(pane().props.dirty, true)
  deleting = deferred<any>()
  createSession = deferred<any>()
  await act(async () => { sidebar().props.onDelete('s') })
  await act(async () => { deleting.resolve({}); await Promise.resolve() })
  assert.equal(pane().props.locked, true)
  await act(async () => { createSession.resolve({id:'after-delete',model:'demo'}); await Promise.resolve() })
  assert.equal(pane().props.sid, 'after-delete')
  assert.equal(pane().props.locked, false)
  await act(async () => { await pane().props.onOpen('new-current.txt') })
  await act(async () => { pane().props.onChange('logout unsaved') })
  await act(async () => { sidebar().props.onLogout() })
  assert.equal(pane().props.locked, true)
  await act(async () => { pane().props.onChange('ignored during logout') })
  assert.equal(pane().props.content, 'logout unsaved')
  await act(async () => { loggingOut.resolve({}); await Promise.resolve() })
  assert.equal(logoutCalls, 1)
  await act(async () => { renderer.unmount() })
  console.log('unsaved file and session navigation, unload and delayed creation: passed')
}
main().then(() => process.exit(0), e => { console.error(e); process.exit(1) })
