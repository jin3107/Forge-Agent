import assert from 'node:assert/strict'
import {api} from '../src/api'
async function main() {
  const calls: {url:string, init:RequestInit}[]=[]
  globalThis.fetch=(async (url:string|URL|Request,init?:RequestInit)=>{
    calls.push({url:String(url),init:init??{}})
    return new Response(JSON.stringify({detail:'Not Found'}),{status:404,headers:{'content-type':'application/json'}})
  }) as typeof fetch
  const version='a'.repeat(64)
  await assert.rejects(api.saveFile('session id','a.txt','draft',version))
  assert.equal(calls.length,1,'must not fall back to an old overwrite endpoint')
  assert.equal(calls[0].url,'/api/workspace/file/conditional?sid=session%20id')
  assert.equal(calls[0].init.method,'PUT')
  assert.deepEqual(JSON.parse(String(calls[0].init.body)),{path:'a.txt',content:'draft',expected_version:version})
  console.log('conditional save uses a distinct route without legacy fallback: passed')
}
main().catch(e=>{console.error(e);process.exitCode=1})
