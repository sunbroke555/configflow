import type { ClientDevicesState, ClientDiscovery } from '@/api'
import { getAuthSessionGeneration, getAuthToken } from '@/authSession'

interface Snapshot {
  state: ClientDevicesState
  discovery: ClientDiscovery | null
  at: number
}

// Preview data belongs to this browser session. It never contains unsaved edits.
const snapshots = new Map<string, Snapshot>()
const MAX_AGE = 10 * 60_000
const MAX_ENTRIES = 16
let sessionGeneration = -1
const copy = <T>(value: T): T => JSON.parse(JSON.stringify(value))

function checkSession() {
  getAuthToken()
  const generation = getAuthSessionGeneration()
  if (generation !== sessionGeneration) {
    snapshots.clear()
    sessionGeneration = generation
  }
}

export function readClientDevicesSnapshot(key: string) {
  checkSession()
  const snapshot = snapshots.get(key)
  if (!snapshot) return null
  if (Date.now() - snapshot.at > MAX_AGE) {
    snapshots.delete(key)
    return null
  }
  snapshots.delete(key)
  snapshots.set(key, snapshot)
  return copy(snapshot)
}

export function rememberClientDevicesSnapshot(key: string, state: ClientDevicesState, discovery: ClientDiscovery | null) {
  checkSession()
  snapshots.delete(key)
  snapshots.set(key, copy({ state, discovery: discovery?.profile_id === state.profile_id ? discovery : null, at: Date.now() }))
  while (snapshots.size > MAX_ENTRIES) snapshots.delete(snapshots.keys().next().value!)
}

export function forgetClientDevicesSnapshot(key?: string) {
  if (key) snapshots.delete(key)
  else snapshots.clear()
}
