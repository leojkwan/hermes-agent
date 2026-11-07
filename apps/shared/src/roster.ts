/** Presentation membership only: equivalent routes retain separate authority. */
export interface RosterEquivalentConnection {
  connectionId: string
  connectionKind: 'cloud' | 'local' | 'remote' | 'ssh'
  connectionLabel: string
  targetProfile: string
}

export interface RosterProfileMetadata {
  display_name?: string
  description?: string
  title?: string
  ui_meta?: Record<string, unknown>
  has_avatar?: boolean
}
