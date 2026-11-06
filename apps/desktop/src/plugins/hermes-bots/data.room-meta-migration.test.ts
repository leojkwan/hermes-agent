import { beforeEach, expect, it, vi } from 'vitest'

vi.mock('@hermes/plugin-sdk', async () => {
  const { atom } = await import('nanostores')

  return {
    atom,
    host: { agents: async () => ({ agents: [], sources: [] }) },
    queryClient: undefined,
    useQuery: vi.fn(),
    useValue: vi.fn()
  }
})

vi.mock('./shared', () => ({ getPluginCtx: () => null, ID: 'hermes-bots' }))

import { $botMeta, migrateBotMeta } from './data'
import { $hiddenRooms, HIDDEN_ROOMS_KEY, isRoomHidden } from './hidden-rooms'

beforeEach(() => {
  $botMeta.set({})
  $hiddenRooms.set({})
})

it.each(['bot-meta', 'bot-meta-v2'])(
  'retains legacy hides after a silent store failure and migrates on retry (%s)',
  async metaKey => {
    const binding = { connectionId: 'cloud-abc', profile: 'default', roomId: 'room-1' }

    const legacy = {
      'cloud-abc::default': { hiddenRooms: ['room-1', 'room-2', 'restored'], title: 'User title' },
      'other::default': { hiddenRooms: ['room-3'] }
    }

    const saved = { 'cloud-abc::default': { 'room-1': true, restored: false } }

    const values = new Map<string, unknown>([
      [metaKey, legacy],
      ['bot-meta-v2-migrated', metaKey === 'bot-meta-v2'],
      [HIDDEN_ROOMS_KEY, saved]
    ])

    let dropHiddenRoomWrites = true

    const storage = {
      get: (key: string, fallback?: unknown) => values.get(key) ?? fallback,
      remove: (key: string) => values.delete(key),
      set: (key: string, value: unknown) => {
        if (key === HIDDEN_ROOMS_KEY && dropHiddenRoomWrites) {
          return
        }

        values.set(key, value)
      }
    }

    const expectVisibility = () => {
      expect(isRoomHidden(binding)).toBe(true)
      expect(isRoomHidden({ ...binding, roomId: 'room-2' })).toBe(true)
      expect(isRoomHidden({ ...binding, roomId: 'restored' })).toBe(false)
      expect(isRoomHidden({ ...binding, connectionId: 'other', roomId: 'room-3' })).toBe(true)
    }

    await migrateBotMeta(storage)
    expectVisibility()
    expect(storage.get(HIDDEN_ROOMS_KEY)).toEqual(saved)
    expect(storage.get(metaKey)).toEqual(legacy)
    expect($botMeta.get()).toEqual(legacy)

    dropHiddenRoomWrites = false
    $botMeta.set({})
    $hiddenRooms.set({})
    await migrateBotMeta(storage)
    expectVisibility()
    expect(storage.get(HIDDEN_ROOMS_KEY)).toEqual({
      'cloud-abc::default': { 'room-1': true, 'room-2': true, restored: false },
      'other::default': { 'room-3': true }
    })
    expect(storage.get(metaKey)).toEqual({ 'cloud-abc::default': { title: 'User title' } })
    expect($botMeta.get()).toEqual({ 'cloud-abc::default': { title: 'User title' } })
  }
)
