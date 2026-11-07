import { beforeEach, describe, expect, it, vi } from 'vitest'
import { buildAgentRoster } from '../electron/connection-registry'
import { mergeMultiSourceRoster } from '../src/plugins/hermes-bots/data'
import { rosterGatewayOptions, rosterGatewaySections } from '../src/plugins/hermes-bots/roster-sections'
import { botConnectionRoute, botRouteKey, requestForBot } from '../src/plugins/hermes-bots/routing'

const { hostMock } = vi.hoisted(() => ({ hostMock: { requestProfile: vi.fn() } }))
vi.mock('@hermes/plugin-sdk', async () => {
  const { atom } = await import('nanostores')
  return { atom, host: hostMock }
})
vi.mock('../src/plugins/hermes-bots/shared', () => ({ getPluginCtx: () => null, ID: 'hermes-bots' }))
beforeEach(() => vi.clearAllMocks())

describe('real producer to renderer alias collapse', () => {
  const a = { id: 'a', kind: 'remote', label: 'A' }
  const b = { id: 'b', kind: 'remote', label: 'B' }
  const rich = {
    profiles: [{ name: 'default', canonical_session: { id: 'chat-b' }, ui_meta: { permissions: 'B only' } }]
  }
  const sources = [a, b].map(connection => ({ connectionId: connection.id, kind: connection.kind, reachable: true }))
  const produce = enumerations => ({
    agents: buildAgentRoster(enumerations, { primaryConnectionId: 'a' }),
    primaryConnectionId: 'a',
    sources: sources.map(source => ({ ...source }))
  })
  const sameInstall = () =>
    produce([a, b].map(connection => ({ connection, profiles: ['default'], installId: 'same' })))

  it('shows one Bot/group with primary A and active B, dispatching through B', async () => {
    const union = sameInstall()
    expect(union.agents).toHaveLength(1)
    expect(union.agents[0].connectionId).toBe('a')
    const rows = mergeMultiSourceRoster(rich, union, 'b').profiles

    expect(rows).toHaveLength(1)
    const options = rosterGatewayOptions(union.sources, rows)
    expect(options.map(option => option.connectionId)).toEqual(['a', 'b'])
    const groups = rosterGatewaySections(rows, options)
    expect(groups.sections).toHaveLength(1)
    expect(groups.sections[0].id).toBe('b')
    expect(rows[0]).toMatchObject({
      connectionId: 'b',
      connectionLabel: 'B',
      canonical_session: { id: 'chat-b' },
      ui_meta: { permissions: 'B only' }
    })
    expect(botConnectionRoute(rows[0])).toEqual({
      connectionId: 'b',
      mode: 'remote',
      profile: 'default',
      targetProfile: 'default'
    })
    expect(botRouteKey(botConnectionRoute(rows[0]))).toBe('b::default')
    await requestForBot(rows[0], 'session.list', { title: 'Bot Chat' })
    expect(hostMock.requestProfile).toHaveBeenCalledWith(
      expect.objectContaining({ connectionId: 'b' }),
      'session.list',
      { title: 'Bot Chat' }
    )
  })

  it('does not retain a stale alias beside the active rich row after one source fails', () => {
    const previous = [
      { name: 'default', connectionId: 'b', connectionKind: 'remote', remoteSource: true, sourceScoped: true }
    ]
    const union = sameInstall()
    union.sources[1].reachable = false
    const rows = mergeMultiSourceRoster(rich, union, 'a', previous).profiles

    expect(rows).toHaveLength(1)
    expect(rows[0].connectionId).toBe('a')
    const failed = mergeMultiSourceRoster(rich, null, 'a', [...rows, ...previous]).profiles
    expect(failed).toHaveLength(1)
    expect(botConnectionRoute(failed[0])?.connectionId).toBe('a')
  })

  it('keeps the selected alias route through an aggregate failure and an explicit alias switch', () => {
    const previous = mergeMultiSourceRoster(rich, sameInstall(), 'b').profiles
    const failedB = mergeMultiSourceRoster(rich, null, 'b', previous).profiles
    const switchedA = mergeMultiSourceRoster(rich, null, 'a', previous).profiles

    expect(failedB).toHaveLength(1)
    expect(botConnectionRoute(failedB[0])?.connectionId).toBe('b')
    expect(switchedA).toHaveLength(1)
    expect(botConnectionRoute(switchedA[0])?.connectionId).toBe('a')
  })

  it.each([false, true])('retains one previously collapsed remote group with reversed history=%s', reverse => {
    const previous = mergeMultiSourceRoster(rich, sameInstall(), 'b').profiles.map(row => ({
      ...row,
      remoteSource: true
    }))
    previous.push({ name: 'default', connectionId: 'a', remoteSource: true })
    if (reverse) {
      previous.reverse()
    }
    const rows = mergeMultiSourceRoster(rich, null, 'other', previous).profiles

    expect(rows).toHaveLength(2)
    expect(rows.filter(row => row.remoteSource)).toHaveLength(1)
  })

  it('does not reuse historic equivalence after a new install or target profile answers', () => {
    const previous = mergeMultiSourceRoster(rich, sameInstall(), 'b').profiles.map(row => ({
      ...row,
      installId: 'same',
      remoteSource: true
    }))
    const newInstall = mergeMultiSourceRoster({ ...rich, install_id: 'replacement' }, null, 'a', previous).profiles
    const newTarget = mergeMultiSourceRoster(
      { profiles: [{ name: 'default', targetProfile: 'other' }] },
      null,
      'a',
      previous
    ).profiles

    expect(newInstall).toHaveLength(2)
    expect(newTarget).toHaveLength(2)
  })

  it('keeps a rich metadata-only row separate without using it as equivalence evidence', () => {
    const rows = mergeMultiSourceRoster(
      { profiles: [{ name: 'default', unserved: true }] },
      sameInstall(),
      'b'
    ).profiles

    expect(rows).toHaveLength(2)
    expect(rows.find(row => row.connectionId === 'a')?.remoteSource).toBe(true)
  })

  it.each([
    ['different installs', 'one', 'two', undefined],
    ['missing identity', undefined, undefined, undefined],
    ['one missing identity', 'one', undefined, undefined],
    ['different target profile', 'same', 'same', 'research']
  ])('keeps %s separate', (_label, idA, idB, target) => {
    const union = produce([
      { connection: a, profiles: ['default'], installId: idA },
      { connection: { ...b, remoteProfile: target }, profiles: ['default'], installId: idB }
    ])
    const rows = mergeMultiSourceRoster(rich, union, 'b').profiles

    expect(rows).toHaveLength(2)
    expect(new Set(rows.map(row => row.connectionId))).toEqual(new Set(['a', 'b']))
  })

  it('keeps an unserved metadata-only union row separate from the active rich row', () => {
    const union = sameInstall()
    const rows = mergeMultiSourceRoster(
      rich,
      { ...union, agents: union.agents.map(agent => ({ ...agent, reachable: false })) },
      'b'
    ).profiles

    expect(rows).toHaveLength(2)
    expect(rows.find(row => row.connectionId === 'a')?.unserved).toBe(true)
  })
})
