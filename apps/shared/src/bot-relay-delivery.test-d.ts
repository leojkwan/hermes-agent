import type { BotRelayDeliverParams } from './gateway-contract.generated'

const legacy: BotRelayDeliverParams = { profile: 'ops', message: 'hello' }
const selected: BotRelayDeliverParams = {
  profile: 'ops', message: 'hello', delivery_id: 'a'.repeat(64), session_id: 'chat',
}
// @ts-expect-error A selected delivery requires its persistent session.
const missingSession: BotRelayDeliverParams = { profile: 'ops', message: 'hello', delivery_id: 'a'.repeat(64) }
// @ts-expect-error A selected session requires its retained delivery identity.
const missingDelivery: BotRelayDeliverParams = { profile: 'ops', message: 'hello', session_id: 'chat' }
// @ts-expect-error Null cannot opt in to selected delivery.
const nullPair: BotRelayDeliverParams = { profile: 'ops', message: 'hello', delivery_id: null, session_id: null }

void [legacy, selected, missingSession, missingDelivery, nullPair]
