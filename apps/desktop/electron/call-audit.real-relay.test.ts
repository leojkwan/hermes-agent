import { type ChildProcess, spawn } from 'node:child_process'
import fs from 'node:fs/promises'
import http from 'node:http'
import os from 'node:os'
import path from 'node:path'

import { afterAll, beforeAll, describe, expect, it } from 'vitest'

import { createCallAuditBridge, RELAY_KEY_HEADER, readRelayKey, relayKeyPath } from './call-audit'

/**
 * The bridge against the voice page's own relay (~da02 test 14). Set
 * HERMES_VOICE_RELAY_DIR to a voice page checkout; skipped otherwise.
 *
 * The relay runs under a throwaway HOME, so it publishes its key and keeps its
 * call store there. Nothing leaves loopback: the Hermes gateway and Ollama are
 * pointed at a closed port and the judge warm-up is stubbed out.
 */
const RELAY_DIR = process.env.HERMES_VOICE_RELAY_DIR
const CLOSED = 'http://127.0.0.1:9'
const SENDER = 1

const SERVE = `
import sys
sys.path.insert(0, sys.argv[1])
import server
server.warm_judge = lambda: None
server.UPSTREAM = ${JSON.stringify(CLOSED)}
server.OLLAMA_EMBED = ${JSON.stringify(`${CLOSED}/api/embed`)}
server.OLLAMA_GENERATE = ${JSON.stringify(`${CLOSED}/api/generate`)}
if server._publish_desktop_key() is None:
    sys.exit("key not published")
httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
print(httpd.server_address[1], flush=True)
httpd.serve_forever()
`

describe.skipIf(!RELAY_DIR)('call recording against the real relay', () => {
  let home = ''
  let relay = ''
  let child: ChildProcess | null = null

  beforeAll(async () => {
    home = await fs.mkdtemp(path.join(os.tmpdir(), 'call-audit-relay-'))
    child = spawn('python3', ['-c', SERVE, RELAY_DIR!], {
      env: { HOME: home, PATH: '/opt/homebrew/bin:/usr/bin:/bin', PYTHONDONTWRITEBYTECODE: '1' },
      stdio: ['ignore', 'pipe', 'pipe']
    })
    const port = await new Promise<string>((resolve, reject) => {
      let err = ''
      child!.stderr!.on('data', chunk => (err += chunk))
      child!.stdout!.once('data', chunk => resolve(String(chunk).trim()))
      child!.once('exit', code => reject(new Error(`relay exited ${code}: ${err.slice(-400)}`)))
    })
    relay = `http://127.0.0.1:${port}`
  }, 20_000)

  // Straight to the relay with its key, past the bridge that would refuse first.
  const keyedPost = (target: string, key: string, json: unknown) =>
    new Promise<number>((resolve, reject) => {
      const body = Buffer.from(JSON.stringify(json))
      const req = http.request(
        new URL(target, relay),
        {
          headers: { 'Content-Length': String(body.length), 'Content-Type': 'application/json', [RELAY_KEY_HEADER]: key },
          method: 'POST'
        },
        res => res.resume().on('end', () => resolve(res.statusCode ?? 0))
      )
      req.on('error', reject)
      req.end(body)
    })

  afterAll(async () => {
    child?.kill()
    await fs.rm(home, { force: true, recursive: true })
  })

  it('publishes an owner-only key the desktop reads', async () => {
    const info = await fs.stat(relayKeyPath(home))

    expect(info.mode & 0o777).toBe(0o600)
    expect(await readRelayKey(relayKeyPath(home))).toMatch(/^[A-Za-z0-9_-]{43}$/)
  })

  it('refuses a start with any other key', async () => {
    const bridge = createCallAuditBridge({ relay, relayKey: async () => 'x'.repeat(43) })

    // No key match falls back to the Hermes sign-in check, which cannot answer here.
    expect(await bridge({ action: 'start' }, SENDER)).toMatchObject({ ok: false, status: 503 })
  })

  it('records a desktop call with the published key and ends it incomplete', async () => {
    const bridge = createCallAuditBridge({ relay, relayKey: () => readRelayKey(relayKeyPath(home)) })
    const start = await bridge({ action: 'start' }, SENDER)

    expect(start).toMatchObject({ ok: true, state: 'open', status: 201 })
    const callId = start.callId!

    // The renderer fails closed unless this event lands (src/lib/call-audit.ts).
    const source = await bridge(
      { action: 'event', callId, json: { client_at: 1, text: 'desktop', type: 'client.source' } },
      SENDER
    )

    expect(source).toMatchObject({ ok: true, status: 201 })

    for (const side of ['mic', 'assistant'] as const) {
      const chunk = await bridge(
        { action: 'chunk', audio: new Uint8Array([1, 2, 3]), callId, mime: 'audio/webm', seq: 0, side },
        SENDER
      )

      expect(chunk).toMatchObject({ ok: true, status: 201 })
    }

    expect(await bridge({ action: 'finish', callId, json: {} }, SENDER)).toMatchObject({
      ok: false,
      state: 'incomplete',
      status: 409
    })
    expect(bridge.hasOpenCalls()).toBe(false)

    const state = JSON.parse(await fs.readFile(path.join(home, '.hermes', 'voice-calls', callId, 'state.json'), 'utf8'))

    expect(state).toMatchObject({ failure: 'finish_rejected', state: 'incomplete' })
    expect(state.events.map((event: { type: string }) => event.type)).toEqual(['client.source'])
    expect(state.finish_uploaded).toEqual({ assistant: 1, mic: 1 })
  })

  it('refuses a keyed transcript event, which the relay would replay into the next call', async () => {
    const key = await readRelayKey(relayKeyPath(home))
    const bridge = createCallAuditBridge({ relay, relayKey: async () => key })
    const callId = (await bridge({ action: 'start' }, SENDER)).callId!

    for (const type of ['transcript.user', 'transcript.assistant']) {
      expect(await keyedPost(`/api/voice-calls/${callId}/event`, key!, { text: 'x', type })).toBeGreaterThanOrEqual(400)
    }

    await bridge({ action: 'finish', callId, json: {} }, SENDER)
    const state = JSON.parse(await fs.readFile(path.join(home, '.hermes', 'voice-calls', callId, 'state.json'), 'utf8'))

    expect(state.events).toEqual([])
  })
})
