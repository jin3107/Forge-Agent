import assert from 'node:assert/strict'
import { api } from '../src/api'
async function main() {
  const calls: {url: string, init: RequestInit}[] = []
  globalThis.fetch = (async (url: string | URL | Request, init?: RequestInit) => {
    calls.push({url:String(url), init:init ?? {}})
    // Old servers do not know this route: the client must not fall back to legacy DELETE.
    return new Response(JSON.stringify({detail:'Not Found'}), {status:404, headers:{'content-type':'application/json'}})
  }) as typeof fetch
  await assert.rejects(api.deleteFile('session id', 'my file.bin', '0'.repeat(64)))
  assert.equal(calls.length, 1)
  assert.equal(calls[0].url, `/api/workspace/file/conditional?sid=session%20id&path=my%20file.bin&expected_version=${'0'.repeat(64)}`)
  assert.equal(calls[0].init.method, 'DELETE')
  console.log('conditional deletion uses a distinct route without legacy fallback: passed')
}
main().catch(err => {console.error(err); process.exitCode=1})
