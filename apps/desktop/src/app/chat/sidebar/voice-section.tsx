import { useMemo } from 'react'

import { DisclosureCaret } from '@/components/ui/disclosure-caret'
import { SidebarGroup, SidebarGroupContent } from '@/components/ui/sidebar'
import { type SessionInfo } from '@/hermes'

import { SidebarPanelLabel } from '../../shell/sidebar-label'
import { SidebarRowBody, SidebarRowLabel, SidebarRowShell } from './chrome'

/**
 * Voice sessions are the ones their creators tagged with a `voice-*` source —
 * the standalone voice page stamps `voice-safari` (title "Voice") on every
 * delegated session it opens. Desktop voice rides the active chat session, so
 * it intentionally produces no rows here.
 */
export function SidebarVoiceSection({
  label,
  onOpenSession,
  onToggle,
  open,
  sessions,
}: {
  label: string
  onOpenSession: (sessionId: string) => void
  onToggle: () => void
  open: boolean
  sessions: SessionInfo[]
}) {
  const voiceSessions = useMemo(
    () => sessions.filter((session) => (session.source ?? '').startsWith('voice')),
    [sessions]
  )

  return (
    <SidebarGroup className="shrink-0 p-0 pb-1">
      <div className="group/section flex shrink-0 items-center justify-between pb-1 pt-1.5">
        <button
          className="group/section-label flex w-fit min-w-0 items-center gap-1 bg-transparent text-left leading-none"
          onClick={onToggle}
          type="button"
        >
          <SidebarPanelLabel>{label}</SidebarPanelLabel>
          <DisclosureCaret
            className="text-(--ui-text-tertiary) opacity-0 transition group-hover/section-label:opacity-100"
            open={open}
          />
        </button>
      </div>
      <SidebarGroupContent className="compact:max-h-none compact:overflow-visible max-h-56 overflow-y-auto overscroll-contain scrollbar-fade">
        {voiceSessions.map((session) => (
          <SidebarRowShell key={session.id} onClick={() => onOpenSession(session.id)}>
            <SidebarRowBody>
              <SidebarRowLabel>{session.title || 'Voice session'}</SidebarRowLabel>
            </SidebarRowBody>
          </SidebarRowShell>
        ))}
      </SidebarGroupContent>
    </SidebarGroup>
  )
}
