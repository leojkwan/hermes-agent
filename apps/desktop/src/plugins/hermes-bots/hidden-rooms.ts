import { atom } from '@hermes/plugin-sdk'

import type { CanonicalGroupBinding } from './canonical-groups'
import type { BotMetaSnapshot } from './data'
import { botRouteKey } from './routing'
import { getPluginCtx } from './shared'

export const HIDDEN_ROOMS_KEY = 'hidden-group-rooms-v1'
// A stored false is an explicit Restore, including while retrying a legacy migration.
export type HiddenRooms = Record<string, Record<string, boolean>>
export const $hiddenRooms = atom<HiddenRooms>({})

interface RoomStorage {
  get?: (key: string, fallback?: unknown) => unknown
  set: (key: string, value: unknown) => unknown
}

function decodeHiddenRooms(value: unknown): HiddenRooms {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return {}
  }

  return Object.fromEntries(
    Object.entries(value).flatMap(([scope, rooms]) => {
      if (!rooms || typeof rooms !== 'object' || Array.isArray(rooms)) {
        return []
      }

      return [
        [
          scope,
          Object.fromEntries(
            Object.entries(rooms).filter((entry): entry is [string, boolean] => typeof entry[1] === 'boolean')
          )
        ]
      ]
    })
  )
}

function mergeHiddenRooms(older: HiddenRooms, newer: HiddenRooms): HiddenRooms {
  return Object.fromEntries(
    [...new Set([...Object.keys(older), ...Object.keys(newer)])].map(scope => [
      scope,
      { ...older[scope], ...newer[scope] }
    ])
  )
}

let pendingWrite = Promise.resolve()

function persistHiddenRooms(storage: RoomStorage | undefined, rooms: HiddenRooms): Promise<void> {
  const write = pendingWrite.then(async () => {
    if (!storage) {
      throw new Error('Room visibility storage is unavailable')
    }

    await storage.set(HIDDEN_ROOMS_KEY, rooms)

    // Plugin storage silently ignores quota/permission failures; only readback proves the copy.
    const saved = decodeHiddenRooms(await storage.get?.(HIDDEN_ROOMS_KEY, {}))

    if (
      !Object.entries(rooms).every(([scope, entries]) =>
        Object.entries(entries).every(([id, hidden]) => saved[scope]?.[id] === hidden)
      )
    ) {
      throw new Error('Room visibility could not be persisted')
    }
  })

  pendingWrite = write.catch(() => undefined)

  return write
}

/** Load presentation state and durably copy legacy hides before appearance cleanup. */
export async function loadHiddenRooms(
  storage: RoomStorage | undefined = getPluginCtx()?.storage,
  legacy: BotMetaSnapshot = {}
): Promise<void> {
  const saved = decodeHiddenRooms(await storage?.get?.(HIDDEN_ROOMS_KEY, {}))

  const migrated = Object.fromEntries(
    Object.entries(legacy).flatMap(([scope, meta]) => {
      const rooms = meta?.hiddenRooms

      if (!scope.includes('::') || !Array.isArray(rooms)) {
        return []
      }

      return [[scope, Object.fromEntries(rooms.filter(id => typeof id === 'string').map(id => [id, true]))]]
    })
  )

  // Existing explicit choices beat old metadata, and gestures during hydration beat disk.
  const next = mergeHiddenRooms(mergeHiddenRooms(migrated, saved), $hiddenRooms.get())
  $hiddenRooms.set(next)

  if (Object.keys(migrated).length) {
    await persistHiddenRooms(storage, next)
  }
}

export function isRoomHidden(binding: CanonicalGroupBinding, rooms = $hiddenRooms.get()): boolean {
  return rooms[botRouteKey(binding)]?.[binding.roomId] === true
}

/** Hide/Restore never reads or writes a Bot appearance record or gateway room. */
export async function setRoomHidden(binding: CanonicalGroupBinding, hidden: boolean): Promise<void> {
  const scope = botRouteKey(binding)
  $hiddenRooms.set({
    ...$hiddenRooms.get(),
    [scope]: { ...$hiddenRooms.get()[scope], [binding.roomId]: hidden }
  })

  try {
    const storage = getPluginCtx()?.storage
    await loadHiddenRooms(storage)
    await persistHiddenRooms(storage, $hiddenRooms.get())
  } catch {
    // Unavailable local storage keeps this presentation choice for the window.
  }
}
