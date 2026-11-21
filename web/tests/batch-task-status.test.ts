import assert from 'node:assert/strict'
import { batchTaskStatus } from '../src/batchTaskStatus'

assert.equal(batchTaskStatus('running', {type:'error', message:'denied', code:'hook_trust_denied'}), 'error')
assert.equal(batchTaskStatus('error', {type:'task_done', index:0, session_id:'s', ok:false}), 'error')
assert.equal(batchTaskStatus('error', {type:'task_done', index:0, session_id:'s'}), 'error')
assert.equal(batchTaskStatus('running', {type:'task_done', index:0, session_id:'s', ok:false}), 'error')
assert.equal(batchTaskStatus('running', {type:'task_done', index:0, session_id:'s', ok:true}), 'done')
assert.equal(batchTaskStatus('running', {type:'assistant_delta', text:'working'}), 'running')
console.log('batch task errors are not overwritten by terminal events: passed')
