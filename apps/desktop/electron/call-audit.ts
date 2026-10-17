import http from 'node:http'

/**
 * Main-process uploader for private GPT-Live call recordings (~va01).
 *
 * The voice relay on loopback :9210 keeps the call store and admits a request
 * only when Hermes' own voice-live status gate accepts its credential. The
 * renderer never holds that credential: it names one fixed action and hands
 * over the audio; main adds the call's backend token and talks to the relay
 * with no Origin header. The relay refuses a present foreign Origin, and that
 * gate is not ours to widen.
 */

export const CALL_AUDIT_RELAY = 'http://127.0.0.1:9210'

// The relay's own limits (voice page server.py): call ids, chunk MIME types,
// 1 MiB chunks, 8 KiB JSON bodies, five-digit sequence numbers.
const CALL_ID = /^[0-9a-f]{32}$/
const AUDIO_TYPES = new Set(['audio/mp4', 'audio/ogg', 'audio/wav', 'audio/webm'])
const MAX_CHUNK_BYTES = 1024 * 1024
const MAX_JSON_BYTES = 8192
const MAX_SEQ = 9999
const LOOPBACK_HOSTS = new Set(['127.0.0.1', '[::1]', 'localhost'])

// Finish concatenates and ffprobes the whole call before it answers.
const TIMEOUT_MS: Record<CallAuditAction, number> = { chunk: 15_000, event: 15_000, finish: 120_000, start: 20_000 }

export type CallAuditAction = 'chunk' | 'event' | 'finish' | 'start'
export type CallAuditSide = 'assistant' | 'mic'

export interface CallAuditRequest {
  action: CallAuditAction
  /** `start` only: the (connection, profile) whose backend credential the relay checks. */
  connectionId?: string
  profile?: string
  callId?: string
  side?: CallAuditSide
  seq?: number
  mime?: string
  audio?: ArrayBufferView | ArrayBuffer
  /** Event or finish body; the relay validates its schema. */
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

export interface CallAuditBackend {
  authMode?: string
  baseUrl?: string
  token?: null | string
}

/** A loopback backend's own session token, or null. A remote backend's token
 *  never goes to a local relay that forwards it to another Hermes. */
export function relayCredential(backend: CallAuditBackend | null | undefined): null | string {
  if (!backend?.token || backend.authMode === 'oauth') {
    return null
  }

  try {
    return LOOPBACK_HOSTS.has(new URL(String(backend.baseUrl)).hostname) ? backend.token : null
  } catch {
    return null
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

  const body = Buffer.from(JSON.stringify(request.json))

  return body.length > MAX_JSON_BYTES ? null : { body, contentType: 'application/json', path: `${base}/${request.action}` }
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

function post(relay: string, target: RelayRequest, credential: string, timeoutMs: number): Promise<CallAuditResult> {
  return new Promise(resolve => {
    // No Origin: the relay's gate admits an absent Origin and still checks the credential upstream.
    const req = http.request(
      new URL(target.path, relay),
      {
        agent: false,
        headers: {
          Authorization: `Bearer ${credential}`,
          'Content-Length': String(target.body.length),
          'Content-Type': target.contentType
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
  /** Resolve the backend a call is placed on; main already owns this routing. */
  backend: (scope: { connectionId?: string; profile?: string }) => Promise<CallAuditBackend>
  relay?: string
}

/** The `hermes:call-audit` handler. Paths are built only from validated fields,
 *  a call id is writable only after this process started it, and the answer
 *  carries the relay's status and state — never a credential. */
export function createCallAuditBridge({ backend, relay = CALL_AUDIT_RELAY }: CallAuditBridgeOptions) {
  const credentials = new Map<string, string>()
  const refused = (error: string): CallAuditResult => ({ error, ok: false, status: 400 })

  const handle = async (input: unknown): Promise<CallAuditResult> => {
    const request = (input && typeof input === 'object' ? input : {}) as CallAuditRequest

    if (request.action === 'start') {
      const text = (value: unknown) => (typeof value === 'string' && value ? value : undefined)
      let credential: null | string

      try {
        credential = relayCredential(await backend({ connectionId: text(request.connectionId), profile: text(request.profile) }))
      } catch (error) {
        return { error: `Hermes backend unavailable (${error instanceof Error ? error.message : String(error)})`, ok: false, status: 0 }
      }

      if (!credential) {
        return { error: 'this connection has no local Hermes credential for call recording', ok: false, status: 0 }
      }

      const target = { body: Buffer.from('{}'), contentType: 'application/json', path: '/api/voice-calls/start' }
      const result = await post(relay, target, credential, TIMEOUT_MS.start)

      if (result.ok && result.callId && CALL_ID.test(result.callId)) {
        credentials.set(result.callId, credential)
      }

      return result
    }

    const callId = typeof request.callId === 'string' && CALL_ID.test(request.callId) ? request.callId : ''
    const credential = credentials.get(callId)
    const target = credential ? relayRequest(request, callId) : null

    if (!credential || !target) {
      return refused('call recording request refused by the desktop')
    }

    const result = await post(relay, target, credential, TIMEOUT_MS[request.action])

    // The relay answered finish for good (complete, or refused as incomplete).
    if (request.action === 'finish' && (result.ok || result.status === 409)) {
      credentials.delete(callId)
    }

    return result
  }

  return Object.assign(handle, {
    hasOpenCalls: () => credentials.size > 0,
    // A process quit cannot prove that renderer MediaRecorders stopped cleanly.
    // Preserve any uploaded chunks, but deliberately request an incomplete
    // relay verdict rather than inventing counts that could mint COMPLETE.
    abortOpenCalls: async () => {
      await Promise.all(
        [...credentials].map(async ([callId, credential]) => {
          const target = relayRequest({ action: 'finish', json: {} }, callId)
          if (!target) return
          const result = await post(relay, target, credential, 5_000)
          if (result.ok || result.status === 409) credentials.delete(callId)
        })
      )
    }
  })
}
