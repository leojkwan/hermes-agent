import type { GatewaySource, RosterRow, UnionRosterAgent } from './types'

export type UnionAgentRow = Partial<UnionRosterAgent>

/** Project only the active member's routing fields. The active rich row still
 * owns its capabilities, permission metadata and session preview. */
export function activeRosterAgent(agent: UnionAgentRow, activeId: string, row?: RosterRow): UnionAgentRow {
  if (agent.connectionId === activeId || agent.reachable === false || row?.unserved) {
    return agent
  }

  const member = agent.equivalentConnections?.find(candidate => candidate.connectionId === activeId)
  const target = row?.route?.targetProfile || row?.targetProfile || agent.profile

  return member && member.targetProfile === target ? { ...agent, ...member } : agent
}

/** A failed aggregate read is not new identity evidence. Reuse only an exact
 * active route's already-proven membership, without copying its rich data. */
export function rememberedActiveRosterIdentity(
  profile: RosterRow,
  activeId: string,
  previous: RosterRow[],
  installId?: string
): RosterRow {
  if (profile.unserved) {
    return profile
  }

  const target = profile.route?.targetProfile || profile.targetProfile || profile.name

  const prior = previous.find(
    row =>
      row.name === profile.name &&
      !row.unserved &&
      !(installId && row.installId && installId !== row.installId) &&
      row.equivalentConnections?.some(member => member.connectionId === activeId && member.targetProfile === target)
  )

  const member = prior?.equivalentConnections?.find(candidate => candidate.connectionId === activeId)

  if (!prior || !member) {
    return profile
  }

  return {
    ...member,
    handle: prior.handle,
    route: {
      connectionId: activeId,
      mode: member.connectionKind === 'local' ? 'local' : 'remote',
      profile: profile.name,
      targetProfile: target
    },
    sourceScoped: true,
    equivalentConnections: prior.equivalentConnections,
    ...profile
  }
}

/** Membership suppresses retained duplicates only for served rows. Local
 * storage and dispatch still use the displayed row's route-qualified key. */
export function rosterPresentationKeys(row: RosterRow): string[] {
  const own = `${row.connectionId || ''}::${row.name}`

  if (row.unserved) {
    return [own]
  }

  return [own, ...(row.equivalentConnections || []).map(member => `${member.connectionId}::${row.name}`)]
}

/** Preserve unavailable routes without resurrecting an already represented alias. */
export function retainRosterRows(
  profiles: RosterRow[],
  agents: UnionAgentRow[],
  sources: GatewaySource[] | undefined,
  previous: RosterRow[]
): void {
  if (Array.isArray(previous) && previous.length > 0) {
    const present = new Set(profiles.flatMap(rosterPresentationKeys))
    const unionSourceIds = new Set(agents.map(agent => String(agent?.connectionId || '').trim()).filter(Boolean))

    const omitted = new Set(
      (Array.isArray(sources) ? sources : [])
        .filter(source => source?.error === 'connect-on-demand' || source?.reachable === false)
        .map(source => String(source.connectionId || '').trim())
        .filter(Boolean)
    )

    const registered = new Set(
      (Array.isArray(sources) ? sources : []).map(source => String(source?.connectionId || '').trim()).filter(Boolean)
    )

    // Prefer proven membership over stale thin aliases, independent of paint order.
    const candidates = previous.toSorted(
      (a, b) => (b.equivalentConnections?.length || 0) - (a.equivalentConnections?.length || 0)
    )

    for (const row of candidates) {
      const connectionId = String(row?.connectionId || '').trim()
      const name = String(row?.name || '').trim()
      const key = `${connectionId}::${name || 'default'}`

      if (!row?.remoteSource || !connectionId || !name || present.has(key)) {
        continue
      }

      if (registered.size > 0 && !registered.has(connectionId)) {
        continue
      }

      if (omitted.has(connectionId) || !unionSourceIds.has(connectionId)) {
        profiles.push({
          ...row,
          remoteSource: true,
          retained: true,
          sourceScoped: true
        })
        rosterPresentationKeys(row).forEach(key => present.add(key))
      }
    }
  }
}
