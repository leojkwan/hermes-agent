import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { once } from 'node:events'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { test } from 'vitest'

import { preflightStateDb, STATE_DB_PREFLIGHT_TIMEOUT_MS } from './state-db-preflight'

test('the desktop preflight publishes committed WAL rows before its caller can stop the backend', async (): Promise<void> => {
  const home: string = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-db-'))
  const python: string = process.env.HERMES_PYTHON || 'python3'
  const script: string = fileURLToPath(new URL('../../../../hermes_cli/backup_sqlite.py', import.meta.url))

  const child = spawn(
    python,
    [
      '-I',
      '-S',
      '-u',
      '-c',
      `
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute('PRAGMA journal_mode=WAL')
c.execute('PRAGMA wal_autocheckpoint=0')
c.execute('CREATE TABLE messages (body TEXT)')
c.commit()
c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
c.execute("INSERT INTO messages VALUES ('pending in WAL')")
c.commit()
print('ready', flush=True)
sys.stdin.readline()
c.close()
`,
      path.join(home, 'state.db')
    ],
    { stdio: ['pipe', 'pipe', 'pipe'] }
  )

  const logs: string[] = []

  try {
    await once(child.stdout!, 'data')
    await preflightStateDb({
      python,
      script,
      home,
      log: (message: string): void => {
        logs.push(message)
      }
    })
    assert.equal(child.exitCode, null)
    const backups: string[] = fs.readdirSync(home).filter((name: string): boolean => name.endsWith('.bak'))
    assert.equal(backups.length, 1, logs.join('\n'))

    const verify = spawnSync(
      python,
      [
        '-I',
        '-S',
        '-c',
        `
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as c:
    assert c.execute('SELECT body FROM messages').fetchall() == [('pending in WAL',)]
`,
        path.join(home, backups[0]!)
      ],
      { encoding: 'utf8' }
    )

    assert.equal(verify.status, 0, verify.stderr)
    const exited = once(child, 'exit')
    child.stdin!.end('\n')
    await exited
  } finally {
    if (child.exitCode === null && child.signalCode === null) {
      child.kill('SIGKILL')
      await once(child, 'exit')
    }

    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('a managed installation runs the snapshot through the installation launcher', async (): Promise<void> => {
  const home: string = fs.mkdtempSync(path.join(os.tmpdir(), 'managed-preflight-'))
  const shims: string = fs.mkdtempSync(path.join(os.tmpdir(), 'launcher-shim-'))
  const python: string = process.env.HERMES_PYTHON || 'python3'
  const script: string = fileURLToPath(new URL('../../../../hermes_cli/backup_sqlite.py', import.meta.url))

  // Stand-in for the installation launcher under `.hermes/bin`: it must accept
  // exactly what the runtime passes it — `--run-module hermes_cli.backup_sqlite
  // <home>` — and publish the snapshot like the real launcher does.
  const shim: string = path.join(shims, process.platform === 'win32' ? 'hermes.cmd' : 'hermes')
  fs.writeFileSync(
    shim,
    process.platform === 'win32'
      ? `@echo off\r\n"${python}" -I -S "${script}" %3\r\n`
      : `#!/bin/sh\nexec "${python}" -I -S "${script}" "$3"\n`
  )

  if (process.platform !== 'win32') {
    fs.chmodSync(shim, 0o755)
  }

  const logs: string[] = []

  try {
    const created = spawnSync(
      python,
      [
        '-I',
        '-S',
        '-c',
        "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute('CREATE TABLE t (x)'); c.commit(); c.close()",
        path.join(home, 'state.db')
      ],
      { encoding: 'utf8' }
    )

    assert.equal(created.status, 0, created.stderr)

    await preflightStateDb({
      python: null,
      launcher: shim,
      script,
      home,
      log: (message: string): void => {
        logs.push(message)
      }
    })

    const backups: string[] = fs.readdirSync(home).filter((name: string): boolean => name.endsWith('.bak'))
    assert.equal(backups.length, 1, logs.join('\n'))
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
    fs.rmSync(shims, { recursive: true, force: true })
  }
})

test('an older selected checkout without the snapshot helper refuses before backend stop', async (): Promise<void> => {
  const oldRoot: string = fs.mkdtempSync(path.join(os.tmpdir(), 'old-preflight-'))
  let stopped = false

  try {
    await assert.rejects(async (): Promise<void> => {
      await preflightStateDb({
        python: process.env.HERMES_PYTHON || 'python3',
        script: path.join(oldRoot, 'hermes_cli', 'backup_sqlite.py'),
        home: oldRoot,
        log: (): void => {}
      })
      stopped = true
    }, /snapshot|pre-flight/)
    assert.equal(stopped, false)
  } finally {
    fs.rmSync(oldRoot, { recursive: true, force: true })
  }
})

test('the main process keeps running while a slow snapshot runs', async (): Promise<void> => {
  const dir: string = fs.mkdtempSync(path.join(os.tmpdir(), 'slow-preflight-'))
  const release: string = path.join(dir, 'release')
  const script: string = path.join(dir, 'slow_snapshot.py')
  // Stand-in snapshot: it waits up to 3 s for a file that only a main-process timer creates.
  // A blocking call keeps that timer from firing, so the child prints "not released".
  fs.writeFileSync(
    script,
    [
      'import os, sys, time',
      'deadline = time.monotonic() + 3',
      'while time.monotonic() < deadline and not os.path.exists(sys.argv[1]):',
      '    time.sleep(0.02)',
      "print('released' if os.path.exists(sys.argv[1]) else 'not released')"
    ].join('\n')
  )
  const logs: string[] = []
  const timer = setTimeout((): void => fs.writeFileSync(release, ''), 50)

  try {
    await preflightStateDb({
      python: process.env.HERMES_PYTHON || 'python3',
      script,
      home: release,
      log: (message: string): void => {
        logs.push(message)
      }
    })
    assert.deepEqual(logs, ['[updates] state.db pre-flight: released'])
  } finally {
    clearTimeout(timer)
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('a snapshot past its limit fails with the same message and leaves no child running', async (): Promise<void> => {
  assert.equal(STATE_DB_PREFLIGHT_TIMEOUT_MS, 150_000)
  const python: string = process.env.HERMES_PYTHON || 'python3'
  const dir: string = fs.mkdtempSync(path.join(os.tmpdir(), 'hung-preflight-'))
  const pidFile: string = path.join(dir, 'pid')
  const script: string = path.join(dir, 'hung_snapshot.py')
  fs.writeFileSync(script, 'import os, sys, time\nopen(sys.argv[1], "w").write(str(os.getpid()))\ntime.sleep(30)\n')

  const message: string =
    `state.db pre-flight failed: spawnSync ${python} ETIMEDOUT. ` +
    'Update cancelled before backend shutdown. Update the selected installation with its hermes update command, then retry.'

  const logs: string[] = []

  try {
    await assert.rejects(
      preflightStateDb({
        python,
        script,
        home: pidFile,
        timeoutMs: 2_000,
        log: (message: string): void => {
          logs.push(message)
        }
      }),
      (error: Error): boolean => {
        assert.equal(error.message, message)
        assert.equal((error.cause as NodeJS.ErrnoException).code, 'ETIMEDOUT')

        return true
      }
    )
    assert.deepEqual(logs, [`[updates] ${message}`])
    const pid = Number(fs.readFileSync(pidFile, 'utf8'))
    assert.throws((): boolean => process.kill(pid, 0), { code: 'ESRCH' })
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})
