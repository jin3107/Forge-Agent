// Real headless Chromium + real authenticated Uvicorn fixture; no provider calls.
// Run after npm ci, npx playwright install chromium, npm run build.
import assert from 'node:assert/strict'
import { mkdtemp, mkdir, readFile, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawn } from 'node:child_process'
import { chromium } from 'playwright'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const temp = await mkdtemp(path.join(tmpdir(), 'forge-browser-'))
const env = {...process.env, FORGE_DATA_DIR:path.join(temp,'data'), PYTHONPATH:[root,process.env.PYTHONPATH ?? ''].join(path.delimiter)}
for (const key of ['FORGE_PROFILE','FORGE_RELEASE_MODE','FORGE_COOKIE_SECURE','FORGE_PUBLIC_URL']) delete env[key]
const server = spawn('python', ['-u', path.join(root,'tests/fixtures/http_background_harness.py'), temp], {cwd:root, env, stdio:['ignore','pipe','pipe']})
let logs = ''
server.stdout.on('data', b => { logs += b.toString() })
server.stderr.on('data', b => { logs += b.toString() })
let browser
try {
  let base
  const deadline = Date.now()+15000
  while (Date.now() < deadline) {
    if (server.exitCode !== null || server.signalCode !== null) throw new Error(`server exited: ${logs}`)
    try {
      const port = (await readFile(path.join(temp,'port'),'utf8')).trim()
      base = `http://127.0.0.1:${port}`
      if ((await fetch(base+'/api/health')).ok) break
    } catch { /* server starting */ }
    await new Promise(r=>setTimeout(r, 40))
  }
  if (!base || !(await fetch(base+'/api/health')).ok) throw new Error(`server not ready: ${logs}`)
  browser = await chromium.launch({headless:true})
  const context = await browser.newContext({viewport:{width:1440,height:1000}})
  const req = context.request
  let response = await req.get(base+'/api/auth/me')
  let csrf = (await response.json()).csrf_token
  response = await req.post(base+'/api/auth/login', {headers:{'X-CSRF-Token':csrf}, data:{email:'alice@example.test',password:'fixture-password-at-least-16-characters'}})
  assert.equal(response.status(), 200, await response.text())
  csrf = (await response.json()).csrf_token
  const headers = {'X-CSRF-Token':csrf}
  response = await req.post(base+'/api/sessions', {headers, data:{}})
  assert.equal(response.status(),200,await response.text())
  const sid = (await response.json()).session.id
  for (const [name,content] of [['a.txt','original a'],['b.txt','original b'],['delete-target.txt','original delete'],['delayed-delete.txt','original delayed'],['crlf.txt','first\r\nsecond\r\n'],['cr.txt','first\rsecond\r'],['lf.txt','first\nsecond\n'],['mixed.txt','first\r\nsecond\nthird\r']]) {
    response = await req.put(base+`/api/workspace/file?sid=${sid}`, {headers, data:{path:name,content}})
    assert.equal(response.status(),200,await response.text())
  }
  const samples = [
    ['image.bin', Buffer.from([0,1,2,3,4])],
    ['legacy.bin', Buffer.from([0x63,0x61,0x66,0xe9])],
    ['late-nul.bin', Buffer.concat([Buffer.alloc(2000,0x61), Buffer.from([0x00,0x62])])],
  ]
  for (const [name, buffer] of samples) {
    response = await req.post(base+`/api/workspace/upload?sid=${sid}`, {headers,multipart:{files:{name,mimeType:'application/octet-stream',buffer}}})
    assert.equal(response.status(),200,await response.text())
  }
  const hookDir = path.join(temp, 'data', 'workspaces', sid, '.forge')
  await mkdir(hookDir, {recursive:true})
  await writeFile(path.join(hookDir, 'config.json'), JSON.stringify({hooks:{unknown:[], stop:'</pre><script>window.__hookInjection=true</script>'}}))
  const page = await context.newPage()
  await page.addInitScript(() => localStorage.setItem('forge-language','en'))
  const errors = []
  page.on('pageerror', e => errors.push(e.message))
  await page.goto(base, {waitUntil:'domcontentloaded'})
  await page.getByRole('button',{name:'a.txt'}).waitFor({timeout:15000})
  if (await page.getByRole('button',{name:'Close settings'}).isVisible()) await page.getByRole('button',{name:'Close settings'}).click()
  await page.getByRole('button',{name:'Review workspace hooks'}).click()
  await page.getByText('This configuration overrides global lifecycle hooks.').waitFor()
  await page.getByText(/Workspace when checked/).waitFor()
  assert.match(await page.locator('.hook-review').innerText(), new RegExp(sid))
  await page.getByText(/Hook trust is not enforced everywhere/).first().waitFor()
  assert.match(await page.locator('.hook-review').innerText(), /normal non-demo chat and manual compaction check approval/)
  assert.match(await page.locator('.hook-review').innerText(), /other paths may still run hooks/)
  assert.equal(await page.getByRole('button',{name:'Request admin hook review'}).count(), 1)
  assert.equal(await page.getByRole('button',{name:'List admin hook requests'}).count(), 1)
  await page.locator('.hook-review-config').getByText(/window.__hookInjection/).waitFor()
  assert.equal(await page.evaluate(() => window.__hookInjection), undefined)
  assert.equal(await page.locator('.hook-review-config script').count(), 0)
  page.once('dialog', async d => { assert.match(d.message(), /all admins/i); assert.match(d.message(), /sensitive/i); assert.match(d.message(), /not enforced/i); await d.dismiss() })
  await page.getByRole('button',{name:'Request admin hook review'}).click()
  assert.equal((await (await req.get(base+'/api/workspace/hook-review-requests')).json()).requests.length, 0)
  page.once('dialog', async d => { await d.accept() })
  await page.getByRole('button',{name:'Request admin hook review'}).click()
  await page.getByText(/Request .* submitted; hook trust is not enforced/).waitFor()
  const submitted = (await (await req.get(base+'/api/workspace/hook-review-requests')).json()).requests[0]
  await page.getByRole('button',{name:'List admin hook requests'}).click()
  await page.getByRole('button',{name:`Review hook request ${submitted.request_id}`}).click()
  await page.getByRole('button',{name:'Approve hook request'}).waitFor()
  assert.equal(await page.locator('.hook-admin-review script').count(), 0)
  page.once('dialog', async d => { assert.match(d.message(), /not enforced/i); await d.dismiss() })
  await page.getByRole('button',{name:'Approve hook request'}).click()
  assert.equal((await (await req.get(base+'/api/workspace/hook-review-requests')).json()).requests[0].status, 'pending')
  page.once('dialog', async d => { await d.accept() })
  await page.getByRole('button',{name:'Approve hook request'}).click()
  await page.getByText('Decision recorded; hook trust is not enforced everywhere. Normal non-demo chat and manual compaction check approval.').waitFor()
  assert.equal((await (await req.get(base+'/api/workspace/hook-review-requests')).json()).requests[0].status, 'approved')
  await page.getByRole('button',{name:'List my hook review requests'}).click()
  await page.getByText(/not revalidated/).first().waitFor()
  page.once('dialog', async d => { assert.match(d.message(), /cannot be recalled/i); await d.accept() })
  await page.getByRole('button',{name:/Revoke hook review request/}).click()
  await page.getByText('Request revoked.').waitFor()
  assert.equal((await (await req.get(base+'/api/workspace/hook-review-requests')).json()).requests.length, 0)
  await page.locator('input[aria-label="Choose files to upload"]').setInputFiles([
    {name:'a.txt',mimeType:'text/plain',buffer:Buffer.from('should not overwrite')},
    {name:'fresh.txt',mimeType:'text/plain',buffer:Buffer.from('uploaded new')},
    ...Array.from({length:19}, (_,i) => ({name:`batch-ui-${i+1}.txt`, mimeType:'text/plain', buffer:Buffer.from('new')})),
  ])
  await page.getByText(/a.txt \(already exists\)/).waitFor()
  await page.getByText(/#21: batch-ui-19.txt \(over 20-file limit\)/).waitFor()
  await page.getByRole('button',{name:'fresh.txt'}).waitFor()
  assert.equal((await readFile(path.join(temp,'data','workspaces',sid,'batch-ui-18.txt'))).toString('utf8'),'new')
  await assert.rejects(readFile(path.join(temp,'data','workspaces',sid,'batch-ui-19.txt')), {code:'ENOENT'})
  assert.equal((await readFile(path.join(temp,'data','workspaces',sid,'a.txt'))).toString('utf8'),'original a')
  assert.equal((await readFile(path.join(temp,'data','workspaces',sid,'fresh.txt'))).toString('utf8'),'uploaded new')
  let releaseRead, seenRead
  const readGate = new Promise(r => { releaseRead = r })
  const readStarted = new Promise(r => { seenRead = r })
  const delayRoute = async route => {
    const u = new URL(route.request().url())
    if (route.request().method() === 'GET' && u.pathname === '/api/workspace/file' && u.searchParams.get('path') === 'a.txt') {
      seenRead()
      await readGate
    }
    await route.continue()
  }
  await page.route('**/api/workspace/file?*', delayRoute)
  await page.getByRole('button',{name:'a.txt'}).click()
  await readStarted
  await page.getByRole('button',{name:'b.txt'}).click()
  await page.getByRole('textbox',{name:'Contents of b.txt'}).waitFor()
  const oldResponse = page.waitForResponse(r => r.request().method() === 'GET' && new URL(r.url()).searchParams.get('path') === 'a.txt')
  releaseRead()
  await oldResponse
  await page.unroute('**/api/workspace/file?*', delayRoute)
  assert.equal(await page.getByRole('textbox',{name:'Contents of b.txt'}).inputValue(),'original b')
  for (const [name, original] of samples) {
    await page.getByRole('button',{name}).click()
    const binary = page.getByRole('textbox',{name:`Contents of ${name}`})
    await binary.waitFor()
    assert.equal(await binary.getAttribute('readonly'), '')
    assert.equal(await binary.isEditable(), false)
    assert.equal(await page.getByRole('button',{name:'Read only'}).isDisabled(),true)
    assert.match(await binary.inputValue(), /binary or non-UTF-8/i)
    assert.deepEqual(await readFile(path.join(temp, 'data', 'workspaces', sid, name)), original)
  }
  // Binary previews carry a version too; stale deletion must not remove new bytes.
  await page.getByRole('button',{name:'image.bin'}).click()
  await page.getByRole('textbox',{name:'Contents of image.bin'}).waitFor()
  await writeFile(path.join(temp,'data','workspaces',sid,'image.bin'), Buffer.from([0,9,8,7]))
  page.once('dialog', async d => { await d.accept() })
  await page.getByRole('button',{name:'Delete file'}).click()
  await page.getByText(/it was not deleted/i).waitFor()
  assert.deepEqual(await readFile(path.join(temp,'data','workspaces',sid,'image.bin')), Buffer.from([0,9,8,7]))
  // Textarea normalizes line endings; saving must restore the original style.
  for (const [name, separator] of [['crlf.txt','\r\n'],['cr.txt','\r'],['lf.txt','\n']]) {
    await page.getByRole('button',{name:`📄 ${name}`,exact:true}).click()
    const textEditor = page.getByRole('textbox',{name:`Contents of ${name}`})
    await textEditor.waitFor()
    assert.equal(await textEditor.inputValue(), 'first\nsecond\n')
    await textEditor.fill('first\nmodified\n')
    await page.getByRole('button',{name:'● Save'}).click()
    await page.getByRole('button',{name:'Saved',exact:true}).waitFor()
    assert.equal((await readFile(path.join(temp,'data','workspaces',sid,name))).toString('utf8'), `first${separator}modified${separator}`)
  }
  await page.getByRole('button',{name:'mixed.txt'}).click()
  const mixed = page.getByRole('textbox',{name:'Contents of mixed.txt'})
  await mixed.waitFor()
  assert.equal(await mixed.isEditable(), false)
  assert.equal(await page.getByRole('button',{name:'Read only'}).isDisabled(),true)
  await page.getByText(/mixed line endings/i).waitFor()
  assert.equal((await readFile(path.join(temp,'data','workspaces',sid,'mixed.txt'))).toString('utf8'), 'first\r\nsecond\nthird\r')
  await page.getByRole('button',{name:'a.txt'}).click()
  let editor = page.getByRole('textbox',{name:'Contents of a.txt'})
  await editor.fill('unsaved edits')
  let dialogMessage = ''
  page.once('dialog', async d => { dialogMessage=d.message(); await d.dismiss() })
  await page.getByRole('button',{name:'b.txt'}).click()
  assert.match(dialogMessage,/unsaved/i)
  assert.equal(await editor.inputValue(),'unsaved edits')
  assert.equal(await page.getByRole('button',{name:'● Save'}).isEnabled(),true)
  const sessionsBefore = (await (await req.get(base+'/api/sessions')).json()).sessions.length
  page.once('dialog', async d => { assert.match(d.message(),/unsaved/i); await d.dismiss() })
  await page.getByRole('button',{name:'＋ New session'}).click()
  assert.equal((await (await req.get(base+'/api/sessions')).json()).sessions.length, sessionsBefore)
  assert.equal(await editor.inputValue(),'unsaved edits')
  page.once('dialog', async d => { assert.match(d.message(),/unsaved/i); await d.dismiss() })
  await page.getByRole('button',{name:'New file',exact:true}).click()
  assert.equal(await editor.inputValue(),'unsaved edits')
  page.once('dialog', async d => { dialogMessage=d.message(); await d.accept() })
  await page.getByRole('button',{name:'b.txt'}).click()
  editor = page.getByRole('textbox',{name:'Contents of b.txt'})
  await editor.waitFor()
  assert.equal(await editor.inputValue(),'original b')
  await editor.fill('saved from browser')
  await page.getByRole('button',{name:'● Save'}).click()
  await page.getByRole('button',{name:'Saved',exact:true}).waitFor()
  response = await req.get(base+`/api/workspace/file?sid=${sid}&path=b.txt`)
  assert.equal((await response.json()).content,'saved from browser')
  // An external writer changes the file after it was opened. Keep the draft on 409.
  await editor.fill('my unsaved conflict draft')
  await writeFile(path.join(temp,'data','workspaces',sid,'b.txt'), 'external modification')
  await page.getByRole('button',{name:'● Save'}).click()
  await page.getByText(/copy them before reopening/i).waitFor()
  assert.equal(await editor.inputValue(), 'my unsaved conflict draft')
  assert.equal((await readFile(path.join(temp,'data','workspaces',sid,'b.txt'))).toString(), 'external modification')
  page.once('dialog', async d => { assert.match(d.message(),/unsaved/i); await d.accept() })
  await page.getByRole('button',{name:'a.txt'}).click()
  await page.getByRole('textbox',{name:'Contents of a.txt'}).waitFor()
  await page.getByRole('button',{name:'b.txt'}).click()
  editor = page.getByRole('textbox',{name:'Contents of b.txt'})
  await editor.waitFor()
  assert.equal(await editor.inputValue(), 'external modification')
  await editor.fill('after resolving conflict')
  await page.getByRole('button',{name:'● Save'}).click()
  await page.getByRole('button',{name:'Saved',exact:true}).waitFor()
  assert.equal((await readFile(path.join(temp,'data','workspaces',sid,'b.txt'))).toString(), 'after resolving conflict')
  const largeDraft = 'é'.repeat(2 * 1024 * 1024 + 1)
  await editor.fill(largeDraft)
  await page.getByRole('button',{name:'● Save'}).click()
  await page.getByText(/content exceeds 4 MiB encoded as UTF-8/).waitFor()
  assert.equal(await editor.inputValue(), largeDraft)
  assert.equal((await readFile(path.join(temp,'data','workspaces',sid,'b.txt'))).toString(), 'after resolving conflict')
  await editor.fill('short draft')
  await page.getByRole('button',{name:'● Save'}).click()
  await page.getByRole('button',{name:'Saved',exact:true}).waitFor()
  assert.equal((await readFile(path.join(temp,'data','workspaces',sid,'b.txt'))).toString(), 'short draft')
  await editor.fill('draft\0with NUL')
  assert.equal(await editor.inputValue(), 'draft\0with NUL')
  await page.getByRole('button',{name:'● Save'}).click()
  await page.getByText(/content contains a NUL character/).waitFor()
  assert.equal(await editor.inputValue(), 'draft\0with NUL')
  assert.equal((await readFile(path.join(temp,'data','workspaces',sid,'b.txt'))).toString(), 'short draft')
  await editor.fill('draft without NUL')
  await page.getByRole('button',{name:'● Save'}).click()
  await page.getByRole('button',{name:'Saved',exact:true}).waitFor()
  assert.equal((await readFile(path.join(temp,'data','workspaces',sid,'b.txt'))).toString(), 'draft without NUL')
  // Simulate an older backend that lacks the conditional PUT route.
  let conditionalAttempts = 0, legacyAttempts = 0
  const rejectOld = async route => { conditionalAttempts++; await route.fulfill({status:405, contentType:'application/json', body:JSON.stringify({detail:'Method Not Allowed'})}) }
  const watchLegacy = async route => { if (route.request().method() === 'PUT') legacyAttempts++; await route.continue() }
  await page.route('**/api/workspace/file/conditional?*', rejectOld)
  await page.route('**/api/workspace/file?*', watchLegacy)
  await editor.fill('draft not sent to legacy save')
  await page.getByRole('button',{name:'● Save'}).click()
  await page.getByText(/could not confirm the save/i).waitFor()
  assert.equal(conditionalAttempts, 1)
  assert.equal(legacyAttempts, 0)
  assert.equal(await editor.inputValue(), 'draft not sent to legacy save')
  assert.equal((await readFile(path.join(temp,'data','workspaces',sid,'b.txt'))).toString(), 'draft without NUL')
  await page.unroute('**/api/workspace/file/conditional?*', rejectOld)
  await page.unroute('**/api/workspace/file?*', watchLegacy)
  await page.getByRole('button',{name:'● Save'}).click()
  await page.getByRole('button',{name:'Saved',exact:true}).waitFor()
  assert.equal((await readFile(path.join(temp,'data','workspaces',sid,'b.txt'))).toString(), 'draft not sent to legacy save')
  // Simulate a lost success response after the server has already written.
  const loseResponse = async route => { const actual = await route.fetch(); assert.equal(actual.status(),200); await route.fulfill({status:503, contentType:'application/json', body:JSON.stringify({detail:'response lost'})}) }
  await page.route('**/api/workspace/file/conditional?*', loseResponse)
  await editor.fill('server wrote but reply was lost')
  await page.getByRole('button',{name:'● Save'}).click()
  await page.getByText(/the file may already have been written/i).waitFor()
  assert.equal(await editor.inputValue(), 'server wrote but reply was lost')
  assert.equal((await readFile(path.join(temp,'data','workspaces',sid,'b.txt'))).toString(), 'server wrote but reply was lost')
  await page.unroute('**/api/workspace/file/conditional?*', loseResponse)
  await page.getByRole('button',{name:'● Save'}).click()
  await page.getByText(/copy them before reopening/i).waitFor()
  assert.equal(await editor.inputValue(), 'server wrote but reply was lost')
  page.once('dialog', async d => { assert.match(d.message(),/unsaved/i); await d.accept() })
  await page.getByRole('button',{name:'b.txt'}).click()
  await page.getByRole('button',{name:'Saved',exact:true}).waitFor()
  assert.equal(await editor.inputValue(), 'server wrote but reply was lost')
  await page.getByRole('button',{name:'delete-target.txt'}).click()
  const deleteEditor = page.getByRole('textbox',{name:'Contents of delete-target.txt'})
  await deleteEditor.waitFor()
  await deleteEditor.fill('unsaved deletion draft')
  await writeFile(path.join(temp,'data','workspaces',sid,'delete-target.txt'), 'externally modified')
  page.once('dialog', async d => { assert.match(d.message(),/unsaved/i); await d.accept() })
  await page.getByRole('button',{name:'Delete file'}).click()
  await page.getByText(/it was not deleted/i).waitFor()
  assert.equal(await deleteEditor.inputValue(), 'unsaved deletion draft')
  assert.equal((await readFile(path.join(temp,'data','workspaces',sid,'delete-target.txt'))).toString(), 'externally modified')
  page.once('dialog', async d => { assert.match(d.message(),/unsaved/i); await d.accept() })
  await page.getByRole('button',{name:'delete-target.txt'}).click()
  await page.waitForFunction(() => document.querySelector('[aria-label="Contents of delete-target.txt"]')?.value === 'externally modified')
  page.once('dialog', async d => { await d.accept() })
  await page.getByRole('button',{name:'Delete file'}).click()
  await page.getByRole('textbox',{name:'Contents of delete-target.txt'}).waitFor({state:'detached'})
  await assert.rejects(readFile(path.join(temp,'data','workspaces',sid,'delete-target.txt')), {code:'ENOENT'})
  await page.getByRole('button',{name:'delayed-delete.txt'}).click()
  const delayedEditor = page.getByRole('textbox',{name:'Contents of delayed-delete.txt'})
  await delayedEditor.waitFor()
  let releaseDelete, seenDelete
  const deleteGate = new Promise(r => { releaseDelete = r })
  const deleteStarted = new Promise(r => { seenDelete = r })
  const deleteRoute = async route => {
    if (route.request().method() === 'DELETE') { seenDelete(); await deleteGate }
    await route.continue()
  }
  await page.route('**/api/workspace/file/conditional?*', deleteRoute)
  page.once('dialog', async d => { await d.accept() })
  await page.getByRole('button',{name:'Delete file'}).click()
  await deleteStarted
  await delayedEditor.fill('draft created while delete waits')
  assert.equal(await page.getByRole('button',{name:'● Save'}).isDisabled(), true)
  assert.equal(await page.getByRole('button',{name:'Delete file'}).isDisabled(), true)
  releaseDelete()
  await page.getByText(/file was deleted while the editor changed/i).waitFor()
  assert.equal(await delayedEditor.inputValue(), 'draft created while delete waits')
  assert.equal(await delayedEditor.isEditable(), false)
  await assert.rejects(readFile(path.join(temp,'data','workspaces',sid,'delayed-delete.txt')), {code:'ENOENT'})
  await page.unroute('**/api/workspace/file/conditional?*', deleteRoute)
  page.once('dialog', async d => { assert.match(d.message(),/unsaved/i); await d.accept() })
  await page.getByRole('button',{name:'b.txt'}).click()
  editor = page.getByRole('textbox',{name:'Contents of b.txt'})
  await editor.waitFor()
  page.once('dialog', async d => { assert.equal(d.type(),'prompt'); await d.accept('a.txt') })
  await page.getByRole('button',{name:'New file',exact:true}).click()
  await page.getByText(/File already exists; it was not overwritten/).waitFor()
  response = await req.get(base+`/api/workspace/file?sid=${sid}&path=a.txt`)
  assert.equal((await response.json()).content,'original a')
  await editor.fill('will survive rejected reload')
  const unload = page.waitForEvent('dialog')
  const reload = page.reload({timeout:3000}).catch(() => null)
  const d = await unload
  assert.equal(d.type(),'beforeunload')
  await d.dismiss()
  await reload
  assert.equal(await editor.inputValue(),'will survive rejected reload')
  assert.deepEqual(errors,[])
  console.log(`real Chromium ${browser.version()}: authenticated editor, binary, dirty prompts, save, create conflict and beforeunload passed`)
  // Avoid another beforeunload during cleanup.
  await page.close({runBeforeUnload:false})
  await context.close()
} finally {
  if (browser) await browser.close()
  if (server.exitCode === null && server.signalCode === null) {
    server.kill('SIGTERM')
    let timer
    await Promise.race([new Promise(r => server.once('exit',r)),new Promise(r => { timer = setTimeout(r,10000) })])
    clearTimeout(timer)
    if (server.exitCode === null && server.signalCode === null) {
      server.kill('SIGKILL')
      await new Promise(r => server.once('exit',r))
    }
  }
  await rm(temp,{recursive:true,force:true})
}
