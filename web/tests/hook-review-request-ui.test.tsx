import React from 'react'
import assert from 'node:assert/strict'
import TestRenderer, {act} from 'react-test-renderer'
import {LanguageProvider} from '../src/i18n'
import {HookReviewRequests} from '../src/components/HookReviewRequests'
import {api} from '../src/api'

const preview = {workspace:'/tmp/workspace',state:'hooks' as const,digest:'a'.repeat(64),hooks:{stop:'secret-command'},on_file_change:null,overrides_global_hooks:true,enforcement_active:false}
function deferred() {let resolve!:(v:any)=>void; const promise=new Promise<any>(r=>{resolve=r}); return {promise,resolve}}
async function main() {
  let confirmed=false, confirmText='', calls=0, request=deferred()
  Object.assign(globalThis,{localStorage:{getItem:()=> 'en',setItem:()=>{}},document:{documentElement:{lang:''}},confirm:(text:string)=>{confirmText=text;return confirmed}})
  const original={...api}
  ;(api as any).requestHookReview=()=>{calls++;return request.promise}
  ;(api as any).listHookReviewRequests=async()=>({requests:[{request_id:'req1',owner_id:'u',session_id:'s1',workspace:'/tmp/workspace',digest:'a'.repeat(64),status:'pending',snapshot_revalidated:false}],enforcement_active:false})
  ;(api as any).revokeHookReviewRequest=async()=>({ok:true,enforcement_active:false})
  let r!:TestRenderer.ReactTestRenderer
  try {
    await act(async()=>{r=TestRenderer.create(<LanguageProvider><HookReviewRequests sid="s1" preview={preview}/></LanguageProvider>)})
    const button=(name:string)=>r.root.findByProps({'aria-label':name})
    await act(async()=>{button('Request admin hook review').props.onClick()})
    assert.equal(calls,0)
    assert.match(confirmText,/all admins/i); assert.match(confirmText,/sensitive/i); assert.match(confirmText,/not enforced/i)
    confirmed=true
    await act(async()=>{button('Request admin hook review').props.onClick(); button('Request admin hook review').props.onClick()})
    assert.equal(calls,1,'duplicate submits are serialized')
    await act(async()=>{request.resolve({request_id:'req1',enforcement_active:false});await Promise.resolve()})
    assert.match(JSON.stringify(r.toJSON()),/not enforced/i)
    await act(async()=>{await button('List my hook review requests').props.onClick()})
    assert.match(JSON.stringify(r.toJSON()),/req1/)
    assert.match(JSON.stringify(r.toJSON()),/not revalidated/i)
    await act(async()=>{await button('Revoke hook review request req1').props.onClick()})
    assert.match(JSON.stringify(r.toJSON()),/revoked/i)
    request=deferred()
    await act(async()=>{button('Request admin hook review').props.onClick()})
    await act(async()=>{r.update(<LanguageProvider><HookReviewRequests sid="s2" preview={preview}/></LanguageProvider>)})
    await act(async()=>{request.resolve({request_id:'late-s1',enforcement_active:false});await Promise.resolve()})
    assert.doesNotMatch(JSON.stringify(r.toJSON()),/late-s1/)
    ;(api as any).requestHookReview=async()=>{throw new Error('network failed')}
    await act(async()=>{await button('Request admin hook review').props.onClick()})
    assert.match(JSON.stringify(r.toJSON()),/may have been created/i)
  } finally {r?.unmount();Object.assign(api,original)}
  console.log('explicit hook request consent, status, revoke and races: passed')
}
main().then(()=>process.exit(0),e=>{console.error(e);process.exit(1)})
