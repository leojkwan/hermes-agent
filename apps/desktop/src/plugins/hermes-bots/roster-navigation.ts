import { host } from '@hermes/plugin-sdk'

import type { CanonicalGroupOrigin } from './canonical-group-registry'
import { openGroupChat } from './group-chat-view'

/** Opening a room from the roster is an explicit navigation to the workspace. */
export function openRosterGroup(group: string, origin: CanonicalGroupOrigin): void {
  if (openGroupChat(group, origin) && typeof host.dismissNarrowOverlay === 'function') {
    host.dismissNarrowOverlay('hermes-bots:pane')
  }
}
