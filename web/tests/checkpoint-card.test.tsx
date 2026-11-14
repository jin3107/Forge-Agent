import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import assert from 'node:assert/strict'
import { ToolCard } from '../src/components/ToolCard'
import { LanguageProvider } from '../src/i18n'
import { applyEvent } from '../src/App'
import type { Item } from '../src/types'

const item = (overrides: Record<string, unknown>): Extract<Item, { kind: 'tool' }> => ({
  kind: 'tool', id: 'test', callId: 'call', name: 'run_command', args: {command:'fixture'},
  output: 'ok', ok: true, ...overrides,
} as Extract<Item, { kind: 'tool' }>)
const render = (it: Extract<Item, { kind: 'tool' }>) => renderToStaticMarkup(
  <LanguageProvider><ToolCard item={it} /></LanguageProvider>,
)

const failed = render(item({ checkpointStatus: 'failed', output: 'x'.repeat(9000) + '\nWarning: checkpoint failed; Undo may not restore' }))
assert.match(failed, /checkpoint-warning/)
assert.match(failed, /checkpoint/i)
assert.doesNotMatch(failed, /tool-body/)
const created = render(item({ checkpointStatus: 'created' }))
assert.doesNotMatch(created, /checkpoint-warning/)
// Persisted sessions currently reconstruct output but not status. A long output
// must retain its end when the card is opened (diff makes this SSR card open).
const persisted = render(item({ diff: '+fixture', output: 'x'.repeat(9000) + '\nWarning: checkpoint failed; Undo may not restore' }))
assert.match(persisted, /Undo may not restore/)
assert.match(persisted, /truncat|cắt bớt/i)
const started = applyEvent([], {type: 'tool_start', id: 'call', name: 'run_command', args: {}})
const completed = applyEvent(started, {type: 'tool_result', id: 'call', name: 'run_command', args: {}, ok: true, output: 'result', checkpoint_status: 'failed'})
assert.equal(completed[0].kind, 'tool')
if (completed[0].kind !== 'tool') throw new Error('missing tool card')
assert.equal(completed[0].checkpointStatus, 'failed')
assert.match(render(completed[0]), /checkpoint-warning/)
console.log('checkpoint card SSR and reducer: 4 checks passed')
