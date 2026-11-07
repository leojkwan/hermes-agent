import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  captureConnectionCacheValidity,
  connectionInstallIds,
  evictConnectionCaches,
  sshRosterCache
} from './connection-caches'
import { probeCachedConnectionInstallId } from './registry-backend-identity'
import { cacheSshProfileInventory, fetchRosterSourceData } from './roster-source-fetch'

test('SSH inventory is cached before identity settles and survives identity exec failure', async () => {
  const id = 'ssh-identity-failure'
  evictConnectionCaches(id)
  sshRosterCache.set(id, ['old-profile'])
  connectionInstallIds.set(id, { id: 'known-install', ts: 1 })
  const profiles = ['default', 'leo']
  let rejectIdentity!: (error: Error) => void

  const identity = new Promise<string>((_resolve, reject) => {
    rejectIdentity = reject
  })

  const pending = cacheSshProfileInventory(
    id,
    captureConnectionCacheValidity(id),
    async () => profiles,
    () => identity
  )

  const failed = assert.rejects(pending, /identity exec failed/)
  await new Promise(resolve => setImmediate(resolve))
  const cachedBeforeIdentity = sshRosterCache.get(id)
  rejectIdentity(new Error('identity exec failed'))
  await failed
  assert.deepEqual(cachedBeforeIdentity, profiles)
  assert.deepEqual(sshRosterCache.get(id), profiles)
  assert.equal(connectionInstallIds.get(id)?.id, 'known-install')
  evictConnectionCaches(id)
})

test('SSH inventory keeps optional identity and empty-list semantics', async () => {
  const id = 'ssh-legacy'
  evictConnectionCaches(id)
  const profiles = ['default', 'leo']

  await cacheSshProfileInventory(
    id,
    captureConnectionCacheValidity(id),
    async () => profiles,
    async () => undefined
  )
  assert.deepEqual(sshRosterCache.get(id), profiles)
  assert.equal(connectionInstallIds.get(id)?.id, undefined)
  assert.equal(typeof connectionInstallIds.get(id)?.ts, 'number')
  await cacheSshProfileInventory(
    id,
    captureConnectionCacheValidity(id),
    async () => [],
    async () => 'verified-install'
  )
  assert.deepEqual(sshRosterCache.get(id), profiles)
  assert.equal(connectionInstallIds.get(id)?.id, 'verified-install')
  evictConnectionCaches(id)
})

test('SSH inventory and identity cannot publish after removal or repointing during either read', async () => {
  for (const phase of ['profiles', 'identity']) {
    const id = `ssh-invalidated-${phase}`
    evictConnectionCaches(id)
    let release!: () => void

    const gate = new Promise<void>(resolve => {
      release = resolve
    })

    let identityReads = 0

    const pending = cacheSshProfileInventory(
      id,
      captureConnectionCacheValidity(id),
      async () => {
        if (phase === 'profiles') {
          await gate
        }

        return ['old-target']
      },
      async () => {
        identityReads++

        if (phase === 'identity') {
          await gate
        }

        return 'old-install'
      }
    )

    await new Promise(resolve => setImmediate(resolve))
    evictConnectionCaches(id)
    captureConnectionCacheValidity(id)
    sshRosterCache.set(id, ['new-target'])
    connectionInstallIds.set(id, { id: 'new-install', ts: 2 })
    release()
    await pending
    assert.deepEqual(sshRosterCache.get(id), ['new-target'])
    assert.equal(connectionInstallIds.get(id)?.id, 'new-install')

    if (phase === 'profiles') {
      assert.equal(identityReads, 0)
    }

    evictConnectionCaches(id)
  }
})

test('a successful profile identity is cached and an older status reply cannot overwrite it', async () => {
  evictConnectionCaches('profile-id')
  let release!: (value: unknown) => void

  const status = new Promise(resolve => {
    release = resolve
  })

  const body = { profiles: [{ name: 'default' }], install_id: 'current-install' }
  const probe = probeCachedConnectionInstallId('profile-id', () => status)

  const pending = fetchRosterSourceData(
    async () => body,
    () => probe,
    'profile-id'
  )

  const result = await Promise.race([pending, new Promise(resolve => setTimeout(() => resolve('blocked'), 20))])
  release({ install_id: 'older-probe' })
  await probe

  assert.deepEqual(result, { body, installId: 'current-install' })
  assert.equal(connectionInstallIds.get('profile-id')?.id, 'current-install')
  evictConnectionCaches('profile-id')
})

test('a profile response arriving after retargeting cannot repopulate the old identity', async () => {
  evictConnectionCaches('moving-profiles')
  let release!: (value: unknown) => void

  const profiles = new Promise(resolve => {
    release = resolve
  })

  const pending = fetchRosterSourceData(
    () => profiles,
    async () => undefined,
    'moving-profiles'
  )

  evictConnectionCaches('moving-profiles')
  release({ profiles: [{ name: 'default' }], install_id: 'old-install' })

  await assert.rejects(pending, /connection changed/)
  assert.equal(connectionInstallIds.has('moving-profiles'), false)
})

test('profile identity returns before an unresolved status read', async () => {
  const body = { profiles: [{ name: 'default' }], install_id: ' install-profile ' }

  const pending = fetchRosterSourceData(
    async () => body,
    () => new Promise(() => {})
  )

  const result = await Promise.race([pending, new Promise(resolve => setTimeout(() => resolve('blocked'), 20))])

  assert.deepEqual(result, { body, installId: 'install-profile' })
})

test('profile identity survives a rejected status read', async () => {
  const body = { profiles: [{ name: 'default' }], install_id: 'install-profile' }

  assert.deepEqual(
    await fetchRosterSourceData(
      async () => body,
      async () => {
        throw new Error('status down')
      }
    ),
    { body, installId: 'install-profile' }
  )
})

test('a status rejection after profile identity returned is handled', async () => {
  let reject!: (error: Error) => void

  const status = new Promise<string | undefined>((_resolve, fail) => {
    reject = fail
  })

  const pending = fetchRosterSourceData(
    async () => ({ install_id: 'profile-id' }),
    () => status
  )

  const result = await Promise.race([pending, new Promise(resolve => setImmediate(() => resolve('blocked')))])

  reject(new Error('late status failure'))
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(result, { body: { install_id: 'profile-id' }, installId: 'profile-id' })
})

test('an invalid profile identity uses the concurrently-started legacy fallback', async () => {
  for (const install_id of ['', '   ', 123, null]) {
    const result = await fetchRosterSourceData(
      async () => ({ profiles: [], install_id }),
      async () => 'status-id'
    )

    assert.equal(result.installId, 'status-id')
  }
})

test('legacy profiles still succeed when the concurrent status read rejects', async () => {
  const body = { profiles: [{ name: 'default' }] }

  assert.deepEqual(
    await fetchRosterSourceData(
      async () => body,
      async () => {
        throw new Error('status down')
      }
    ),
    { body, installId: undefined }
  )
})

test('roster source starts profile and install-id reads together and preserves both results', async () => {
  let releaseProfiles!: (value: { profiles: Array<{ name: string }> }) => void
  let releaseInstallId!: (value: string | undefined) => void

  const profiles = new Promise<{ profiles: Array<{ name: string }> }>(resolve => {
    releaseProfiles = resolve
  })

  const installId = new Promise<string | undefined>(resolve => {
    releaseInstallId = resolve
  })

  const started: string[] = []

  const pending = fetchRosterSourceData(
    () => {
      started.push('profiles')

      return profiles
    },
    () => {
      started.push('install-id')

      return installId
    }
  )

  assert.deepEqual(started, ['profiles', 'install-id'])
  releaseInstallId('install-1')
  releaseProfiles({ profiles: [{ name: 'default' }] })
  assert.deepEqual(await pending, {
    body: { profiles: [{ name: 'default' }] },
    installId: 'install-1'
  })
})

test('roster source preserves a profile-read failure after starting the install-id read', async () => {
  const profilesError = new Error('profiles unavailable')
  let installIdStarted = false

  await assert.rejects(
    fetchRosterSourceData(
      async () => {
        throw profilesError
      },
      async () => {
        installIdStarted = true

        return undefined
      }
    ),
    profilesError
  )
  assert.equal(installIdStarted, true)
})
