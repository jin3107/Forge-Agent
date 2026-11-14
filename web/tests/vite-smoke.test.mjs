// Compatibility smoke test for the installed Vite: dev transforms, proxy and preview.
import assert from 'node:assert/strict'
import http from 'node:http'
import { createServer, preview } from 'vite'

const backend = http.createServer((req, res) => {
  if (req.url === '/api/fixture') {
    res.writeHead(200, { 'content-type': 'application/json' })
    res.end(JSON.stringify({ ok: true, source: 'fixture-backend' }))
  } else {
    res.writeHead(404).end()
  }
})
let dev, built
try {
  await new Promise((resolve, reject) => {
    backend.once('error', reject)
    backend.listen(8000, '127.0.0.1', resolve)
  })
  dev = await createServer({ server: { host: '127.0.0.1', port: 0, strictPort: false }, logLevel: 'error' })
  await dev.listen()
  const devBase = `http://127.0.0.1:${dev.httpServer.address().port}`
  let response = await fetch(devBase + '/')
  assert.equal(response.status, 200)
  assert.match(await response.text(), /src\/main\.tsx/)
  response = await fetch(devBase + '/src/main.tsx')
  assert.equal(response.status, 200)
  assert.match(await response.text(), /react/)
  response = await fetch(devBase + '/api/fixture')
  assert.equal(response.status, 200)
  assert.deepEqual(await response.json(), { ok: true, source: 'fixture-backend' })

  // Requires npm run build beforehand: exercise the actual generated assets.
  built = await preview({ preview: { host: '127.0.0.1', port: 0, strictPort: false }, logLevel: 'error' })
  const previewBase = `http://127.0.0.1:${built.httpServer.address().port}`
  response = await fetch(previewBase + '/')
  assert.equal(response.status, 200)
  const html = await response.text()
  const asset = html.match(/src="(\.\/assets\/[^\"]+\.js)"/)
  assert.ok(asset, 'built index must reference a JS bundle')
  response = await fetch(new URL(asset[1], previewBase + '/'))
  assert.equal(response.status, 200)
  assert.ok((await response.text()).length > 1000)
  console.log('Vite dev transform, API proxy and production preview: passed')
} finally {
  if (dev) await dev.close()
  if (built) await new Promise((resolve, reject) => built.httpServer.close(err => err ? reject(err) : resolve()))
  if (backend.listening) await new Promise((resolve, reject) => backend.close(err => err ? reject(err) : resolve()))
}
