import React from 'react'
import assert from 'node:assert/strict'
import TestRenderer, {act} from 'react-test-renderer'
import { renderToStaticMarkup } from 'react-dom/server'
import { HookReview } from '../src/components/HookReview'
import { LanguageProvider } from '../src/i18n'
import { api } from '../src/api'

function deferred() { let resolve!: (v:any)=>void; const promise=new Promise<any>(r=>{resolve=r}); return {promise, resolve} }
async function main() {
  Object.assign(globalThis, { localStorage:{getItem:()=> 'en', setItem:()=>{}}, document:{documentElement:{lang:''}} })
  const original = api.reviewWorkspaceHooks
  let request = deferred(); let calls = 0
  ;(api as any).reviewWorkspaceHooks = () => {calls++; return request.promise}
  const payload = {workspace:'/workspace/one',state:'hooks',digest:'a'.repeat(64),hooks:{stop:'</pre><script>evil()</script>'},on_file_change:null,overrides_global_hooks:true,enforcement_active:false}
  let renderer!:TestRenderer.ReactTestRenderer
  try {
    await act(async()=>{renderer=TestRenderer.create(<LanguageProvider><HookReview sid="one" /></LanguageProvider>)})
    assert.equal(calls,0, 'review requires explicit click')
    assert.equal(renderer.root.findAllByProps({'aria-label':'List admin hook requests'}).length,0)
    await act(async()=>{renderer.update(<LanguageProvider><HookReview sid="one" isAdmin /></LanguageProvider>)})
    assert.equal(renderer.root.findAllByProps({'aria-label':'List admin hook requests'}).length,1)
    await act(async()=>{renderer.update(<LanguageProvider><HookReview sid="one" /></LanguageProvider>)})
    assert.equal(renderer.root.findAllByProps({'aria-label':'List admin hook requests'}).length,0)
    const button=()=>renderer.root.findByProps({'aria-label':'Review workspace hooks'})
    await act(async()=>{button().props.onClick()})
    assert.equal(calls,1)
    await act(async()=>{request.resolve(payload);await Promise.resolve()})
    const rendered=JSON.stringify(renderer.toJSON())
    assert.match(rendered,/not enforced/i)
    assert.match(rendered,/overrides global/i)
    assert.match(rendered,/\/workspace\/one/)
    assert.match(rendered,/evil\(\)/)
    assert.equal(renderer.root.findAllByType('script').length,0)
    const html=renderToStaticMarkup(<pre>{JSON.stringify(payload.hooks)}</pre>)
    assert.doesNotMatch(html, /<script>/)
    request=deferred()
    await act(async()=>{button().props.onClick()})
    await act(async()=>{renderer.update(<LanguageProvider><HookReview sid="two" /></LanguageProvider>)})
    await act(async()=>{request.resolve(payload);await Promise.resolve()})
    assert.doesNotMatch(JSON.stringify(renderer.toJSON()), /evil\(\)/, 'stale response must not appear in another session')
    ;(api as any).reviewWorkspaceHooks = async()=>{throw new Error('offline')}
    await act(async()=>{button().props.onClick();await Promise.resolve()})
    assert.match(JSON.stringify(renderer.toJSON()), /offline/)
    assert.match(JSON.stringify(renderer.toJSON()), /not enforced/i)
  } finally { renderer?.unmount(); (api as any).reviewWorkspaceHooks=original }
  console.log('hook preview is explicit, read-only, escaped and session-scoped: passed')
}
main().then(()=>process.exit(0), e=>{console.error(e);process.exit(1)})
