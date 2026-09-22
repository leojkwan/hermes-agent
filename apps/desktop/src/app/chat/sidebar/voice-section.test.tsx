import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'
import { SidebarVoiceSection } from './voice-section'

afterEach(cleanup)

const voiceSession = {
  id: 'voice-1',
  ended_at: null,
  source: 'voice-safari',
  title: 'Night voice session',
} as unknown as SessionInfo

const typedSession = {
  id: 'typed-1',
  ended_at: null,
  source: 'desktop',
  title: 'Typed chat',
} as unknown as SessionInfo

function renderSection(overrides?: Partial<Parameters<typeof SidebarVoiceSection>[0]>) {
  const props = {
    label: 'Voice',
    onOpenSession: vi.fn(),
    onToggle: vi.fn(),
    open: true,
    sessions: [voiceSession, typedSession],
    ...overrides,
  }
  render(<SidebarVoiceSection {...props} />)
  return props
}

describe('SidebarVoiceSection', () => {
  it('groups voice-tagged sessions and drops everything else', () => {
    renderSection()
    expect(screen.getByText('Night voice session')).toBeTruthy()
    expect(screen.queryByText('Typed chat')).toBeNull()
  })

  it('opens the clicked voice session by id', () => {
    const props = renderSection()
    fireEvent.click(screen.getByText('Night voice session'))
    expect(props.onOpenSession).toHaveBeenCalledWith('voice-1')
  })

  it('toggles from the section header', () => {
    const props = renderSection()
    fireEvent.click(screen.getByText('Voice'))
    expect(props.onToggle).toHaveBeenCalledOnce()
  })

  it('renders no rows when nothing is voice-tagged', () => {
    renderSection({ sessions: [typedSession] })
    expect(screen.queryByText('Night voice session')).toBeNull()
    expect(screen.queryByText('Typed chat')).toBeNull()
  })
})
