import http from 'node:http'
import type { AddressInfo } from 'node:net'

import { afterEach, describe, expect, it } from 'vitest'

import { createCallAuditBridge, relayCredential } from './call-audit'

// ~va01: the renderer hands main a fixed call-recording action; main adds the
// call's backend credential and talks to the loopback relay with no Origin.
// These tests run the real transport against a local stand-in relay; they say
// nothing about whether the real relay's upstream accepts a given token.

const TOKEN = 'tok-secret-0123456789'
const CALL_ID = 'a'.repeat(32)
const LOCAL_BACKEND = { authMode: 'token', baseUrl: 'http://127.0.0.1:53111', token: TOKEN }

interface Seen {
  body: Buffer
  headers: http.IncomingHttpHeaders
  url: string
}

let server: http.Server | null = null

async function relay(answer: (seen: Seen) => [number, unknown]) {
  const seen: Seen[] = []

  server = http.createServer((req, res) => {
    const chunks: Buffer[] = []
    req.on('data', chunk => chunks.push(chunk))
    req.on('end', () => {
      const entry = { body: Buffer.concat(chunks), headers: req.headers, url: req.url ?? '' }
      seen.push(entry)
      const [status, body] = answer(entry)
      res.writeHead(status, { 'Content-Type': 'application/json' }).end(JSON.stringify(body))
    })
  })
  await new Promise<void>(resolve => server!.listen(0, '127.0.0.1', resolve))

  return { seen, url: `http://127.0.0.1:${(server!.address() as AddressInfo).port}` }
}

afterEach(async () => {
  await new Promise(resolve => (server ? server.close(resolve) : resolve(null)))
  server = null
})

describe('call audit bridge', () => {
  it('sends the backend credential to the relay without an Origin and never returns it', async () => {
    const stand = await relay(({ url }) =>
      url.endsWith('/start') ? [201, { call_id: CALL_ID, state: 'open' }] : [201, { seq: 0, side: 'mic' }]
    )

    const bridge = createCallAuditBridge({ backend: async () => LOCAL_BACKEND, relay: stand.url })
    const audio = new Uint8Array([1, 2, 3, 4])

    const results = [
      await bridge({ action: 'start', profile: 'default' }),
      await bridge({
        action: 'chunk',
        audio,
        callId: CALL_ID,
        // Renderer-supplied routing is ignored, not honored.
        headers: { Origin: 'http://evil.example' },
        mime: 'audio/webm;codecs=opus',
        path: '/elsewhere',
        seq: 0,
        side: 'mic',
        url: 'http://evil.example/'
      }),
      await bridge({ action: 'event', callId: CALL_ID, json: { text: 'desktop', type: 'client.source' } })
    ]

    expect(results[0]).toEqual({ callId: CALL_ID, ok: true, state: 'open', status: 201 })
    expect(stand.seen.map(entry => entry.url)).toEqual([
      '/api/voice-calls/start',
      `/api/voice-calls/${CALL_ID}/chunk?side=mic&seq=0`,
      `/api/voice-calls/${CALL_ID}/event`
    ])

    for (const entry of stand.seen) {
      expect(entry.headers.authorization).toBe(`Bearer ${TOKEN}`)
      expect(entry.headers.origin).toBeUndefined()
      expect(entry.headers['x-hermes-session-token']).toBeUndefined()
    }

    expect(stand.seen[1].headers['content-type']).toBe('audio/webm')
    expect([...stand.seen[1].body]).toEqual([1, 2, 3, 4])
    expect(JSON.parse(stand.seen[2].body.toString())).toEqual({ text: 'desktop', type: 'client.source' })
    expect(JSON.stringify(results)).not.toContain(TOKEN)
  })

  it("reports the relay's exact refusal and cannot write to a call it did not start", async () => {
    const stand = await relay(() => [401, { error: 'sign in to Hermes' }])
    const bridge = createCallAuditBridge({ backend: async () => LOCAL_BACKEND, relay: stand.url })

    expect(await bridge({ action: 'start' })).toEqual({ error: 'sign in to Hermes', ok: false, status: 401 })

    const orphan = await bridge({ action: 'finish', callId: CALL_ID, json: { stopped: { assistant: 1, mic: 1 } } })

    expect(orphan).toMatchObject({ ok: false, status: 400 })
    expect(stand.seen).toHaveLength(1)
  })

  it('refuses malformed chunks locally and keeps a finish refusal a refusal', async () => {
    const stand = await relay(({ url }) =>
      url.endsWith('/start') ? [201, { call_id: CALL_ID, state: 'open' }] : [409, { state: 'incomplete' }]
    )

    const bridge = createCallAuditBridge({ backend: async () => LOCAL_BACKEND, relay: stand.url })
    await bridge({ action: 'start' })
    const chunk = { action: 'chunk', audio: new Uint8Array([1]), callId: CALL_ID, mime: 'audio/webm', seq: 0, side: 'mic' }

    for (const bad of [
      { ...chunk, side: 'speaker' },
      { ...chunk, seq: 10_000 },
      { ...chunk, seq: 1.5 },
      { ...chunk, mime: 'text/html' },
      { ...chunk, audio: new Uint8Array(1024 * 1024 + 1) },
      { ...chunk, callId: '../../etc' },
      { ...chunk, action: 'delete' }
    ]) {
      expect(await bridge(bad)).toMatchObject({ ok: false, status: 400 })
    }

    expect(stand.seen).toHaveLength(1)
    expect(await bridge({ action: 'finish', callId: CALL_ID, json: { stopped: { assistant: 0, mic: 0 } } })).toEqual({
      ok: false,
      state: 'incomplete',
      status: 409
    })
  })

  it('never lends a remote or OAuth backend token to the local relay', async () => {
    expect(relayCredential(LOCAL_BACKEND)).toBe(TOKEN)
    expect(relayCredential({ ...LOCAL_BACKEND, baseUrl: 'http://localhost:9119' })).toBe(TOKEN)
    expect(relayCredential({ ...LOCAL_BACKEND, baseUrl: 'https://hermes.example.com' })).toBeNull()
    expect(relayCredential({ ...LOCAL_BACKEND, baseUrl: 'http://127.0.0.1.example.com' })).toBeNull()
    expect(relayCredential({ ...LOCAL_BACKEND, authMode: 'oauth' })).toBeNull()

    const stand = await relay(() => [201, { call_id: CALL_ID, state: 'open' }])

    const bridge = createCallAuditBridge({
      backend: async () => ({ ...LOCAL_BACKEND, baseUrl: 'https://hermes.example.com' }),
      relay: stand.url
    })

    expect(await bridge({ action: 'start' })).toMatchObject({ ok: false, status: 0 })
    expect(stand.seen).toHaveLength(0)
  })

  it('on app quit marks an open call incomplete without inventing stopped audio counts', async () => {
    const stand = await relay(({ url, body }) => {
      if (url.endsWith('/start')) return [201, { call_id: CALL_ID, state: 'open' }]
      if (url.endsWith('/finish') && body.toString() === '{}') return [409, { state: 'incomplete' }]
      return [400, { error: 'unexpected request' }]
    })
    const bridge = createCallAuditBridge({ backend: async () => LOCAL_BACKEND, relay: stand.url })
    expect(await bridge({ action: 'start' })).toMatchObject({ ok: true, callId: CALL_ID })

    await bridge.abortOpenCalls()

    expect(stand.seen.map(entry => entry.url)).toEqual([
      '/api/voice-calls/start',
      `/api/voice-calls/${CALL_ID}/finish`
    ])
    expect(JSON.parse(stand.seen[1].body.toString())).toEqual({})
    expect(await bridge({ action: 'event', callId: CALL_ID, json: { type: 'client.source' } })).toMatchObject({
      ok: false,
      status: 400
    })
  })

  it('reports an unreachable relay as status 0', async () => {
    const stand = await relay(() => [201, {}])
    const url = stand.url
    await new Promise(resolve => server!.close(resolve))
    server = null

    const result = await createCallAuditBridge({ backend: async () => LOCAL_BACKEND, relay: url })({ action: 'start' })

    expect(result).toMatchObject({ ok: false, status: 0 })
    expect(JSON.stringify(result)).not.toContain(TOKEN)
  })
})
