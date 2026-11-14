import assert from 'node:assert/strict'
import { api } from '../src/api'
async function main() {
  const calls: {url: string, init: RequestInit}[] = []
  globalThis.fetch = (async (url: string | URL | Request, init?: RequestInit) => {
    calls.push({url:String(url), init:init ?? {}})
    return new Response(JSON.stringify({ok:true, checkpoint_status:'created'}), {status:200, headers:{'content-type':'application/json'}})
  }) as typeof fetch
  const result = await api.createFile('session id', 'existing.bin')
  assert.equal(result.checkpoint_status, 'created')
  assert.equal(calls.length, 1)
  assert.equal(calls[0].url, '/api/workspace/file?sid=session%20id')
  assert.equal(calls[0].init.method, 'POST')
  assert.deepEqual(JSON.parse(String(calls[0].init.body)), {path:'existing.bin',content:''})
  console.log('New File API uses exclusive create endpoint: passed')
}
main().catch(err => { console.error(err); process.exitCode = 1 })
