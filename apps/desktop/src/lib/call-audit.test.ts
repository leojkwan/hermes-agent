import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { CallAuditRequest, CallAuditResult, CallAuditSide } from '../../electron/call-audit'

import { CallAudit } from './call-audit'

// ~va01: a desktop call is complete only on the relay's verdict from exact
// per-side stopped counts. The stand-in relay below applies the real relay's
// finish rule (both sides non-empty, contiguous from 0, count == uploaded).

const CALL_ID = 'b'.repeat(32)

class FakeRecorder extends EventTarget {
  static isTypeSupported = (mime: string) => mime === 'audio/webm;codecs=opus'
  static made: FakeRecorder[] = []
  readonly stream: MediaStream
  readonly mimeType: string
  state: 'inactive' | 'recording' = 'inactive'
  timeslice: number | undefined

  constructor(stream: MediaStream, options: MediaRecorderOptions) {
    super()
    this.stream = stream
    this.mimeType = options.mimeType ?? ''
    FakeRecorder.made.push(this)
  }

  start(timeslice?: number) {
    this.state = 'recording'
    this.timeslice = timeslice
  }

  emit(size: number) {
    const event = Object.assign(new Event('dataavailable'), {
      data: new Blob([new Uint8Array(size)], { type: this.mimeType })
    })

    this.dispatchEvent(event)
  }

  /** Like a browser: the final slice arrives, then `stop`. */
  stop() {
    this.emit(4)
    this.state = 'inactive'
    this.dispatchEvent(new Event('stop'))
  }
}

function relay(override?: (request: CallAuditRequest) => CallAuditResult | Promise<CallAuditResult> | undefined) {
  const chunks: Record<CallAuditSide, number[]> = { assistant: [], mic: [] }
  const events: unknown[] = []
  const finishes: unknown[] = []

  const bridge = vi.fn(async (request: CallAuditRequest): Promise<CallAuditResult> => {
    const answer = override?.(request)

    if (answer) {
      return answer
    }

    if (request.action === 'start') {
      return { callId: CALL_ID, ok: true, state: 'open', status: 201 }
    }

    if (request.action === 'event') {
      events.push(request.json)

      return { ok: true, status: 201 }
    }

    if (request.action === 'chunk') {
      chunks[request.side!].push(request.seq!)

      return { ok: true, status: 201 }
    }

    finishes.push(request.json)
    const stopped = (request.json as { stopped?: Record<CallAuditSide, number> }).stopped

    const complete =
      !!stopped &&
      (['mic', 'assistant'] as const).every(
        side => chunks[side].length > 0 && stopped[side] === chunks[side].length && chunks[side].every((seq, i) => seq === i)
      )

    return complete
      ? { callId: CALL_ID, ok: true, state: 'complete', status: 200 }
      : { ok: false, state: 'incomplete', status: 409 }
  })

  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { callAudit: bridge } })

  return { bridge, chunks, events, finishes }
}

async function recording(failures: string[] = []) {
  const audit = new CallAudit(reason => failures.push(reason))
  await audit.start()
  const mic = { id: 'mic' } as unknown as MediaStream
  const assistant = { id: 'assistant' } as unknown as MediaStream
  audit.record('mic', mic)
  audit.record('assistant', assistant)
  const [micRecorder, assistantRecorder] = FakeRecorder.made

  return { assistant, assistantRecorder, audit, mic, micRecorder }
}

beforeEach(() => {
  FakeRecorder.made = []
  Object.defineProperty(globalThis, 'MediaRecorder', { configurable: true, value: FakeRecorder })
})

afterEach(() => {
  Reflect.deleteProperty(window, 'hermesDesktop')
  Reflect.deleteProperty(globalThis, 'MediaRecorder')
})

describe('desktop call recording', () => {
  it('uploads contiguous 1 s chunks per side and finishes with the exact stopped counts', async () => {
    const stand = relay()
    const failures: string[] = []
    const { assistant, assistantRecorder, audit, mic, micRecorder } = await recording(failures)

    expect([micRecorder.stream, assistantRecorder.stream]).toEqual([mic, assistant])
    expect([micRecorder.timeslice, assistantRecorder.timeslice]).toEqual([1000, 1000])

    micRecorder.emit(10)
    micRecorder.emit(0) // an empty slice is not a chunk and takes no sequence number
    micRecorder.emit(10)
    assistantRecorder.emit(10)

    const outcome = await audit.finish()

    expect(stand.events[0]).toMatchObject({ text: 'desktop', type: 'client.source' })
    expect(stand.chunks).toEqual({ assistant: [0, 1], mic: [0, 1, 2] })
    expect(stand.finishes).toEqual([{ stopped: { assistant: 2, mic: 3 } }])
    expect(outcome).toEqual({ callId: CALL_ID, complete: true, reason: null })
    expect(failures).toEqual([])
    // Re-entrant close reuses the one finish.
    await expect(audit.finish()).resolves.toBe(outcome)
    expect(stand.finishes).toHaveLength(1)
  })

  it('a refused chunk ends the record as incomplete, never complete', async () => {
    const stand = relay(request =>
      request.action === 'chunk' && request.seq === 1
        ? { failure: 'superseded', ok: false, state: 'incomplete', status: 409 }
        : undefined
    )

    const failures: string[] = []
    const { assistantRecorder, audit, micRecorder } = await recording(failures)
    micRecorder.emit(10)
    micRecorder.emit(10)
    micRecorder.emit(10)
    assistantRecorder.emit(10)
    await vi.waitFor(() => expect(failures).toEqual(['a newer call started']))

    const outcome = await audit.finish()

    expect(stand.chunks.mic).toEqual([0])
    expect(stand.bridge.mock.calls.filter(([request]) => request.action === 'chunk')).toHaveLength(2)
    expect(stand.finishes).toEqual([{}])
    expect(outcome).toEqual({ callId: CALL_ID, complete: false, reason: 'a newer call started' })
  })

  it('a finish refusal stays refused; only a transient one is retried', async () => {
    const refused = relay(request => (request.action === 'finish' ? { ok: false, status: 401 } : undefined))
    const first = await recording()

    expect(await first.audit.finish()).toMatchObject({ complete: false, reason: 'call recording refused (401)' })
    expect(refused.bridge.mock.calls.filter(([request]) => request.action === 'finish')).toHaveLength(1)

    FakeRecorder.made = []
    const busy = [{ ok: false, status: 503 }]
    const flaky = relay(request => (request.action === 'finish' ? busy.shift() : undefined))
    const second = await recording()

    expect(await second.audit.finish()).toEqual({ callId: CALL_ID, complete: true, reason: null })
    expect(flaky.bridge.mock.calls.filter(([request]) => request.action === 'finish')).toHaveLength(2)
  })

  it('a chunk stored before its answer was lost counts once, not as a refusal', async () => {
    let lost = true

    const stand = relay(request => {
      if (request.action !== 'chunk' || request.side !== 'mic' || request.seq !== 0) {
        return undefined
      }

      if (lost) {
        lost = false
        stand.chunks.mic.push(0) // stored, but main timed out waiting for the answer

        return { error: 'relay unreachable (timed out)', ok: false, status: 0 }
      }

      return { error: 'duplicate or excessive sequence', ok: false, status: 409 }
    })

    const failures: string[] = []
    const { assistantRecorder, audit, micRecorder } = await recording(failures)
    micRecorder.emit(10)
    assistantRecorder.emit(10)

    expect(await audit.finish()).toEqual({ callId: CALL_ID, complete: true, reason: null })
    expect(stand.chunks.mic).toEqual([0, 1])
    expect(failures).toEqual([])
  })

  it('a side that was never recorded cannot finish complete', async () => {
    const stand = relay()
    const audit = new CallAudit(() => undefined)
    await audit.start()
    audit.record('mic', {} as MediaStream)
    FakeRecorder.made[0].emit(10)

    expect(await audit.finish()).toEqual({
      callId: CALL_ID,
      complete: false,
      reason: 'assistant audio was never recorded'
    })
    expect(stand.finishes).toEqual([{}])
  })

  it('stops buffering audio when uploads fall behind', async () => {
    const stand = relay(request => (request.action === 'chunk' ? new Promise(() => undefined) : undefined))
    const failures: string[] = []
    const { micRecorder } = await recording(failures)
    const uploads = () => stand.bridge.mock.calls.filter(([request]) => request.action === 'chunk')
    micRecorder.emit(10)
    await vi.waitFor(() => expect(uploads()).toHaveLength(1))

    for (let slice = 1; slice < 130; slice += 1) {
      micRecorder.emit(10)
    }

    expect(failures).toEqual(['recording upload fell behind'])
    expect(uploads()).toHaveLength(1)
  })

  it('does not open a relay call when this renderer cannot record', async () => {
    const stand = relay()
    Reflect.deleteProperty(globalThis, 'MediaRecorder')

    await expect(new CallAudit(() => undefined).start()).rejects.toThrow(/unavailable/)
    expect(stand.bridge).not.toHaveBeenCalled()
  })
})
