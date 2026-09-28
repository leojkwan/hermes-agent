import type { CallAuditRequest, CallAuditResult, CallAuditSide } from '../../electron/call-audit'

/**
 * Private recording of a desktop GPT-Live call (~va01) into the voice relay's
 * call store, under the same completeness contract as the voice page.
 *
 * The mic is recorded from the getUserMedia stream before it joins the offer,
 * the assistant from its remote track before playback. Audio goes to Electron
 * main through `window.hermesDesktop.callAudit`, which holds the relay key;
 * the renderer only keeps a bounded in-memory queue. A call is complete only
 * when the relay mints it from exact per-side stopped counts — any refusal,
 * lost chunk, missing side or unclean stop leaves it incomplete.
 */

const SIDES: CallAuditSide[] = ['mic', 'assistant']
const MIME_PREFERENCE = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/webm']
const SLICE_MS = 1_000
// About a minute of both sides. Past it the relay is not keeping up and the
// record cannot be complete, so stop holding audio in memory.
const MAX_PENDING_UPLOADS = 120
// Transient failures only; the relay's finish freshness window is a few seconds.
const RETRY_DELAYS_MS = [250, 1_000]
const STOP_TIMEOUT_MS = 1_500

export interface CallAuditOutcome {
  /** The relay call, when one was opened. */
  callId: null | string
  complete: boolean
  reason: null | string
}

function recorderMime(): string | undefined {
  return typeof MediaRecorder === 'undefined' || !MediaRecorder.isTypeSupported
    ? undefined
    : MIME_PREFERENCE.find(mime => MediaRecorder.isTypeSupported(mime))
}

function refusal(result: CallAuditResult): string {
  if (result.failure === 'superseded') {
    return 'a newer call started'
  }

  const detail = result.error || result.state

  return result.status
    ? `call recording refused (${result.status})${detail ? `: ${detail}` : ''}`
    : `call recording unavailable${detail ? `: ${detail}` : ''}`
}

const isTransient = (status: number) => status === 0 || status === 429 || status >= 500

export class CallAudit {
  private readonly onFailure: (reason: string) => void
  private callId: null | string = null
  private starting: null | Promise<void> = null
  private finishing: null | Promise<CallAuditOutcome> = null
  private closing = false
  private readonly recorders = new Map<CallAuditSide, MediaRecorder>()
  private readonly sequences: Record<CallAuditSide, number> = { assistant: 0, mic: 0 }
  private queue: Promise<void> = Promise.resolve()
  private pending = 0
  failure: null | string = null

  /** `onFailure` fires once when a live call's recording breaks; the call cannot be complete after it. */
  constructor(onFailure: (reason: string) => void) {
    this.onFailure = onFailure
  }

  /** Open the relay call and mark it desktop-originated. Rejects on any refusal. */
  start(): Promise<void> {
    this.starting ??= this.open()

    return this.starting
  }

  private async open(): Promise<void> {
    const bridge = window.hermesDesktop?.callAudit

    if (!bridge || !recorderMime()) {
      throw new Error('Call recording is unavailable, so the call did not start.')
    }

    const started = await bridge({ action: 'start' })

    if (!started.ok || !started.callId) {
      throw new Error(`Could not start: ${refusal(started)}`)
    }

    this.callId = started.callId

    // The relay records no top-level source; this event is client-declared provenance, not attestation.
    const source = await this.send({
      action: 'event',
      callId: started.callId,
      json: { client_at: Date.now(), text: 'desktop', type: 'client.source' }
    })

    if (!source.ok) {
      this.failure = refusal(source)

      throw new Error(`Could not start: ${this.failure}`)
    }
  }

  /** Record one side from now until finish. Throws if its recorder cannot start. */
  record(side: CallAuditSide, stream: MediaStream): void {
    const callId = this.callId

    if (!callId || this.closing || this.recorders.has(side)) {
      return
    }

    const recorder = new MediaRecorder(stream, { mimeType: recorderMime() })
    this.recorders.set(side, recorder)
    // Nothing but a counter and a queue append runs on the media callback.
    recorder.addEventListener('dataavailable', ({ data }) => {
      if (data?.size) {
        this.upload(callId, side, this.sequences[side]++, data, data.type || recorder.mimeType)
      }
    })
    recorder.addEventListener('error', () => this.fail(`${side} recorder failed`))
    recorder.start(SLICE_MS)
  }

  private upload(callId: string, side: CallAuditSide, seq: number, data: Blob, mime: string): void {
    if (this.failure) {
      return
    }

    if (this.pending >= MAX_PENDING_UPLOADS) {
      this.fail('recording upload fell behind')

      return
    }

    this.pending += 1
    this.queue = this.queue.then(async () => {
      try {
        if (!this.failure) {
          const audio = new Uint8Array(await data.arrayBuffer())
          const result = await this.send({ action: 'chunk', audio, callId, mime, seq, side })

          if (!result.ok) {
            this.fail(refusal(result))
          }
        }
      } finally {
        this.pending -= 1
      }
    })
  }

  private async send(request: CallAuditRequest): Promise<CallAuditResult> {
    for (let attempt = 0; ; attempt += 1) {
      const result = await window.hermesDesktop.callAudit(request).catch(
        (error: unknown): CallAuditResult => ({ error: String(error), ok: false, status: 0 })
      )

      // The relay can store a chunk whose answer never arrived; the retry then
      // meets its own copy. That chunk landed — the finish verdict still decides completeness.
      if (attempt > 0 && request.action === 'chunk' && result.status === 409 && result.error?.includes('duplicate')) {
        return { ...result, ok: true }
      }

      if (result.ok || !isTransient(result.status) || attempt >= RETRY_DELAYS_MS.length) {
        return result
      }

      await new Promise(resolve => window.setTimeout(resolve, RETRY_DELAYS_MS[attempt]))
    }
  }

  private fail(reason: string): void {
    if (this.failure) {
      return
    }

    this.failure = reason

    if (!this.closing) {
      this.onFailure(reason)
    }
  }

  /** Stop both recorders now, flush, and ask the relay to close the call.
   *  Idempotent; the outcome is complete only on the relay's own verdict. */
  finish(): Promise<CallAuditOutcome> {
    if (!this.finishing) {
      this.closing = true
      // Stop synchronously, before the caller ends the tracks, so each recorder flushes its last slice.
      const stopped = [...this.recorders].map(([side, recorder]) => this.stop(side, recorder))
      this.finishing = this.settle(stopped)
    }

    return this.finishing
  }

  private stop(side: CallAuditSide, recorder: MediaRecorder): Promise<void> {
    if (recorder.state === 'inactive') {
      this.fail(`${side} recorder stopped unexpectedly`)

      return Promise.resolve()
    }

    return new Promise(resolve => {
      const timer = window.setTimeout(() => {
        this.fail(`${side} recorder did not stop`)
        resolve()
      }, STOP_TIMEOUT_MS)

      recorder.addEventListener(
        'stop',
        () => {
          window.clearTimeout(timer)
          resolve()
        },
        { once: true }
      )

      try {
        recorder.stop()
      } catch {
        window.clearTimeout(timer)
        this.fail(`${side} recorder could not stop`)
        resolve()
      }
    })
  }

  private async settle(stopped: Promise<void>[]): Promise<CallAuditOutcome> {
    await Promise.all(stopped)
    await this.starting?.catch(() => undefined)
    const callId = this.callId

    if (!callId) {
      return { callId: null, complete: false, reason: this.failure ?? 'call recording never started' }
    }

    for (const side of SIDES) {
      if (!this.recorders.has(side)) {
        this.fail(`${side} audio was never recorded`)
      }
    }

    await this.queue
    const json = this.failure ? {} : { stopped: { ...this.sequences } }
    const result = await this.send({ action: 'finish', callId, json })

    if (!this.failure && !(result.ok && result.state === 'complete')) {
      this.failure = refusal(result)
    }

    return { callId, complete: !this.failure, reason: this.failure }
  }
}
