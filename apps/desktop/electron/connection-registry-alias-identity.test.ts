import assert from 'node:assert/strict'

import { test } from 'vitest'

import { connectionInstallIds, evictConnectionCaches, sshRosterCache } from './connection-caches'
import { buildAgentRoster, rememberSshEnumeration } from './connection-registry'
import { fetchRosterSourceData } from './roster-source-fetch'

test('roster: remembered profiles keep verified identity during failure until connection eviction', async () => {
  const a = { id: 'a', kind: 'remote' as const, label: 'A' }
  const b = { id: 'b', kind: 'remote' as const, label: 'B' }
  const failed = { profiles: null, error: 'temporarily down' }
  evictConnectionCaches('b')

  const { body } = await fetchRosterSourceData(
    async () => ({ profiles: ['default'], install_id: 'same-install' }),
    async () => undefined,
    'b'
  )

  sshRosterCache.set('b', body.profiles)

  const remembered = rememberSshEnumeration(
    failed,
    sshRosterCache.get('b'),
    'remote',
    connectionInstallIds.get('b')?.id
  )

  const roster = buildAgentRoster([
    { connection: a, profiles: ['default'], installId: 'same-install' },
    { connection: b, ...remembered }
  ])

  assert.equal(roster.length, 1)
  evictConnectionCaches('b')

  const retargeted = rememberSshEnumeration(
    failed,
    sshRosterCache.get('b'),
    'remote',
    connectionInstallIds.get('b')?.id
  )

  assert.deepEqual(retargeted, failed)

  const unknownTarget = buildAgentRoster([
    { connection: a, profiles: ['default'], installId: 'same-install' },
    { connection: b, profiles: ['default'], installId: connectionInstallIds.get('b')?.id }
  ])

  assert.equal(unknownTarget.length, 2)
})

test('roster: same install and displayed profile with different remote targets remain separate', () => {
  const roster = buildAgentRoster([
    { connection: { id: 'a', kind: 'remote', label: 'A' }, profiles: ['default'], installId: 'same' },
    {
      connection: { id: 'b', kind: 'remote', label: 'B', remoteProfile: 'research' },
      profiles: ['default'],
      installId: 'same'
    }
  ])

  assert.equal(roster.length, 2)
  assert.deepEqual(
    roster.map(row => row.targetProfile),
    ['default', 'research']
  )
})
