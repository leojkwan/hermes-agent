import fs, { type FileHandle } from 'node:fs/promises'
import http from 'node:http'
import os from 'node:os'
import path from 'node:path'

/**
 * Main-process uploader for private GPT-Live call recordings (~va01, ~da02).
 *
 * The voice relay on loopback :9210 keeps the call store. At every start it
 * publishes a fresh owner-only recording key; main reads that key and sends it
 * only to this fixed relay, for its call-store actions only. No Hermes token —
 * spawned, remote, SSH-forwarded or OAuth — ever goes to the relay. The
 * renderer never sees the key: it names one fixed action and hands over the
 * audio. Requests carry no Origin; the relay refuses a present foreign Origin,
 * and that gate is not ours to widen.
 */

export const CALL_AUDIT_RELAY = 'http://127.0.0.1:9210'
export const RELAY_KEY_HEADER = 'X-Voice-Relay-Key'

// The relay's own limits (voice page server.py): call ids, chunk MIME types,
// 1 MiB chunks, 8 KiB JSON bodies, five-digit sequence numbers, and its key
// (secrets.token_urlsafe(32), written with no newline).
const CALL_ID = /^[0-9a-f]{32}$/
const RELAY_KEY = /^[A-Za-z0-9_-]{43}$/
const AUDIO_TYPES = new Set(['audio/mp4', 'audio/ogg', 'audio/wav', 'audio/webm'])
const MAX_CHUNK_BYTES = 1024 * 1024
const MAX_JSON_BYTES = 8192
const MAX_SEQ = 9999

// Finish concatenates and ffprobes the whole call before it answers.
const TIMEOUT_MS: Record<CallAuditAction, number> = { chunk: 15_000, event: 15_000, finish: 120_000, start: 20_000 }
// Teardown: how long a quit waits for a start in flight, and for each incomplete finish.
const QUIT_START_WAIT_MS = 5_000
const QUIT_FINISH_MS = 5_000

export type CallAuditAction = 'chunk' | 'event' | 'finish' | 'start'
export type CallAuditSide = 'assistant' | 'mic'

export interface CallAuditRequest {
  action: CallAuditAction
  callId?: string
  side?: CallAuditSide
  seq?: number
  mime?: string
  audio?: ArrayBufferView | ArrayBuffer
  /** Finish body (the relay validates it), or the desktop provenance event. */
  json?: Record<string, unknown>
}

export interface CallAuditResult {
  ok: boolean
  /** The relay's HTTP status; 0 when no answer came back. */
  status: number
  callId?: string
  state?: string
  failure?: string
  error?: string
}

export function relayKeyPath(home = os.homedir()): string {
  return path.join(home, '.hermes', 'voice-relay', 'desktop.key')
}

/** The relay's published recording key, or null unless the file is this
 *  user's own regular, owner-only, well-formed key. Never follows a symlink. */
export async function readRelayKey(file = relayKeyPath()): Promise<null | string> {
  let handle: FileHandle | null = null

  try {
    handle = await fs.open(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW)
    const info = await handle.stat()

    if (!info.isFile() || info.uid !== process.getuid?.() || (info.mode & 0o077) !== 0) {
      return null
    }

    const key = await handle.readFile('utf8')

    return RELAY_KEY.test(key) ? key : null
  } catch {
    return null
  } finally {
    await handle?.close().catch(() => undefined)
  }
}

function audioBytes(audio: unknown): Buffer | null {
  if (audio instanceof ArrayBuffer) {
    return Buffer.from(audio)
  }

  return ArrayBuffer.isView(audio) ? Buffer.from(audio.buffer, audio.byteOffset, audio.byteLength) : null
}

interface RelayRequest {
  body: Buffer
  contentType: string
  path: string
}

const START: RelayRequest = { body: Buffer.from('{}'), contentType: 'application/json', path: '/api/voice-calls/start' }

function relayRequest(request: CallAuditRequest, callId: string): RelayRequest | null {
  const base = `/api/voice-calls/${callId}`

  if (request.action === 'chunk') {
    const mime = String(request.mime ?? '').split(';', 1)[0].trim().toLowerCase()
    const body = audioBytes(request.audio)
    const seq = request.seq

    if (
      (request.side !== 'mic' && request.side !== 'assistant') ||
      typeof seq !== 'number' ||
      !Number.isInteger(seq) ||
      seq < 0 ||
      seq > MAX_SEQ ||
      !AUDIO_TYPES.has(mime) ||
      !body?.length ||
      body.length > MAX_CHUNK_BYTES
    ) {
      return null
    }

    return { body, contentType: mime, path: `${base}/chunk?side=${request.side}&seq=${seq}` }
  }

  if (request.action !== 'event' && request.action !== 'finish') {
    return null
  }

  if (!request.json || typeof request.json !== 'object' || Array.isArray(request.json)) {
    return null
  }

  const json = request.action === 'event' ? provenanceEvent(request.json) : request.json

  if (!json) {
    return null
  }

  const body = Buffer.from(JSON.stringify(json))

  return body.length > MAX_JSON_BYTES ? null : { body, contentType: 'application/json', path: `${base}/${request.action}` }
}

/** The one event the desktop sends: that this call came from the desktop.
 *  The relay replays a call's transcript.* events as the next call's GPT-Live
 *  history, so the key must never carry anything the renderer wrote. */
function provenanceEvent(json: Record<string, unknown>): Record<string, unknown> | null {
  const { client_at: clientAt, text, type, ...rest } = json

  if (type !== 'client.source' || text !== 'desktop' || Object.keys(rest).length) {
    return null
  }

  if (clientAt === undefined) {
    return { text, type }
  }

  return typeof clientAt === 'number' && Number.isFinite(clientAt) ? { client_at: clientAt, text, type } : null
}

function relayResult(status: number, raw: Buffer): CallAuditResult {
  let body: Record<string, unknown> = {}

  try {
    const parsed = JSON.parse(raw.toString('utf8'))
    body = parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    // A non-JSON answer still has a status; that is what gets reported.
  }

  const result: CallAuditResult = { ok: status >= 200 && status < 300, status }

  for (const [key, field] of [
    ['call_id', 'callId'],
    ['state', 'state'],
    ['failure', 'failure'],
    ['error', 'error']
  ] as const) {
    if (typeof body[key] === 'string') {
      result[field] = (body[key] as string).slice(0, 200)
    }
  }

  return result
}

function post(relay: string, target: RelayRequest, key: string, timeoutMs: number): Promise<CallAuditResult> {
  return new Promise(resolve => {
    // No Origin and no Authorization: the relay admits its own key after its Origin gate.
    const req = http.request(
      new URL(target.path, relay),
      {
        agent: false,
        headers: {
          'Content-Length': String(target.body.length),
          'Content-Type': target.contentType,
          [RELAY_KEY_HEADER]: key
        },
        method: 'POST'
      },
      res => {
        const chunks: Buffer[] = []
        res.on('data', chunk => chunks.push(chunk))
        res.on('end', () => resolve(relayResult(res.statusCode ?? 0, Buffer.concat(chunks))))
        res.on('error', () => resolve({ error: 'relay answer interrupted', ok: false, status: 0 }))
      }
    )

    req.setTimeout(timeoutMs, () => req.destroy(new Error('timed out')))
    req.on('error', (error: NodeJS.ErrnoException) =>
      resolve({ error: `relay unreachable (${error.code ?? error.message})`, ok: false, status: 0 })
    )
    req.end(target.body)
  })
}

export interface CallAuditBridgeOptions {
  relay?: string
  /** The relay's recording key, read afresh at each start (the relay rotates it). */
  relayKey?: () => Promise<null | string>
}

export type CallAuditBridge = ReturnType<typeof createCallAuditBridge>

/** The `hermes:call-audit` handler. Paths are built only from validated
 *  fields, a call id is writable only by the renderer (webContents id) that
 *  started it, and the answer carries the relay's status and state — never
 *  the key. A quit, a lost renderer or a start that lands after either is
 *  finished `{}`, which the relay can only record as incomplete. */
export function createCallAuditBridge({ relay = CALL_AUDIT_RELAY, relayKey = () => readRelayKey() }: CallAuditBridgeOptions = {}) {
  const calls = new Map<string, { key: string; sender: number }>()
  const starting = new Map<Promise<CallAuditResult>, { released: boolean; sender: number }>()
  let closing = false
  const refused = (error: string): CallAuditResult => ({ error, ok: false, status: 400 })

  const finishIncomplete = async (callId: string) => {
    const call = calls.get(callId)
    const target = call && relayRequest({ action: 'finish', json: {} }, callId)

    if (!call || !target) {
      return
    }

    const result = await post(relay, target, call.key, QUIT_FINISH_MS)

    if (result.ok || result.status === 409) {
      calls.delete(callId)
    }
  }

  const open = async (sender: number, self: { released: boolean }): Promise<CallAuditResult> => {
    const key = await relayKey().catch(() => null)

    if (!key) {
      return { error: 'call recording key unavailable (is the voice relay running?)', ok: false, status: 0 }
    }

    const result = await post(relay, START, key, TIMEOUT_MS.start)

    if (!result.ok || !result.callId || !CALL_ID.test(result.callId)) {
      return result.ok ? { error: 'relay answered start without a call id', ok: false, status: result.status } : result
    }

    calls.set(result.callId, { key, sender })

    if (closing || self.released) {
      await finishIncomplete(result.callId)

      return { error: 'call recording ended before it started', ok: false, status: 0 }
    }

    return result
  }

  const handle = async (input: unknown, sender: number): Promise<CallAuditResult> => {
    const request = (input && typeof input === 'object' ? input : {}) as CallAuditRequest

    if (request.action === 'start') {
      if (closing) {
        return { error: 'desktop is quitting', ok: false, status: 0 }
      }

      const self = { released: false, sender }
      const pending = open(sender, self)
      starting.set(pending, self)

      try {
        return await pending
      } finally {
        starting.delete(pending)
      }
    }

    const callId = typeof request.callId === 'string' && CALL_ID.test(request.callId) ? request.callId : ''
    const call = calls.get(callId)
    const target = call && call.sender === sender ? relayRequest(request, callId) : null

    if (!call || !target) {
      return refused('call recording request refused by the desktop')
    }

    const result = await post(relay, target, call.key, TIMEOUT_MS[request.action])

    // The relay answered finish for good (complete, or refused as incomplete).
    if (request.action === 'finish' && (result.ok || result.status === 409)) {
      calls.delete(callId)
    }

    return result
  }

  return Object.assign(handle, {
    hasOpenCalls: () => calls.size > 0 || starting.size > 0,
    // A quit cannot prove the renderer's MediaRecorders stopped cleanly. Keep
    // the uploaded chunks, but ask for an incomplete verdict rather than
    // invent stopped counts that could mint COMPLETE.
    abortOpenCalls: async () => {
      closing = true
      const pending = Promise.allSettled([...starting.keys()])
      await Promise.race([pending, new Promise(resolve => setTimeout(resolve, QUIT_START_WAIT_MS).unref())])
      await Promise.all([...calls.keys()].map(finishIncomplete))
    },
    // A renderer that reloads, crashes or closes cannot finish its calls; end them incomplete.
    releaseSender: async (sender: number) => {
      const pending = [...starting].filter(([, self]) => self.sender === sender)

      for (const [, self] of pending) {
        self.released = true
      }

      await Promise.all([
        ...[...calls].filter(([, call]) => call.sender === sender).map(([callId]) => finishIncomplete(callId)),
        ...pending.map(([promise]) => promise.catch(() => undefined))
      ])
    }
  })
}

/** `app.exit` skips before-quit, so an exit path first closes the bridge
 *  (open calls end incomplete, new starts are refused), then stops the
 *  backend, then exits — whatever fails. */
export async function exitAfterShutdown(
  {
    backendShutdown,
    callAudit,
    exit
  }: {
    backendShutdown: { run: () => Promise<void> }
    callAudit: Pick<CallAuditBridge, 'abortOpenCalls'>
    exit: (code: number) => void
  },
  code: number
): Promise<void> {
  try {
    await callAudit.abortOpenCalls()
  } catch {
    // The relay may be gone; the exit must still happen.
  }

  try {
    await backendShutdown.run()
  } catch {
    // Already logged by backendShutdown; the exit must still happen.
  }

  exit(code)
}
