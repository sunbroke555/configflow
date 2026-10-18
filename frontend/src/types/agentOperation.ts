/** Only server/Agent-observed transitions; the UI never fills in skipped stages. */
export interface AgentOperationEvent {
  at: string
  status: string
  message?: string
}
