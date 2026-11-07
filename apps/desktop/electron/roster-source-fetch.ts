import { captureConnectionCacheValidity, connectionInstallIds, sshRosterCache } from './connection-caches'
import { installIdFromStatus, rememberConnectionInstallId } from './registry-backend-identity'

export async function cacheSshProfileInventory(
  connectionId: string,
  isCurrent: () => boolean,
  fetchProfiles: () => Promise<string[]>,
  fetchInstallId: () => Promise<string | undefined>
): Promise<void> {
  const profiles = await fetchProfiles()

  if (!isCurrent()) {
    return
  }

  if (profiles.length > 0) {
    sshRosterCache.set(connectionId, profiles)
  }

  // Identity is optional: a later transport failure must not discard fresh profiles.
  const installId = await fetchInstallId()

  if (!isCurrent()) {
    return
  }

  connectionInstallIds.set(connectionId, { id: installId, ts: Date.now() })
}

export function assertRosterConnectionCurrent(isCurrent: () => boolean): void {
  if (!isCurrent()) {
    throw new Error('roster connection changed during enumeration')
  }
}

export async function fetchRosterSourceData<T>(
  fetchProfiles: () => Promise<T>,
  fetchInstallId: () => Promise<string | undefined>,
  connectionId?: string
): Promise<{ body: T; installId: string | undefined }> {
  const isCurrent = connectionId ? captureConnectionCacheValidity(connectionId) : () => true
  const profiles = fetchProfiles()
  // Start legacy fallback concurrently, and own its rejection even when the
  // profile body supplies identity before the status read has settled.
  const fallback = fetchInstallId().catch(() => undefined)
  const body = await profiles
  const profileId = installIdFromStatus(body)

  assertRosterConnectionCurrent(isCurrent)

  if (profileId && connectionId) {
    rememberConnectionInstallId(connectionId, body)
  }

  const installId = profileId || (await fallback)

  assertRosterConnectionCurrent(isCurrent)

  return { body, installId }
}
