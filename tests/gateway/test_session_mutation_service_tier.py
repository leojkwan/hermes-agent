"""FBL-194: a durable tier pick preserves the conversation across owner restarts."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.session_controls import AuthorityConnection
from gateway.session_local import create_local_session
from gateway.session_policy import policy_for_source
from gateway.session_state import SessionState, SERVICE_TIER_UNSET
from hermes_state_local import local_receipt
import hermes_state_runtime as rt


def _owner(home, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.run_config_loaders import GatewayConfigLoadersMixin
    from gateway.session import SessionStore
    from gateway.session_authority import SessionAuthority

    home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, 'home', lambda: home)
    monkeypatch.setenv('HERMES_HOME', str(home))
    config = {'agent': {'service_tier': 'priority'},
              'model': {'default': 'frozen', 'provider': 'custom', 'base_url': 'http://127.0.0.1:1/v1'}}
    monkeypatch.setattr(run, '_load_gateway_config', lambda *a: config)
    store = SessionStore(home / 'sessions', GatewayConfig())
    db, states, agents, evictions = store._db, {}, {}, []
    runner = SimpleNamespace(session_store=store, _session_db=db, adapters={}, _draining=False,
                             _evict_cached_agent=evictions.append, _cached_agent_for=agents.get,
                             _handle_message=None, _resolve_session_agent_runtime=lambda **k: ('frozen', {}),
                             _peek_session_state=states.get,
                             _session_state=lambda route: states.setdefault(route, SessionState()))
    runner._set_session_service_tier_override = lambda *a, **k: (
        GatewayConfigLoadersMixin._set_session_service_tier_override(runner, *a, **k))
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    authority = SessionAuthority(runner, profile_id=str(home), instance_id='owner', db=db,
                                 epoch=rt.begin_runtime_epoch(db, instance_id='owner'))
    owner = AuthorityConnection(authority, object(), {'user_id': 'human'})
    return store, db, authority, owner, agents, evictions


def _create(authority, owner, home, *, tier='priority'):
    return create_local_session(authority, owner.actor, dict(
        request_id='chat', source='gui', cwd=str(home), model='frozen', toolsets=[],
        service_tier=tier, follow_profile_config=True))


def _mutation(db, ref, request_id='tier'):
    row = db.get_session(ref.session_id)
    return {'id': request_id, 'method': 'session.mutate', 'params': {
        'session_id': ref.session_id, 'request_id': request_id, 'operation': 'service_tier',
        'payload': {'tier': 'Normal'}, 'expected_revision': row['runtime_revision'],
        'expected_generation': row['runtime_generation']}}


def _stored(db, sid):
    with db._read_ctx() as conn:
        metadata = [tuple(row) for row in conn.execute('SELECT key,value FROM state_meta ORDER BY key')]
    return db.get_session(sid), db.get_messages(sid), metadata


def _admission(db, authority, ref, request, status):
    rt.admit_session_input(db, epoch=authority.epoch, principal_id='human', session_id=ref.session_id,
                           request_id='input', payload={'text': 'preserve'})
    if status != 'queued':
        assert rt.claim_session_input(db, epoch=authority.epoch, session_id=ref.session_id)
    if status == 'unknown':
        authority.epoch = rt.begin_runtime_epoch(db, instance_id='restarted')
        rt.recover_session_inputs(db, epoch=authority.epoch)
    request['params']['expected_generation'] = db.get_session(ref.session_id)['runtime_generation']


@pytest.mark.asyncio
async def test_normal_pick_survives_restart_without_rebuilding_conversation(tmp_path, monkeypatch):
    from gateway.session_local_recovery import restore_local_session

    home, other_home = tmp_path / 'primary', tmp_path / 'other'
    other_store, other_db, other_authority, other_owner, _, _ = _owner(other_home, monkeypatch)
    other_ref = _create(other_authority, other_owner, other_home)
    other_before = _stored(other_db, other_ref.session_id)
    store, db, authority, owner, agents, evictions = _owner(home, monkeypatch)
    ref = _create(authority, owner, home)
    live = authority.sessions[ref.session_id]
    db.update_system_prompt(ref.session_id, 'cached system prompt')
    db.append_message(ref.session_id, role='user', content='preserved history')
    agent = agents[live.route] = SimpleNamespace(service_tier='priority', model='frozen', provider='custom',
                                                system_prompt='cached system prompt', tools=['retained'])
    request = _mutation(db, ref)
    before, history, _ = _stored(db, ref.session_id)
    launch = local_receipt(db, ref.session_id)['policy']['request_json']
    # The existing config.set path remains transient; a durable pick supersedes that override.
    transient = await owner.dispatch({'id': 'transient', 'method': 'config.set', 'params': {
        'session_id': ref.session_id, 'key': 'service_tier', 'value': 'ultrafast'}})
    assert transient['result']['value'] == 'ultrafast'
    watermark = live.event_stream.watermark()
    try:
        result = await owner.dispatch(request)
        assert 'result' in result, result
        assert result['result']['tier'] == 'normal', result
        assert await owner.dispatch(request) == result
        after = db.get_session(ref.session_id)
        assert after['runtime_generation'] == before['runtime_generation'] + 1
        assert after['runtime_revision'] == before['runtime_revision'] + 1
        assert after['system_prompt'] == before['system_prompt']
        assert (after['model'], after['model_config']) == (before['model'], before['model_config'])
        assert db.get_messages(ref.session_id) == history
        policy = policy_for_source(authority.runner, live.source)
        assert policy.request_json == launch
        assert json.loads(launch)['service_tier'] == 'priority'
        assert policy.current_service_tier(authority.runner, live.route) is None
        assert authority.runner._peek_session_state(live.route).conversation.service_tier_override is SERVICE_TIER_UNSET
        assert agents[live.route] is agent and agent.service_tier is None
        assert (agent.model, agent.provider, agent.system_prompt, agent.tools) == (
            'frozen', 'custom', 'cached system prompt', ['retained'])
        assert not evictions
        events = live.event_stream.since(*watermark)['events']
        assert any(event['type'] == 'session.info' and event['payload']['service_tier'] == '' for event in events)
        assert _stored(other_db, other_ref.session_id) == other_before
        # A pick also overrides followed defaults when creation did not explicitly select a tier.
        followed = create_local_session(authority, owner.actor, dict(
            request_id='followed', source='gui', cwd=str(home), model='frozen', toolsets=[], follow_profile_config=True))
        selected = await owner.dispatch(_mutation(db, followed, 'followed-tier'))
        assert selected['result']['tier'] == 'normal', selected
        followed_live = authority.sessions[followed.session_id]
        assert policy_for_source(authority.runner, followed_live.source).current_service_tier(
            authority.runner, followed_live.route) is None
    finally:
        await owner.close()
        store.close_all_db_handles()
        await other_owner.close()
        other_store.close_all_db_handles()

    store, db, authority, owner, _, _ = _owner(home, monkeypatch)
    try:
        restore_local_session(authority, ref.session_id)
        live = authority.sessions[ref.session_id]
        policy = policy_for_source(authority.runner, live.source)
        assert policy.current_service_tier(authority.runner, live.route) is None
        assert policy.request_json == launch
        assert await owner.dispatch(request) == result
        assert db.get_messages(ref.session_id) == history
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_tier_mutation_refuses_invalid_authority_fences_and_busy_work(tmp_path, monkeypatch):
    for case, reason in [('unauthorized', 'permission_denied'), ('revision', 'revision_conflict'),
                         ('generation', 'stale_generation'), ('missing_generation', 'invalid_params'),
                         ('queued', 'session_busy'), ('started', 'session_busy'),
                         ('unknown', 'unknown_execution'), ('lease', 'session_busy'),
                         ('invalid', 'invalid_params'), ('extra_payload', 'invalid_params')]:
        home = tmp_path / case
        store, db, authority, owner, _, _ = _owner(home, monkeypatch)
        ref = _create(authority, owner, home)
        request = _mutation(db, ref)
        params = request['params']
        setup = {
            'unauthorized': lambda: None,
            'revision': lambda: params.update(expected_revision=params['expected_revision'] + 1),
            'generation': lambda: params.update(expected_generation=params['expected_generation'] + 1),
            'missing_generation': lambda: params.pop('expected_generation'),
            'queued': lambda: _admission(db, authority, ref, request, 'queued'),
            'started': lambda: _admission(db, authority, ref, request, 'started'),
            'unknown': lambda: _admission(db, authority, ref, request, 'unknown'),
            'lease': lambda: db.try_acquire_session_turn_lease(ref.session_id, 'foreign-turn'),
            'invalid': lambda: params['payload'].update(tier='instant'),
            'extra_payload': lambda: params['payload'].update(persist=True),
        }
        prepared = setup[case]()
        if case == 'lease':
            assert prepared
        client = (AuthorityConnection(authority, object(), {'user_id': 'reader', 'capabilities': ['session:read']})
                  if case == 'unauthorized' else owner)
        before = _stored(db, ref.session_id)
        try:
            response = await client.dispatch(request)
            assert response['error']['message'] == reason, (case, response)
            assert _stored(db, ref.session_id) == before, case
        finally:
            if client is not owner:
                await client.close()
            await owner.close()
            store.close_all_db_handles()
