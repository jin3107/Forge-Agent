import type { AgentEvent, TaskDoneEvent, TaskGroup } from './types'

/** Terminal events must never overwrite an earlier task error. */
export function batchTaskStatus(status: TaskGroup['status'], event: AgentEvent | TaskDoneEvent): TaskGroup['status'] {
  if (event.type === 'error') return 'error'
  if (event.type === 'task_done') return status === 'error' || event.ok === false ? 'error' : 'done'
  return status
}
