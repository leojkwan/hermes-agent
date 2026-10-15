import { execFile } from 'node:child_process'

import { hiddenWindowsChildOptions } from '../windows-child-options'

interface StateDbPreflight {
  python: string | null
  script: string
  home: string
  log: (message: string) => void
  /**
   * The installation launcher of a PM-managed checkout (`.hermes/bin/hermes`).
   * A managed checkout carries no venv of its own — the launcher owns
   * interpreter and generation selection there — so the snapshot runs through
   * it exactly like the update check does (`readSourceUpdate`).
   */
  launcher?: string | null
  /** Above the module's own 120 s copy-and-check budget (PREFLIGHT_BUDGET_SECONDS), so a slow
   *  snapshot fails with its own message; this limit only catches a hung interpreter. */
  timeoutMs?: number
}

export const STATE_DB_PREFLIGHT_TIMEOUT_MS = 150_000

// The caller awaits this before it stops the backend. It runs the snapshot in an asynchronous
// child process, so the main process keeps serving the window while the copy runs.
export async function preflightStateDb({
  python,
  script,
  home,
  log,
  launcher = null,
  timeoutMs = STATE_DB_PREFLIGHT_TIMEOUT_MS
}: StateDbPreflight): Promise<void> {
  try {
    const command: string | null = launcher ?? python

    if (!command) {
      throw new Error('Python not found')
    }

    const args: string[] = launcher ? ['--run-module', 'hermes_cli.backup_sqlite', home] : ['-I', '-S', script, home]

    // Node refuses direct .cmd execFile; an older published launcher can still
    // be one. Same fail-closed guard as the update check: shell:true would
    // interpolate untrusted paths, so keep cmd.exe's one unavoidable parse
    // closed instead.
    const viaCmd: boolean = process.platform === 'win32' && /\.cmd$/i.test(command)

    if (viaCmd && [command, ...args].some((value: string): boolean => /["%&|<>^\r\n]/.test(value))) {
      throw new Error('The pre-flight snapshot contains an unsafe Windows command argument.')
    }

    const file: string = viaCmd ? (process.env.ComSpec ?? 'cmd.exe') : command

    const result: string = await new Promise<string>((resolve, reject) => {
      const child = execFile(
        file,
        viaCmd
          ? ['/d', '/v:off', '/s', '/c', `""${command}" ${args.map((arg: string): string => `"${arg}"`).join(' ')}"`]
          : args,
        hiddenWindowsChildOptions({ encoding: 'utf8', timeout: timeoutMs, windowsVerbatimArguments: viaCmd }),
        (error, stdout, stderr) => {
          if (child.killed) {
            // execFile's timeout killed the child; a SIGTERM handler can still exit zero. Keep the
            // message and code the earlier execFileSync call produced, so logs and support read alike.
            reject(Object.assign(new Error(`spawnSync ${file} ETIMEDOUT`), { code: 'ETIMEDOUT', cause: error }))
          } else if (error) {
            // execFile ends "Command failed: …" with a newline even when stderr is empty;
            // execFileSync did not. Keep the earlier message.
            if (!stderr) {
              error.message = error.message.replace(/\n$/, '')
            }

            reject(error)
          } else {
            resolve(String(stdout))
          }
        }
      )

      // The snapshot reads no input.
      child.stdin?.end()
    })

    log(`[updates] state.db pre-flight: ${result.trim()}`)
  } catch (error: unknown) {
    const message =
      `state.db pre-flight failed: ${error instanceof Error ? error.message : String(error)}. ` +
      'Update cancelled before backend shutdown. Update the selected installation with its hermes update command, then retry.'

    log(`[updates] ${message}`)
    throw new Error(message, { cause: error })
  }
}
