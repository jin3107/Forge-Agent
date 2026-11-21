import React from 'react'
import assert from 'node:assert/strict'
import TestRenderer, {act} from 'react-test-renderer'
import {LanguageProvider} from '../src/i18n'
import {HookAdminReview} from '../src/components/HookAdminReview'
import {api} from '../src/api'

const detail={request_id:'r1',owner_id:'owner',session_id:'s',workspace:'/workspace',digest:'a'.repeat(64),status:'pending' as const,hooks:{stop:'</pre><script>evil()</script>'},on_file_change:null,overrides_global_hooks:true,enforcement_active:false}
async function main() {
  let allow=false, confirmText='', decisions=0, sent:any=null
  Object.assign(globalThis,{localStorage:{getItem:()=> 'en',setItem:()=>{}},document:{documentElement:{lang:''}},confirm:(v:string)=>{confirmText=v;return allow}})
  const original={...api}
  ;(api as any).listHookReviewRequests=async()=>({requests:[{...detail,snapshot_revalidated:false}],enforcement_active:false})
  ;(api as any).getHookReviewRequest=async()=>detail
  ;(api as any).decideHookReviewRequest=async(...args:any[])=>{decisions++;sent=args;return {request_id:'r1',status:'approved',enforcement_active:false}}
  let r!:TestRenderer.ReactTestRenderer
  try {
    await act(async()=>{r=TestRenderer.create(<LanguageProvider><HookAdminReview/></LanguageProvider>)})
    const b=(name:string)=>r.root.findByProps({'aria-label':name})
    assert.equal(r.root.findAllByProps({'aria-label':'Approve hook request'}).length,0)
    await act(async()=>{await b('List admin hook requests').props.onClick()})
    await act(async()=>{await b('Review hook request r1').props.onClick()})
    const rendered=JSON.stringify(r.toJSON())
    assert.match(rendered,/not enforced/i); assert.match(rendered,/overrides global/i); assert.match(rendered,/evil\(\)/)
    assert.equal(r.root.findAllByType('script').length,0)
    await act(async()=>{await b('Approve hook request').props.onClick()})
    assert.equal(decisions,0); assert.match(confirmText,/not enforced/i); assert.match(confirmText,/\/workspace/)
    allow=true
    await act(async()=>{await b('Approve hook request').props.onClick()})
    assert.equal(decisions,1); assert.deepEqual(sent,['r1','approve','/workspace','a'.repeat(64)])
    assert.match(JSON.stringify(r.toJSON()),/not enforced/i)
    await act(async()=>{await b('List admin hook requests').props.onClick()})
    await act(async()=>{await b('Review hook request r1').props.onClick()})
    ;(api as any).decideHookReviewRequest=async()=>{throw new Error('connection lost')}
    await act(async()=>{await b('Deny hook request').props.onClick()})
    assert.match(JSON.stringify(r.toJSON()),/may have been recorded/i)
    assert.equal(r.root.findAllByProps({'aria-label':'Approve hook request'}).length,0)
    await act(async()=>{await b('List admin hook requests').props.onClick()})
    ;(api as any).getHookReviewRequest=async()=>({...detail,request_id:'wrong'})
    await act(async()=>{await b('Review hook request r1').props.onClick()})
    assert.equal(r.root.findAllByProps({'aria-label':'Approve hook request'}).length,0, 'mismatched details must not be actionable')
    assert.match(JSON.stringify(r.toJSON()),/invalid/i)
  } finally {r?.unmount();Object.assign(api,original)}
  console.log('admin review requires preview and confirmation, never claims enforcement: passed')
}
main().then(()=>process.exit(0),e=>{console.error(e);process.exit(1)})
