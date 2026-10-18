import type { AgentDeployment } from '@/composables/useAgentDeployments'

/** Toasts show one short line; the complete returned failure remains in the page. */
export function deploymentErrorSummary(task: Partial<AgentDeployment>) {
  const text = task.error || task.rollback_error || task.message || ''
  const line = text.trim().split(/\r?\n/, 1)[0] || ''
  return line.length > 240 ? `${line.slice(0, 240)}…` : line
}
