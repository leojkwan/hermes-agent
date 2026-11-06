import type * as HermesSdk from '@hermes/plugin-sdk'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { createPluginContext } from '@/contrib/plugin'
import { botAppearance } from '@/plugins/hermes-bots/avatar'
import { $botMeta, migrateBotMeta, saveBotMeta, setLocalBotMeta } from '@/plugins/hermes-bots/data'
import { editedLook } from '@/plugins/hermes-bots/edit-profile-look'
import { isBotPinned } from '@/plugins/hermes-bots/hidden-bots'
import { $hiddenRooms, HIDDEN_ROOMS_KEY, isRoomHidden, setRoomHidden } from '@/plugins/hermes-bots/hidden-rooms'
import { displayName } from '@/plugins/hermes-bots/labels'
import { mergeServerMeta } from '@/plugins/hermes-bots/profile-ops'
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
  $hiddenRooms.set({})
  setPluginCtx(context)
  indexAliasRoutes([alias])
  vi.clearAllMocks()
})

afterEach(() => {
  vi.restoreAllMocks()
  $botMeta.set({})
  $hiddenRooms.set({})
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
      const appearance = $botMeta.get()
      await setRoomHidden(binding, hidden)
      expect($botMeta.get()).toBe(appearance)
      expect(isRoomHidden(binding)).toBe(hidden)
      expectAliasLook()

      $botMeta.set({})
      $hiddenRooms.set({})
      indexAliasRoutes([])
      await migrateBotMeta(context.storage)
      indexAliasRoutes([alias])
      expect(isRoomHidden(binding)).toBe(hidden)
      expectAliasLook()
    }

    expect(request).not.toHaveBeenCalled()
    expect(requestProfile).not.toHaveBeenCalled()
  }
)

it('keeps Edit Profile Match the name color clear through Hide/Restore and reload', async () => {
  context.storage.set('bot-meta-v2', { 'cloud-abc::moxie': look })
  context.storage.set('bot-meta-v2-migrated', true)
  await migrateBotMeta(context.storage)
  requestProfile.mockImplementation(async (_route: unknown, method: string) =>
    method === 'profiles.list' ? { profiles: [{ name: 'default' }] } : { applied: { ui_meta: true } }
  )
  // AvatarPicker's "Match the name" supplies null to Edit Profile's submit path.
  const opened = { ...botAppearance(bot.name, look), title: look.title ?? '' }
  const patch = editedLook(opened, { ...opened, color: null })

  if (!patch) {
    throw new Error('Edit Profile did not produce its color-clear patch')
  }

  await saveBotMeta(bot, patch)

  const expectNameColor = () => {
    const meta = botRosterMeta(bot, $botMeta.get())
    expect(botAppearance(bot.name, meta).color).toBe(botAppearance(bot.name, { custom: true }).color)
    expect(botAppearance(bot.name, meta).color).not.toBe(look.color)
  }

  expectNameColor()

  for (const hidden of [true, false]) {
    await setRoomHidden(binding, hidden)
    expectNameColor()
    $botMeta.set({})
    $hiddenRooms.set({})
    await migrateBotMeta(context.storage)
    expectNameColor()
    expect(isRoomHidden(binding)).toBe(hidden)
  }
})

it('keeps alias and explicit user titles through thin refresh, Hide/Restore and reload', async () => {
  context.storage.set('bot-meta-v2', { 'cloud-abc::moxie': look })
  context.storage.set('bot-meta-v2-migrated', true)
  await migrateBotMeta(context.storage)
  const thin = { ...bot, title: 'Backend heading' }

  const refresh = async () => {
    mergeServerMeta([thin])
    await vi.waitFor(() => expect(context.storage.get('bot-meta-v2', {})).toEqual($botMeta.get()))
  }

  const expectTitle = (title: string) => expect(displayName(thin, botRosterMeta(thin, $botMeta.get()))).toBe(title)
  await refresh()
  expectTitle('Moxie Archive Guide')

  for (const hidden of [true, false]) {
    await setRoomHidden(binding, hidden)
    await refresh()
    expectTitle('Moxie Archive Guide')
    $botMeta.set({})
    $hiddenRooms.set({})
    await migrateBotMeta(context.storage)
    await refresh()
    expectTitle('Moxie Archive Guide')
  }

  requestProfile.mockImplementation(async (_route: unknown, method: string) =>
    method === 'profiles.list' ? { profiles: [{ name: 'default' }] } : { applied: { ui_meta: true } }
  )
  await saveBotMeta(bot, { title: 'User title' })
  await refresh()
  expectTitle('User title')
  $botMeta.set({})
  $hiddenRooms.set({})
  await migrateBotMeta(context.storage)
  await refresh()
  expectTitle('User title')

  // An authoritative rich response can still clear that user override.
  mergeServerMeta([{ ...thin, ui_meta_revisions: { 'hermes-bots': 2 } }])
  await vi.waitFor(() => expect(context.storage.get('bot-meta-v2', {})).toEqual($botMeta.get()))
  expectTitle('Moxie Archive Guide')
})

it('migrates legacy room hides without losing appearance, isolation or an explicit Restore on retry', async () => {
  const legacy = {
    'cloud-abc::moxie': look,
    'cloud-abc::default': { hiddenRooms: ['room-1', 'room-2'], title: 'User title' },
    'other::default': { hiddenRooms: ['room-1'] },
    'cloud-abc::work': { hiddenRooms: ['room-3'] }
  }

  context.storage.set('bot-meta-v2', legacy)
  context.storage.set('bot-meta-v2-migrated', true)
  await migrateBotMeta(context.storage)
  expect($botMeta.get()).toEqual({ 'cloud-abc::moxie': look, 'cloud-abc::default': { title: 'User title' } })
  expect(context.storage.get('bot-meta-v2', {})).toEqual($botMeta.get())
  expect(context.storage.get(HIDDEN_ROOMS_KEY, {})).toEqual({
    'cloud-abc::default': { 'room-1': true, 'room-2': true },
    'other::default': { 'room-1': true },
    'cloud-abc::work': { 'room-3': true }
  })
  expect(displayName(bot, botRosterMeta(bot, $botMeta.get()))).toBe('User title')
  await setRoomHidden(binding, false)

  // A crash/retry retaining old metadata must not resurrect a restored room.
  context.storage.set('bot-meta-v2', legacy)
  $botMeta.set({})
  $hiddenRooms.set({})
  await migrateBotMeta(context.storage)
  expect(isRoomHidden(binding)).toBe(false)
  expect(isRoomHidden({ ...binding, roomId: 'room-2' })).toBe(true)
  expect(isRoomHidden({ ...binding, connectionId: 'other' })).toBe(true)
  expect(isRoomHidden({ ...binding, profile: 'work', roomId: 'room-3' })).toBe(true)
  expect($botMeta.get()).toEqual({ 'cloud-abc::moxie': look, 'cloud-abc::default': { title: 'User title' } })
})

it('retains legacy hides when the separate store cannot commit, then migrates on retry', async () => {
  const legacy = { 'cloud-abc::default': { hiddenRooms: ['room-1'] }, 'cloud-abc::moxie': look }
  context.storage.set('bot-meta-v2', legacy)
  context.storage.set('bot-meta-v2-migrated', true)
  const write = context.storage.set

  const failing = vi.spyOn(context.storage, 'set').mockImplementation((key, value) => {
    if (key === HIDDEN_ROOMS_KEY) {
      throw new Error('disk full')
    }

    return write(key, value)
  })

  await migrateBotMeta(context.storage)
  expect(context.storage.get('bot-meta-v2', {})).toEqual(legacy)
  expect(isRoomHidden(binding)).toBe(true)
  failing.mockRestore()
  $botMeta.set({})
  $hiddenRooms.set({})
  await migrateBotMeta(context.storage)
  expect(isRoomHidden(binding)).toBe(true)
  expect($botMeta.get()).toEqual({ 'cloud-abc::moxie': look })
  expect(context.storage.get(HIDDEN_ROOMS_KEY, {})).toEqual({ 'cloud-abc::default': { 'room-1': true } })
})

it('preserves a Bot edit while legacy hide migration is waiting for storage', async () => {
  context.storage.set('bot-meta-v2', {
    'cloud-abc::default': { hiddenRooms: ['room-1'] },
    'cloud-abc::moxie': look
  })
  context.storage.set('bot-meta-v2-migrated', true)
  const gate = Promise.withResolvers<void>()
  const write = context.storage.set
  vi.spyOn(context.storage, 'set').mockImplementation((key, value) => {
    if (key === HIDDEN_ROOMS_KEY) {
      return gate.promise.then(() => write(key, value))
    }

    return write(key, value)
  })
  const migrating = migrateBotMeta(context.storage)
  await vi.waitFor(() => expect(isRoomHidden(binding)).toBe(true))
  await setLocalBotMeta(bot, { title: 'Newer user edit' })
  gate.resolve()
  await migrating
  expect(displayName(bot, botRosterMeta(bot, $botMeta.get()))).toBe('Newer user edit')
  expect(context.storage.get('bot-meta-v2', {})).toEqual({
    'cloud-abc::default': { title: 'Newer user edit' },
    'cloud-abc::moxie': look
  })
})
