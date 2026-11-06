import type * as HermesSdk from '@hermes/plugin-sdk'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { createPluginContext } from '@/contrib/plugin'
import { ContribRender } from '@/contrib/react/boundary'
import { translateBotsIn } from '@/plugins/hermes-bots/i18n-test-helper'

const { requestProfile, openWorkspace, setConnection, setProfile, setGateway } = vi.hoisted(() => ({
  requestProfile: vi.fn(),
  openWorkspace: vi.fn<typeof HermesSdk.host.openWorkspace>(),
  setConnection: vi.fn<(id: string) => void>(),
  setProfile: vi.fn<(profile: string) => void>(),
  setGateway: vi.fn<(state: string) => void>()
}))

vi.mock('@hermes/plugin-sdk', async importOriginal => {
  const sdk = await importOriginal<typeof HermesSdk>()
  const connectionId = sdk.atom<string | null>('local')
  const profile = sdk.atom('default')
  const gateway = sdk.atom('open')
  setConnection.mockImplementation(id => connectionId.set(id))
  setProfile.mockImplementation(name => profile.set(name))
  setGateway.mockImplementation(state => gateway.set(state))

  return {
    ...sdk,
    host: {
      ...sdk.host,
      agents: vi.fn(async () => ({ agents: [], sources: [] })),
      requestProfile,
      openWorkspace,
      paneVisibility: () => sdk.atom(true),
      state: {
        ...sdk.host.state,
        connectionId,
        gateway,
        profile
      }
    },
    usePluginI18n: () => translateBotsIn('en')
  }
})

import { CANONICAL_GROUP_CAPABILITIES } from '@/plugins/hermes-bots/canonical-group-contract-test-utils'
import { $canonicalRoomLive, canonicalRoomLiveKey } from '@/plugins/hermes-bots/canonical-group-details'
import { $canonicalGroupBindings } from '@/plugins/hermes-bots/canonical-group-registry'
import { $botMeta, migrateBotMeta } from '@/plugins/hermes-bots/data'
import { $groupChatWorkspace } from '@/plugins/hermes-bots/group-chat'
import { dropGroupMainTab, recordGroupMainTab } from '@/plugins/hermes-bots/group-panes'
import { $showHiddenBots } from '@/plugins/hermes-bots/hidden-bots'
import { $hiddenRooms, HIDDEN_ROOMS_KEY, isRoomHidden } from '@/plugins/hermes-bots/hidden-rooms'
import { BotsPane } from '@/plugins/hermes-bots/roster-pane'
import { setPluginCtx } from '@/plugins/hermes-bots/shared'

const room = { room_id: 'room-1', name: 'Launch crew', members: [], updated_at: Date.now() / 1000 }
const key = 'canonical:local:default:room-1'
const context = createPluginContext('hermes-bots')

interface RoomList {
  rooms: (typeof room)[]
  next_offset: null
}

let listedRooms: (typeof room)[] = []
let pendingRoomList: Promise<RoomList> | null = null

function renderRoster() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  const view = render(
    <QueryClientProvider client={client}>
      <BotsPane />
    </QueryClientProvider>
  )

  return { ...view, client }
}

async function rowAction(action: string) {
  fireEvent.contextMenu(await screen.findByRole('button', { name: room.name }))
  fireEvent.click(await screen.findByRole('menuitem', { name: action }))
}

beforeEach(() => {
  localStorage.clear()
  $botMeta.set({})
  $hiddenRooms.set({})
  $canonicalGroupBindings.set({})
  $groupChatWorkspace.set(null)
  $showHiddenBots.set(false)
  $canonicalRoomLive.set({})
  setConnection('local')
  setProfile('default')
  setGateway('open')
  listedRooms = [room]
  pendingRoomList = null
  openWorkspace.mockReturnValue(() => undefined)
  setPluginCtx(context)
  Element.prototype.scrollIntoView = vi.fn()
  requestProfile.mockImplementation(async (_route: unknown, method: string, params: { room_id?: string } = {}) => {
    if (method === 'groups.list') {
      return pendingRoomList ?? { rooms: listedRooms, next_offset: null }
    }

    if (method === 'groups.capabilities') {
      return CANONICAL_GROUP_CAPABILITIES
    }

    if (method === 'groups.state') {
      const selected = listedRooms.find(candidate => candidate.room_id === params.room_id) ?? room

      return { room: { ...selected, latest_seq: 1 }, driver_status: {} }
    }

    if (method === 'groups.log') {
      return {
        events: [
          {
            seq: 1,
            room_id: params.room_id,
            event_id: 'old-message',
            kind: 'message.user',
            payload: {
              text: params.room_id === room.room_id ? 'Retained room history' : `History for ${params.room_id}`
            }
          }
        ]
      }
    }

    throw new Error(`Unexpected RPC: ${method}`)
  })
})

afterEach(() => {
  cleanup()
  dropGroupMainTab(key)
  dropGroupMainTab('canonical:local:default:room-old')
  dropGroupMainTab('canonical:local:default:room-new')
  setPluginCtx(null)
  localStorage.clear()
  vi.clearAllMocks()
})

it('hides and restores in the existing Hidden section without disbanding or closing an open tab', async () => {
  const close = vi.fn()
  const binding = { connectionId: 'local', profile: 'default', roomId: room.room_id, roomName: room.name }
  $canonicalGroupBindings.set({ [key]: binding })
  $groupChatWorkspace.set(key)
  recordGroupMainTab(key, close)
  renderRoster()

  const appearance = $botMeta.get()
  await rowAction('Hide')
  expect($botMeta.get()).toBe(appearance)
  await waitFor(() => expect(screen.queryByRole('button', { name: room.name })).toBeNull())
  fireEvent.click(screen.getByRole('button', { name: /Hidden/ }))
  const hiddenSection = screen.getByRole('button', { name: /Hidden/ }).parentElement!
  expect(within(hiddenSection).getByRole('button', { name: room.name })).toBeTruthy()
  await rowAction('Restore')

  expect(screen.getByRole('button', { name: room.name })).toBeTruthy()
  expect(screen.queryByRole('button', { name: /Hidden/ })).toBeNull()
  expect($canonicalGroupBindings.get()[key]).toEqual(binding)
  expect($groupChatWorkspace.get()).toBe(key)
  expect(close).not.toHaveBeenCalled()
  expect(requestProfile.mock.calls.map(([, method]) => method)).not.toContain('groups.disband')
  expect(requestProfile.mock.calls.every(([, method]) => method === 'groups.list')).toBe(true)
})

it('reloads the saved hidden state and the saved restore from existing plugin storage', async () => {
  const view = renderRoster()
  await rowAction('Hide')
  await waitFor(() => expect(context.storage.get(HIDDEN_ROOMS_KEY, {})).toEqual($hiddenRooms.get()))
  view.unmount()

  const reload = async () => {
    setPluginCtx(createPluginContext('hermes-bots'))
    $botMeta.set({})
    $hiddenRooms.set({})
    $showHiddenBots.set(false)
    await migrateBotMeta(context.storage)
  }

  await act(reload)
  const restarted = renderRoster()
  expect(await screen.findByRole('button', { name: /Hidden/ })).toBeTruthy()
  expect(screen.queryByRole('button', { name: room.name })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: /Hidden/ }))
  await rowAction('Restore')
  await waitFor(() =>
    expect(context.storage.get(HIDDEN_ROOMS_KEY, {})).toMatchObject({ 'local::default': { 'room-1': false } })
  )
  // The separate visibility state must be durable before simulating termination.
  await waitFor(() => expect(context.storage.get(HIDDEN_ROOMS_KEY, {})).toEqual($hiddenRooms.get()))
  restarted.unmount()

  await act(reload)
  renderRoster()
  expect(await screen.findByRole('button', { name: room.name })).toBeTruthy()
  expect(screen.queryByRole('button', { name: /Hidden/ })).toBeNull()
})

it('isolates the same room ID by connection and profile and leaves other room IDs visible', async () => {
  renderRoster()
  await rowAction('Hide')
  const binding = { connectionId: 'local', profile: 'default', roomId: room.room_id }
  expect(isRoomHidden(binding, $hiddenRooms.get())).toBe(true)
  expect(isRoomHidden({ ...binding, roomId: 'other-room' }, $hiddenRooms.get())).toBe(false)

  await act(async () => setConnection('remote'))
  expect(await screen.findByRole('button', { name: room.name })).toBeTruthy()
  expect(screen.queryByRole('button', { name: /Hidden/ })).toBeNull()
  await rowAction('Hide')
  await act(async () => setProfile('work'))
  expect(await screen.findByRole('button', { name: room.name })).toBeTruthy()
  expect(screen.queryByRole('button', { name: /Hidden/ })).toBeNull()

  await act(async () => {
    setConnection('local')
    setProfile('default')
  })
  expect(await screen.findByRole('button', { name: /Hidden/ })).toBeTruthy()
  expect(screen.queryByRole('button', { name: room.name })).toBeNull()
})

it('searches and opens a hidden room through the real workspace and reads its existing history', async () => {
  renderRoster()
  await rowAction('Hide')
  fireEvent.change(screen.getByRole('textbox', { name: 'Search bots and group chats' }), {
    target: { value: 'Launch' }
  })
  const row = await screen.findByRole('button', { name: room.name })
  expect(screen.getByText('Hidden')).toBeTruthy()
  fireEvent.click(row)
  const opened = openWorkspace.mock.calls.at(-1)
  expect(opened?.[1].title).toBe(room.name)
  expect($groupChatWorkspace.get()).toBe(key)

  if (!opened) {
    throw new Error('Hidden room did not open a workspace')
  }

  render(<ContribRender render={opened[1].render} />)
  expect(await within(await screen.findByRole('log')).findByText('Retained room history')).toBeTruthy()
  const log = requestProfile.mock.calls.find(([, method]) => method === 'groups.log')
  expect(log?.[0]).toMatchObject({ connectionId: 'local', profile: 'default' })
  expect(log?.[2]).toMatchObject({ room_id: room.room_id })
  expect(isRoomHidden({ connectionId: 'local', profile: 'default', roomId: room.room_id }, $hiddenRooms.get())).toBe(
    true
  )
  expect(requestProfile.mock.calls.map(([, method]) => method)).not.toContain('groups.disband')
  expect(requestProfile.mock.calls.map(([, method]) => method)).not.toContain('groups.create')
})

it('keeps the hidden row activity supplied by the open-view live cache', async () => {
  renderRoster()
  await rowAction('Hide')
  fireEvent.click(screen.getByRole('button', { name: /Hidden/ }))
  const binding = { connectionId: 'local', profile: 'default', roomId: room.room_id }
  await act(async () =>
    $canonicalRoomLive.set({
      [canonicalRoomLiveKey(binding, room.room_id)]: { working: true, needsYou: false, failed: false }
    })
  )
  expect(screen.getByRole('button', { name: room.name }).querySelector('[data-room-state="working"]')).toBeTruthy()
})

it('finds and opens a hidden-only match in a Groups-only search', async () => {
  renderRoster()
  await rowAction('Hide')
  fireEvent.pointerDown(screen.getByRole('button', { name: 'Filter roster' }), { button: 0, ctrlKey: false })
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Group chats only' }))
  expect(screen.getByRole('button', { name: 'Filter roster, 1 active' })).toBeTruthy()
  fireEvent.change(screen.getByRole('textbox', { name: 'Search bots and group chats' }), {
    target: { value: 'Launch' }
  })

  const row = await screen.findByRole('button', { name: room.name })
  expect(screen.getByText('Hidden')).toBeTruthy()
  fireEvent.click(row)
  const opened = openWorkspace.mock.calls.at(-1)
  expect($groupChatWorkspace.get()).toBe(key)

  if (!opened) {
    throw new Error('Groups-only search did not open its hidden match')
  }

  render(<ContribRender render={opened[1].render} />)
  expect(await within(await screen.findByRole('log')).findByText('Retained room history')).toBeTruthy()
  expect(requestProfile.mock.calls.find(([, method]) => method === 'groups.log')?.[2]).toMatchObject({
    room_id: room.room_id
  })
  expect(requestProfile.mock.calls.map(([, method]) => method)).not.toContain('groups.disband')
})

it('retains stored hides through empty and partial lists and reconnect loading until explicit Restore', async () => {
  const view = renderRoster()
  await rowAction('Hide')
  await waitFor(() => expect(context.storage.get(HIDDEN_ROOMS_KEY, {})).toEqual($hiddenRooms.get()))
  const saved = context.storage.get(HIDDEN_ROOMS_KEY, {})
  const binding = { connectionId: 'local', profile: 'default', roomId: room.room_id }
  const roomQuery = { queryKey: ['hermes-bots', 'gateway-group-rooms'] }

  listedRooms = []
  await act(async () => {
    await view.client.refetchQueries(roomQuery)
  })
  expect(screen.queryByRole('button', { name: room.name })).toBeNull()
  expect(isRoomHidden(binding, $hiddenRooms.get())).toBe(true)
  expect(context.storage.get(HIDDEN_ROOMS_KEY, {})).toEqual(saved)

  const other = { ...room, room_id: 'other-room', name: 'Other crew' }
  listedRooms = [other]
  await act(async () => {
    await view.client.refetchQueries(roomQuery)
  })
  expect(await screen.findByRole('button', { name: other.name })).toBeTruthy()
  expect(context.storage.get(HIDDEN_ROOMS_KEY, {})).toEqual(saved)

  const reconnect = Promise.withResolvers<RoomList>()
  await act(async () => setGateway('closed'))
  pendingRoomList = reconnect.promise
  await act(async () => setGateway('open'))
  await waitFor(() => expect(view.client.isFetching(roomQuery)).toBe(1))
  expect(isRoomHidden(binding, $hiddenRooms.get())).toBe(true)
  expect(context.storage.get(HIDDEN_ROOMS_KEY, {})).toEqual(saved)

  listedRooms = [room, other]
  await act(async () => {
    pendingRoomList = null
    reconnect.resolve({ rooms: listedRooms, next_offset: null })
  })
  await waitFor(() => expect(view.client.isFetching(roomQuery)).toBe(0))
  expect(screen.queryByRole('button', { name: room.name })).toBeNull()
  expect(context.storage.get(HIDDEN_ROOMS_KEY, {})).toEqual(saved)
  fireEvent.click(await screen.findByRole('button', { name: /Hidden/ }))
  expect(screen.getByRole('button', { name: room.name })).toBeTruthy()
  await rowAction('Restore')
  await waitFor(() =>
    expect(context.storage.get(HIDDEN_ROOMS_KEY, {})).toMatchObject({ 'local::default': { 'room-1': false } })
  )
  expect(isRoomHidden(binding, $hiddenRooms.get())).toBe(false)
  expect(screen.getByRole('button', { name: room.name })).toBeTruthy()
  expect(requestProfile.mock.calls.map(([, method]) => method)).not.toContain('groups.disband')
})

it('hides, restores and reopens same-name rooms by exact ID and history after stored-state reload', async () => {
  const old = { ...room, room_id: 'room-old', name: 'Studio ship huddle' }
  const newer = { ...old, room_id: 'room-new' }
  // Name lookup would pick the newer sibling; the intended old room is second.
  listedRooms = [newer, old]
  const view = renderRoster()
  const rows = await screen.findAllByRole('button', { name: old.name })
  expect(rows).toHaveLength(2)
  fireEvent.contextMenu(rows[1])
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Hide' }))
  expect($hiddenRooms.get()['local::default']).toEqual({ 'room-old': true })
  fireEvent.contextMenu(screen.getByRole('button', { name: old.name }))
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Hide' }))
  await waitFor(() =>
    expect(context.storage.get(HIDDEN_ROOMS_KEY, {})).toMatchObject({
      'local::default': { 'room-old': true, 'room-new': true }
    })
  )
  await waitFor(() => expect(context.storage.get(HIDDEN_ROOMS_KEY, {})).toEqual($hiddenRooms.get()))
  view.unmount()

  await act(async () => {
    $botMeta.set({})
    $hiddenRooms.set({})
    $canonicalGroupBindings.set({})
    $showHiddenBots.set(false)
    setPluginCtx(createPluginContext('hermes-bots'))
    await migrateBotMeta(context.storage)
  })
  renderRoster()
  fireEvent.click(await screen.findByRole('button', { name: /Hidden/ }))
  const hidden = screen.getByRole('button', { name: /Hidden/ }).parentElement!
  const hiddenRows = within(hidden).getAllByRole('button', { name: old.name })
  fireEvent.contextMenu(hiddenRows[0])
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Restore' }))
  expect($hiddenRooms.get()['local::default']).toEqual({ 'room-old': true, 'room-new': false })
  expect(within(hidden).getAllByRole('button', { name: old.name })).toHaveLength(1)

  fireEvent.click(within(hidden).getByRole('button', { name: old.name }))
  expect($groupChatWorkspace.get()).toBe('canonical:local:default:room-old')
  const opened = openWorkspace.mock.calls.at(-1)

  if (!opened) {
    throw new Error('The hidden old room did not reopen')
  }

  // A restored tab has no binding cache: resolve its stored key against both siblings.
  await act(async () => $canonicalGroupBindings.set({}))
  render(<ContribRender render={opened[1].render} />)
  const history = within(await screen.findByRole('log'))
  expect(await history.findByText('History for room-old')).toBeTruthy()
  expect(history.queryByText('History for room-new')).toBeNull()
  expect(
    requestProfile.mock.calls.filter(([, method]) => method === 'groups.log').map(([, , params]) => params.room_id)
  ).toEqual(['room-old'])
  expect(
    requestProfile.mock.calls
      .filter(([, method]) => method === 'groups.state')
      .every(([, , params]) => params.room_id === 'room-old')
  ).toBe(true)
  expect(requestProfile.mock.calls.map(([, method]) => method)).not.toContain('groups.create')
  expect(requestProfile.mock.calls.map(([, method]) => method)).not.toContain('groups.disband')
})
