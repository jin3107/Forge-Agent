import React from 'react'
import assert from 'node:assert/strict'
import TestRenderer, {act} from 'react-test-renderer'
import App from '../src/App'
import {api} from '../src/api'
import {LanguageProvider} from '../src/i18n'

async function main() {
  Object.assign(globalThis, {
    window:{location:{search:''},matchMedia:()=>({matches:true}),addEventListener:()=>{},removeEventListener:()=>{}},
    location:{search:''}, document:{documentElement:{lang:''}},
    localStorage:{getItem:()=> 'en',setItem:()=>{}},
  })
  const fake=api as any
  fake.authMe=async()=>({user:{id:'u',role:'member',email:'u@test',name:'User'},google_enabled:false,registration_enabled:false,needs_setup:false})
  fake.health=async()=>{throw new Error('no settings')}
  fake.models=async()=>({models:[]})
  fake.listSessions=async()=>[{id:'s',title:'Test',model:'demo',created_at:'',updated_at:''}]
  fake.getSession=async()=>({id:'s',model:'demo',messages:[]})
  fake.context=async()=>{throw new Error('no context')}
  fake.tree=async()=>[]
  fake.batchStream=async(_prompts:any, callback:any)=>{
    callback({type:'task_created',index:0,session_id:'new',title:'task'})
    callback({type:'task_event',index:0,event:{type:'error',code:'hook_trust_denied',message:'Earlier operations may have run'}})
    callback({type:'task_event',index:0,event:{type:'task_done',index:0,session_id:'new',ok:false}})
    callback({type:'batch_done',ok:false,tasks:[{index:0,session_id:'new',title:'task',ok:false}]})
  }
  let renderer!:TestRenderer.ReactTestRenderer
  await act(async()=>{renderer=TestRenderer.create(<LanguageProvider><App /></LanguageProvider>);await new Promise(r=>setTimeout(r,0))})
  const button=renderer.root.findByProps({className:'parallel-toggle '})
  await act(async()=>{button.props.onClick()})
  const textarea=renderer.root.findByType('textarea')
  await act(async()=>{textarea.props.onChange({target:{value:'task'}})})
  await act(async()=>{renderer.root.findByProps({className:'btn-send'}).props.onClick(); await new Promise(r=>setTimeout(r,0))})
  const cards=renderer.root.findAllByProps({className:'st st-bad'})
  assert.equal(cards.length,1, 'denied task must remain error after task_done and batch_done')
  assert.match(JSON.stringify(renderer.toJSON()),/Earlier operations may have run/)
  renderer.unmount()
  console.log('batch denial remains visible as an error: passed')
}
main().then(()=>process.exit(0),e=>{console.error(e);process.exit(1)})
