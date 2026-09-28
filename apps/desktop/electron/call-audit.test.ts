import fs from 'node:fs'
import http from 'node:http'
import type { AddressInfo } from 'node:net'
import os from 'node:os'
import path from 'node:path'

import { afterEach, describe, expect, it } from 'vitest'

import * as callAudit from './call-audit'

const { createCallAuditBridge } = callAudit

// ~va01/~da02: the renderer hands main a fixed call-recording action; main
// adds the relay's published recording key and talks to the loopback relay
// with no Origin. No Hermes token ever goes to the relay. These tests run the
// real transport against a local stand-in relay.

const KEY = 'k'.repeat(43)
const CALL_ID = 'a'.repeat(32)
const OTHER_CALL_ID = 'b'.repeat(32)
const REMOTE_TOKEN = 'remote-forward-token-0123456789'
const LOCAL_TOKEN = 'spawned-backend-token-0123456789'
const MIC = { action: 'chunk', audio: new Uint8Array([1]), mime: 'audio/webm', seq: 0, side: 'mic' } as const

interface Seen {
  body: Buffer
  headers: http.IncomingHttpHeaders
  url: string
}

let server: http.Server | null = null
const temporary: string[] = []

async function relay(answer: (seen: Seen) => [number, unknown] | Promise<[number, unknown]>) {
  const seen: Seen[] = []

  server = http.createServer((req, res) => {
    const chunks: Buffer[] = []
    req.on('data', chunk => chunks.push(chunk))
    req.on('end', async () => {
      const entry = { body: Buffer.concat(chunks), headers: req.headers, url: req.url ?? '' }
      seen.push(entry)
      const [status, body] = await answer(entry)
      res.writeHead(status, { 'Content-Type': 'application/json' }).end(JSON.stringify(body))
    })
  })
  await new Promise<void>(resolve => server!.listen(0, '127.0.0.1', resolve))

  return { seen, url: `http://127.0.0.1:${(server!.address() as AddressInfo).port}` }
}

const opened = (id = CALL_ID): [number, unknown] => [201, { call_id: id, state: 'open' }]

// Every bridge here is also handed the resolver the pre-key bridge used to
// lend the call's backend token. The bridge must ignore it.
const LENDER = {
  backend: async () => ({ authMode: 'token', baseUrl: 'http://127.0.0.1:56872', token: LOCAL_TOKEN })
}

function bridgeTo(relayUrl: string, relayKey: () => Promise<null | string> = async () => KEY) {
  const options = { ...LENDER, relay: relayUrl, relayKey }

  return createCallAuditBridge(options)
}

function held() {
  let release: () => void = () => undefined
  const gate = new Promise<void>(resolve => (release = resolve))

  return { gate, release: () => release() }
}

async function until(predicate: () => boolean) {
  for (let i = 0; i < 200 && !predicate(); i += 1) {
    await new Promise(resolve => setTimeout(resolve, 5))
  }

  expect(predicate()).toBe(true)
}

function keyFile(contents: string, mode = 0o600): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'call-audit-key-'))
  temporary.push(dir)
  const file = path.join(dir, 'desktop.key')
  fs.writeFileSync(file, contents, { mode })
  fs.chmodSync(file, mode)

  return file
}

afterEach(async () => {
  server?.closeAllConnections()
  await new Promise(resolve => (server ? server.close(resolve) : resolve(null)))
  server = null

  for (const dir of temporary.splice(0)) {
    fs.rmSync(dir, { force: true, recursive: true })
  }
})

function expectNoToken(seen: Seen[], token: string) {
  for (const entry of seen) {
    expect(JSON.stringify(entry.headers)).not.toContain(token)
    expect(entry.headers.authorization).toBeUndefined()
    expect(entry.headers.origin).toBeUndefined()
    expect(entry.headers['x-voice-relay-key']).toBe(KEY)
  }
}

describe('call audit bridge', () => {
  it('start never lends a remote-mode SSH-forwarded token', async () => {
    const stand = await relay(() => opened())
    // The pre-key bridge resolved the call's backend and lent its token when
    // the base URL looked loopback — which an SSH forward always does.
    const options = {
      backend: async () => ({ authMode: 'token', baseUrl: 'http://127.0.0.1:53999', mode: 'remote', token: REMOTE_TOKEN }),
      relay: stand.url,
      relayKey: async () => KEY
    }

    const result = await createCallAuditBridge(options)({ action: 'start', connectionId: 'ssh-box' }, 1)

    expect(result).toMatchObject({ callId: CALL_ID, ok: true, status: 201 })
    expect(stand.seen).toHaveLength(1)
    expectNoToken(stand.seen, REMOTE_TOKEN)
  })

  it('start never sends a spawned local backend token', async () => {
    const stand = await relay(() => opened())

    const bridge = bridgeTo(stand.url)
    await bridge({ action: 'start' }, 1)
    // Renderer-supplied routing is ignored, not honored.
    await bridge({ ...MIC, callId: CALL_ID, headers: { Origin: 'http://evil.example' }, path: '/elsewhere' }, 1)
    await bridge({ action: 'event', callId: CALL_ID, json: { text: 'desktop', type: 'client.source' } }, 1)

    expect(stand.seen.map(entry => entry.url)).toEqual([
      '/api/voice-calls/start',
      `/api/voice-calls/${CALL_ID}/chunk?side=mic&seq=0`,
      `/api/voice-calls/${CALL_ID}/event`
    ])
    expectNoToken(stand.seen, LOCAL_TOKEN)
  })

  it('reads only a private, regular, well-formed relay key file', async () => {
    const { readRelayKey } = callAudit
    const good = keyFile(KEY)
    const link = path.join(path.dirname(good), 'link.key')
    fs.symlinkSync(good, link)

    expect(await readRelayKey(good)).toBe(KEY)
    expect(await readRelayKey(keyFile(KEY, 0o644))).toBeNull()
    expect(await readRelayKey(link)).toBeNull()
    expect(await readRelayKey(keyFile('short'))).toBeNull()
    expect(await readRelayKey(keyFile(`${KEY}\n`))).toBeNull()
    expect(await readRelayKey(path.join(path.dirname(good), 'missing.key'))).toBeNull()
    expect(await readRelayKey(path.dirname(good))).toBeNull()
  })

  it('start refuses without a usable relay key and sends nothing', async () => {
    const stand = await relay(() => opened())
    const unsafe = keyFile(KEY, 0o644)
    const bridge = bridgeTo(stand.url, () => callAudit.readRelayKey(unsafe))

    expect(await bridge({ action: 'start' }, 1)).toMatchObject({ ok: false, status: 0 })
    expect(await bridgeTo(stand.url, async () => null)({ action: 'start' }, 1)).toMatchObject({ ok: false, status: 0 })
    expect(stand.seen).toHaveLength(0)
  })

  it('a pending start counts as an open call', async () => {
    const hold = held()
    const stand = await relay(async () => {
      await hold.gate

      return opened()
    })

    const bridge = bridgeTo(stand.url)
    const starting = bridge({ action: 'start' }, 1)
    await until(() => stand.seen.length === 1)

    expect(bridge.hasOpenCalls()).toBe(true)
    hold.release()
    await starting
    expect(bridge.hasOpenCalls()).toBe(true)
  })

  it('abortOpenCalls waits for a pending start, then finishes it {}', async () => {
    const hold = held()
    const stand = await relay(async ({ url }) => {
      if (url.endsWith('/start')) {
        await hold.gate

        return opened()
      }

      return [409, { state: 'incomplete' }]
    })

    const bridge = bridgeTo(stand.url)
    const starting = bridge({ action: 'start' }, 1)
    await until(() => stand.seen.length === 1)
    const aborting = bridge.abortOpenCalls()
    hold.release()
    await aborting

    expect(await starting).toMatchObject({ ok: false })
    expect(stand.seen.map(entry => entry.url)).toEqual(['/api/voice-calls/start', `/api/voice-calls/${CALL_ID}/finish`])
    expect(JSON.parse(stand.seen[1].body.toString())).toEqual({})
    expect(bridge.hasOpenCalls()).toBe(false)
    expect(await bridge({ action: 'start' }, 1)).toMatchObject({ ok: false, status: 0 })
    expect(stand.seen).toHaveLength(2)
  })

  it("releaseSender finishes that sender's calls {} and only theirs", async () => {
    let next = 0
    const stand = await relay(({ url }) =>
      url.endsWith('/start') ? opened([CALL_ID, OTHER_CALL_ID][next++]) : [409, { state: 'incomplete' }]
    )

    const bridge = bridgeTo(stand.url)
    await bridge({ action: 'start' }, 1)
    await bridge({ action: 'start' }, 2)

    await bridge.releaseSender(1)

    expect(stand.seen.map(entry => entry.url).slice(2)).toEqual([`/api/voice-calls/${CALL_ID}/finish`])
    expect(JSON.parse(stand.seen[2].body.toString())).toEqual({})
    expect(await bridge({ ...MIC, callId: CALL_ID }, 1)).toMatchObject({ ok: false, status: 400 })
    expect(await bridge({ ...MIC, callId: OTHER_CALL_ID }, 2)).toMatchObject({ status: 409 })
    expect(bridge.hasOpenCalls()).toBe(true)
  })

  it('a start still pending when its sender goes away is finished {} on arrival', async () => {
    const hold = held()
    const stand = await relay(async ({ url }) => {
      if (url.endsWith('/start')) {
        await hold.gate

        return opened()
      }

      return [409, { state: 'incomplete' }]
    })

    const bridge = bridgeTo(stand.url)
    const starting = bridge({ action: 'start' }, 1)
    await until(() => stand.seen.length === 1)
    const releasing = bridge.releaseSender(1)
    hold.release()
    await releasing

    expect(await starting).toMatchObject({ ok: false })
    expect(stand.seen.map(entry => entry.url)).toEqual(['/api/voice-calls/start', `/api/voice-calls/${CALL_ID}/finish`])
    expect(bridge.hasOpenCalls()).toBe(false)
  })

  it("a non-owner sender cannot write another sender's call", async () => {
    const stand = await relay(() => opened())
    const bridge = bridgeTo(stand.url)
    await bridge({ action: 'start' }, 1)

    for (const request of [
      { ...MIC, callId: CALL_ID },
      { action: 'event', callId: CALL_ID, json: { type: 'client.source' } },
      { action: 'finish', callId: CALL_ID, json: {} }
    ]) {
      expect(await bridge(request, 2)).toMatchObject({ ok: false, status: 400 })
    }

    expect(stand.seen).toHaveLength(1)
  })

  it("reports the relay's exact refusal and cannot write to a call it did not start", async () => {
    const stand = await relay(() => [401, { error: 'sign in to Hermes' }])
    const bridge = bridgeTo(stand.url)

    expect(await bridge({ action: 'start' }, 1)).toEqual({ error: 'sign in to Hermes', ok: false, status: 401 })

    const orphan = await bridge({ action: 'finish', callId: CALL_ID, json: { stopped: { assistant: 1, mic: 1 } } }, 1)

    expect(orphan).toMatchObject({ ok: false, status: 400 })
    expect(stand.seen).toHaveLength(1)
    expect(bridge.hasOpenCalls()).toBe(false)
  })

  it('refuses malformed chunks locally and keeps a finish refusal a refusal', async () => {
    const stand = await relay(({ url }) => (url.endsWith('/start') ? opened() : [409, { state: 'incomplete' }]))
    const bridge = bridgeTo(stand.url)
    await bridge({ action: 'start' }, 1)
    const chunk = { ...MIC, callId: CALL_ID }

    for (const bad of [
      { ...chunk, side: 'speaker' },
      { ...chunk, seq: 10_000 },
      { ...chunk, seq: 1.5 },
      { ...chunk, mime: 'text/html' },
      { ...chunk, audio: new Uint8Array(1024 * 1024 + 1) },
      { ...chunk, callId: '../../etc' },
      { ...chunk, action: 'delete' }
    ]) {
      expect(await bridge(bad, 1)).toMatchObject({ ok: false, status: 400 })
    }

    expect(stand.seen).toHaveLength(1)
    expect(await bridge({ action: 'finish', callId: CALL_ID, json: { stopped: { assistant: 0, mic: 0 } } }, 1)).toEqual({
      ok: false,
      state: 'incomplete',
      status: 409
    })
    expect(bridge.hasOpenCalls()).toBe(false)
  })

  it('forwards only the desktop provenance event, never a transcript or any other event', async () => {
    const stand = await relay(({ url }) => (url.endsWith('/start') ? opened() : [201, { seq: 0 }]))
    const bridge = bridgeTo(stand.url)
    await bridge({ action: 'start' }, 1)
    const event = { action: 'event', callId: CALL_ID } as const

    // The relay replays transcript.* events as the next call's GPT-Live history.
    for (const json of [
      { client_at: 1, text: 'x', type: 'transcript.user' },
      { client_at: 1, text: 'x', type: 'transcript.assistant' },
      { client_at: 1, text: 'page', type: 'client.source' },
      { client_at: 1, id: 'x', text: 'desktop', type: 'client.source' },
      { client_at: '1', text: 'desktop', type: 'client.source' },
      { client_at: 1, text: 'desktop', type: 'task.queued' }
    ]) {
      expect(await bridge({ ...event, json }, 1)).toMatchObject({ ok: false, status: 400 })
    }

    expect(stand.seen).toHaveLength(1)
    const provenance = { client_at: 1, text: 'desktop', type: 'client.source' }

    expect(await bridge({ ...event, json: provenance }, 1)).toMatchObject({ ok: true, status: 201 })
    expect(stand.seen[1].url).toBe(`/api/voice-calls/${CALL_ID}/event`)
    expect(JSON.parse(stand.seen[1].body.toString())).toEqual(provenance)
  })

  it('on app quit marks an open call incomplete without inventing stopped audio counts', async () => {
    const stand = await relay(({ url, body }) => {
      if (url.endsWith('/start')) return opened()
      if (url.endsWith('/finish') && body.toString() === '{}') return [409, { state: 'incomplete' }]
      return [400, { error: 'unexpected request' }]
    })

    const bridge = bridgeTo(stand.url)
    expect(await bridge({ action: 'start' }, 1)).toMatchObject({ callId: CALL_ID, ok: true })

    await bridge.abortOpenCalls()

    expect(stand.seen.map(entry => entry.url)).toEqual(['/api/voice-calls/start', `/api/voice-calls/${CALL_ID}/finish`])
    expect(JSON.parse(stand.seen[1].body.toString())).toEqual({})
    expect(await bridge({ action: 'event', callId: CALL_ID, json: { type: 'client.source' } }, 1)).toMatchObject({
      ok: false,
      status: 400
    })
  })

  it('reports an unreachable relay as status 0 without the key', async () => {
    const stand = await relay(() => [201, {}])
    const url = stand.url
    await new Promise(resolve => server!.close(resolve))
    server = null

    const result = await bridgeTo(url)({ action: 'start' }, 1)

    expect(result).toMatchObject({ ok: false, status: 0 })
    expect(JSON.stringify(result)).not.toContain(KEY)
  })
})

describe('exitAfterShutdown', () => {
  it('aborts open calls before the backend stops and the app exits', async () => {
    const order: string[] = []

    await callAudit.exitAfterShutdown(
      {
        backendShutdown: { run: async () => void order.push('backend') },
        callAudit: { abortOpenCalls: async () => void order.push('abort') },
        exit: code => void order.push(`exit ${code}`)
      },
      0
    )

    expect(order).toEqual(['abort', 'backend', 'exit 0'])
  })

  it('still exits when the abort or the backend shutdown fails', async () => {
    const exits: number[] = []

    await callAudit.exitAfterShutdown(
      {
        backendShutdown: { run: () => Promise.reject(new Error('backend')) },
        callAudit: { abortOpenCalls: () => Promise.reject(new Error('relay')) },
        exit: code => void exits.push(code)
      },
      3
    )

    expect(exits).toEqual([3])
  })

  it('refuses a start that arrives while the backend stops, even with no call open', async () => {
    const { seen, url } = await relay(() => opened())
    const bridge = bridgeTo(url)
    const backend = held()
    const exits: number[] = []

    const exiting = callAudit.exitAfterShutdown(
      { backendShutdown: { run: () => backend.gate }, callAudit: bridge, exit: code => void exits.push(code) },
      0
    )

    expect(await bridge({ action: 'start' }, 1)).toMatchObject({ ok: false, status: 0 })
    backend.release()
    await exiting

    expect(seen).toEqual([])
    expect(exits).toEqual([0])
  })
})
