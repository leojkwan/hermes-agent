import { afterEach, describe, expect, it, vi } from 'vitest'

import { setApiRequestConnection, setApiRequestProfile } from '@/hermes'

import { type VoiceLiveHandlers, VoiceLiveSession } from './voice-live'

// A GPT-Live session is created on the chat OWNER's (connection, profile) —
// the Bot that owns the chat — never on the window's active scope, mirroring
// the TTS legs (#117014): two Bots with different voices configured must get
// their own voices. The status probe follows the same rule so a Bot tile
// mounts the engine the Bot's own profile selected.

const handlers: VoiceLiveHandlers = {
  onClosed: () => undefined,
  onDelegation: () => undefined,
  onError: () => undefined
}

type CallAuditFake = (request: { action: string }) => Promise<unknown>

const acceptingCallAudit: CallAuditFake = async request =>
  request.action === 'start'
    ? { callId: 'c'.repeat(32), ok: true, state: 'open', status: 201 }
    : { ok: true, status: 201 }

const sessionAnswer = { ok: true, session: { id: 's1' }, transport: { sdp: 'answer', type: 'answer' } }

function installApi(callAudit: CallAuditFake = acceptingCallAudit) {
  const api = vi.fn(async (_request: unknown) => sessionAnswer)

  const audit = vi.fn(callAudit)

  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { api, callAudit: audit } })

  return { api, audit }
}

let recorders: Array<{ state: string; stream: unknown; timeslice?: number }> = []

/** Enough of RTCPeerConnection/getUserMedia/MediaRecorder for `start()` to reach the POST. */
function installWebRTC(microphone: unknown = { getAudioTracks: () => [] }) {
  const channel = { addEventListener: () => undefined, readyState: 'open' as const, send: () => undefined }

  const PeerConnection = class {
    addEventListener = () => undefined
    addTrack = () => undefined
    connectionState = 'new'
    createDataChannel = () => channel
    createOffer = async () => ({})
    iceGatheringState = 'complete'
    localDescription = { sdp: 'v=0\r\n' }
    setLocalDescription = async () => undefined
    setRemoteDescription = async () => undefined
  }

  const Recorder = class extends EventTarget {
    static isTypeSupported = (mime: string) => mime === 'audio/webm;codecs=opus'
    mimeType = 'audio/webm;codecs=opus'
    state = 'inactive'
    timeslice: number | undefined
    constructor(readonly stream: unknown) {
      super()
      recorders.push(this)
    }
    start = (timeslice?: number) => {
      this.state = 'recording'
      this.timeslice = timeslice
    }
  }

  recorders = []

  Object.defineProperty(globalThis, 'RTCPeerConnection', { configurable: true, value: PeerConnection })
  Object.defineProperty(globalThis, 'MediaRecorder', { configurable: true, value: Recorder })
  Object.defineProperty(window.navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia: async () => microphone }
  })
}

afterEach(() => {
  setApiRequestConnection(null)
  setApiRequestProfile(null)
  Reflect.deleteProperty(window, 'hermesDesktop')
  Reflect.deleteProperty(globalThis, 'RTCPeerConnection')
  Reflect.deleteProperty(globalThis, 'MediaRecorder')
  Reflect.deleteProperty(window.navigator, 'mediaDevices')
})

describe('GPT-Live owner routing', () => {
  it('creates the session on the chat owner (connection, profile) ahead of the active scope', async () => {
    installWebRTC()
    const { api, audit } = installApi()
    setApiRequestConnection('gw-active')
    setApiRequestProfile('research')

    const session = new VoiceLiveSession(handlers, { connectionId: 'gw-bots', profile: 'bot-adam' })
    await session.start([])

    expect(api).toHaveBeenCalledTimes(1)
    const request = api.mock.calls[0][0] as { connectionId?: string; path: string; priority?: string; profile?: string }

    expect(request.path).toBe('/api/audio/voice-live/session')
    expect(request.profile).toBe('bot-adam')
    expect(request.connectionId).toBe('gw-bots')
    expect(request.priority).toBe('foreground')
    // Recording authenticates with the relay's own key in main, so no backend scope goes with it.
    expect(audit.mock.calls[0][0]).toEqual({ action: 'start' })

    // Ownerless chats keep the active scope verbatim (the old bare profileScoped()).
    const ownerless = new VoiceLiveSession(handlers, null)
    await ownerless.start([])
    const plain = api.mock.calls[1][0] as { connectionId?: string; path: string; priority?: string; profile?: string }
    expect(plain.path).toBe('/api/audio/voice-live/session')
    expect(plain.profile).toBe('research')
    expect(plain.connectionId).toBe('gw-active')
    expect(plain.priority).toBeUndefined()
  })
})

describe('GPT-Live call recording gate', () => {
  it('a refused recording start never creates the voice session', async () => {
    installWebRTC()
    const { api } = installApi(async () => ({ error: 'sign in to Hermes', ok: false, status: 401 }))

    await expect(new VoiceLiveSession(handlers, null).start([])).rejects.toThrow(/401/)
    expect(api).not.toHaveBeenCalled()
  })

  it('is already recording the microphone stream when the voice session is created', async () => {
    const microphone = { getAudioTracks: () => [] }
    installWebRTC(microphone)
    const { api } = installApi()
    let armed: unknown[] = []

    api.mockImplementationOnce(async () => {
      armed = recorders.map(recorder => [recorder.stream, recorder.state, recorder.timeslice])

      return sessionAnswer
    })

    await new VoiceLiveSession(handlers, null).start([])

    expect(armed).toEqual([[microphone, 'recording', 1000]])
  })
})
