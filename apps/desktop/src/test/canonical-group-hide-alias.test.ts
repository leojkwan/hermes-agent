import type * as HermesSdk from '@hermes/plugin-sdk'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { createPluginContext } from '@/contrib/plugin'
import { botAppearance } from '@/plugins/hermes-bots/avatar'
import { $botMeta, migrateBotMeta, setLocalRoomHidden } from '@/plugins/hermes-bots/data'
import { isBotPinned, isRoomHidden } from '@/plugins/hermes-bots/hidden-bots'
import { displayName } from '@/plugins/hermes-bots/labels'
import { botRosterMeta, indexAliasRoutes } from '@/plugins/hermes-bots/routing'
import { setPluginCtx } from '@/plugins/hermes-bots/shared'
import type { BotMeta, ProfileRoute, RosterRow } from '@/plugins/hermes-bots/types'

const { request, requestProfile } = vi.hoisted(() => ({ request: vi.fn(), requestProfile: vi.fn() }))

vi.mock('@hermes/plugin-sdk', async importOriginal => {
  const sdk = await importOriginal<typeof HermesSdk>()

  return { ...sdk, host: { ...sdk.host, request, requestProfile } }
})

const alias: ProfileRoute = {
  connectionId: 'cloud-abc',
  mode: 'remote',
  profile: 'moxie',
  targetProfile: 'default'
}

const bot: RosterRow = {
  name: 'default',
  connectionId: alias.connectionId,
  sourceScoped: true,
  remoteSource: true,
  route: { ...alias, profile: 'default' },
  targetProfile: 'default'
}

const binding = { connectionId: alias.connectionId, profile: 'default', roomId: 'room-1' }

const look: BotMeta = {
  title: 'Moxie Archive Guide',
  image: 'data:image/png;base64,AA==',
  custom: true,
  shape: 'circle',
  color: '#cd8800',
  pinned: true
}

const context = createPluginContext('hermes-bots')

beforeEach(() => {
  localStorage.clear()
  $botMeta.set({})
  setPluginCtx(context)
  indexAliasRoutes([alias])
  vi.clearAllMocks()
})

afterEach(() => {
  $botMeta.set({})
  indexAliasRoutes([])
  setPluginCtx(null)
  localStorage.clear()
})

it.each(['cloud-abc::moxie', 'moxie'])(
  'keeps alias title, avatar and pin through room Hide/Restore and reload (%s)',
  async aliasKey => {
    context.storage.set('bot-meta-v2', { [aliasKey]: look })
    context.storage.set('bot-meta-v2-migrated', true)
    await migrateBotMeta(context.storage)

    const expectAliasLook = () => {
      const meta = botRosterMeta(bot, $botMeta.get())
      expect(displayName(bot, meta)).toBe(look.title)
      expect(botAppearance(bot.name, meta)).toEqual({ image: look.image, shape: look.shape, color: look.color })
      expect(isBotPinned(bot, $botMeta.get())).toBe(true)
      expect($botMeta.get()[aliasKey]).toEqual(look)
    }

    expectAliasLook()

    for (const hidden of [true, false]) {
      await setLocalRoomHidden(binding, hidden)
      expect(isRoomHidden(binding, $botMeta.get())).toBe(hidden)
      expectAliasLook()

      $botMeta.set({})
      indexAliasRoutes([])
      await migrateBotMeta(context.storage)
      indexAliasRoutes([alias])
      expect(isRoomHidden(binding, $botMeta.get())).toBe(hidden)
      expectAliasLook()
    }

    expect(request).not.toHaveBeenCalled()
    expect(requestProfile).not.toHaveBeenCalled()
  }
)
