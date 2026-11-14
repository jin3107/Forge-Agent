import assert from 'node:assert/strict'
import {api} from '../src/api'

async function main() {
  const original=globalThis.fetch
  const calls:{url:string,init:RequestInit}[]=[]
  globalThis.fetch=async (input:any, init:any={})=>{
    calls.push({url:String(input),init})
    if (String(input)==='/api/auth/me') return new Response(JSON.stringify({csrf_token:'csrf',user:null}),{status:200})
    if (init.method==='DELETE') return new Response(JSON.stringify({ok:true,enforcement_active:false}),{status:200})
    return new Response(JSON.stringify({request_id:'id',enforcement_active:false}),{status:200})
  }
  try {
    await api.authMe()
    await api.requestHookReview('s','/workspace','a'.repeat(64))
    assert.equal(calls[1].url,'/api/workspace/hook-review-requests')
    assert.equal(new Headers(calls[1].init.headers).get('X-CSRF-Token'),'csrf')
    assert.deepEqual(JSON.parse(String(calls[1].init.body)),{sid:'s',expected_workspace:'/workspace',expected_digest:'a'.repeat(64),share_commands:true})
    await api.revokeHookReviewRequest('a/b')
    assert.equal(calls[2].url,'/api/workspace/hook-review-requests/a%2Fb')
    assert.equal(new Headers(calls[2].init.headers).get('X-CSRF-Token'),'csrf')
    await api.decideHookReviewRequest('a/b','deny','/workspace','a'.repeat(64))
    assert.equal(calls[3].url,'/api/workspace/hook-review-requests/a%2Fb/decision')
    assert.equal(new Headers(calls[3].init.headers).get('X-CSRF-Token'),'csrf')
    assert.deepEqual(JSON.parse(String(calls[3].init.body)),{decision:'deny',expected_workspace:'/workspace',expected_digest:'a'.repeat(64)})
  } finally {globalThis.fetch=original}
  console.log('hook request API sends explicit consent and CSRF: passed')
}
main().then(()=>process.exit(0),e=>{console.error(e);process.exit(1)})
