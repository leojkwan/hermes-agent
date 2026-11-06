"""Retained local conversations keep their creation identity across profile renames."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig
from gateway.session import SessionStore
from gateway.session_authorities import SessionAuthorities
from gateway.session_authority import initialize_session_authority
from gateway.session_contract import SessionActor, Submission
from gateway.session_local import create_local_session, staged_creation_follows
from gateway.session_local_recovery import restore_local_session
from hermes_state import SessionDB
from hermes_state_local import local_receipt


async def owner(home, root):
    store = SessionStore(home / 'sessions', GatewayConfig(multiplex_profiles=True))
    store._db = SessionDB(home / 'state.db')
    runner = SimpleNamespace(session_store=store, _session_db=store._db, adapters={}, _draining=False,
                             _cached_agent_for=lambda route: None, _evict_cached_agent=lambda route: None)
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    runner.session_authorities = SessionAuthorities(root, multiplexed=True)
    authority = await initialize_session_authority(runner, profile_id=str(home), instance_id='fixture')
    runner.session_authorities.add(home, authority, name=home.name)
    actor = SessionActor('uid:1000', str(home), frozenset({'session:create', 'session:read', 'session:submit'}), 'native')
    return authority, actor


@pytest.mark.asyncio
async def test_retained_chat_runs_and_creation_retry_keeps_id_after_two_renames(tmp_path, monkeypatch):
    from gateway import run
    root = tmp_path / '.hermes'
    home = root / 'profiles' / 'watchdog'
    home.mkdir(parents=True)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(root))
    monkeypatch.setattr(run, '_load_gateway_config', lambda *args: {'platform_toolsets': {'cli': []}})
    authority, actor = await owner(home, root)
    params = dict(request_id='bot', source='gui', cwd=str(tmp_path), model='fixture', toolsets=[],
                  follow_profile_config=True)
    ref = create_local_session(authority, actor, params)
    authority.db.append_message(ref.session_id, 'user', 'retained history')
    original = local_receipt(authority.db, ref.session_id)
    assert original['route'].startswith('agent:watchdog:'), original['route']
    authority.db.close()
    for name in ('studio', 'studio2'):
        renamed = home.with_name(name)
        home.rename(renamed)
        db = SessionDB(renamed / 'state.db')
        db.rekey_profile_state(home.name, name)
        db.rekey_profile_state(home.name, name)
        db.close()
        authority, actor = await owner(renamed, root)
        restored = restore_local_session(authority, ref.session_id)
        assert restored.session_id == ref.session_id
        assert staged_creation_follows(authority, actor, params['request_id']) is True
        assert create_local_session(authority, actor, params).session_id == ref.session_id
        assert authority.db.get_messages_as_conversation(ref.session_id)[0]['content'] == 'retained history'
        saved = local_receipt(authority.db, ref.session_id)
        assert saved['principal_id'] == original['principal_id']
        assert saved['policy'] == original['policy']
        assert len(authority.db._read_all('SELECT id FROM sessions')) == 1
        authority.db.close()
        home = renamed
    authority, actor = await owner(home, root)
    seen = []

    async def execute(authority, ref, row):
        seen.append((ref.session_id, row['payload']['text']))
        return 'bounded reply'

    monkeypatch.setattr('gateway.session_finite.execute_finite_admission', execute)
    await authority.submit(actor, Submission('next-turn', restore_local_session(authority, ref.session_id),
                                             {'text': 'continue'}, 'queue'))
    await authority.sessions[ref.session_id].task
    assert seen == [(ref.session_id, 'continue')]
    authority.db.close()


async def scenario(tmp_path, monkeypatch):
    from gateway import run
    root = tmp_path / '.hermes'
    home = root / 'profiles' / 'watchdog'
    home.mkdir(parents=True)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(root))
    monkeypatch.setattr(run, '_load_gateway_config', lambda *args: {'platform_toolsets': {'cli': []}})
    authority, actor = await owner(home, root)
    params = dict(request_id='bot', source='gui', cwd=str(tmp_path), model='fixture', toolsets=[])
    ref = create_local_session(authority, actor, params)
    return authority, actor, ref, home, root


def save_receipt(db, receipt):
    from hermes_state_local import POLICY_PREFIX
    db._write_sql('UPDATE state_meta SET value=? WHERE key=?',
                  (json.dumps(receipt), POLICY_PREFIX + receipt['session_id']))


def snapshot(db):
    with db._read_ctx() as conn:
        return list(conn.iterdump())


@pytest.mark.asyncio
async def test_old_version_partial_rename_repairs_exact_retained_receipt(tmp_path, monkeypatch):
    authority, _, ref, home, root = await scenario(tmp_path, monkeypatch)
    original = local_receipt(authority.db, ref.session_id)
    authority.db.rekey_profile_state('watchdog', 'studio')
    # Older builds moved rows and routes but left this private receipt behind.
    save_receipt(authority.db, original)
    authority.db.rekey_profile_state('watchdog', 'studio')
    saved = local_receipt(authority.db, ref.session_id)
    assert saved['session_id'] == original['session_id']
    assert saved['entry']['origin']['profile'] == 'studio'
    assert authority.db.get_session(ref.session_id)['session_key'] == saved['route']
    authority.db.close()
    renamed = home.with_name('studio')
    home.rename(renamed)
    authority, _ = await owner(renamed, root)
    assert restore_local_session(authority, ref.session_id).session_id == ref.session_id
    authority.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['identity', 'principal', 'origin', 'collision', 'queued', 'started',
                                    'unknown', 'worker', 'lease', 'compression'])
async def test_refused_migration_is_atomic(tmp_path, monkeypatch, damage):
    import time
    from hermes_state_keys import local_identity
    from hermes_state_runtime import admit_session_input, claim_session_input, RuntimeStoreError
    from hermes_state_errors import SessionTurnLeaseLostError, SessionCompressionInProgressError
    authority, actor, ref, home, _ = await scenario(tmp_path, monkeypatch)
    db = authority.db
    receipt = local_receipt(db, ref.session_id)
    if damage in {'identity', 'principal', 'origin'}:
        if damage == 'identity':
            receipt['request_id'] = 'forged'
        elif damage == 'principal':
            receipt['principal_id'] = 'uid:foreign'
        else:
            receipt['entry']['origin']['profile'] = 'foreign'
        save_receipt(db, receipt)
        before = snapshot(db)
        with pytest.raises((RuntimeStoreError, ValueError, SessionTurnLeaseLostError, SessionCompressionInProgressError)):
            db.rekey_profile_state('watchdog', 'studio')
        assert snapshot(db) == before
        db.close()
        return
    if damage == 'collision':
        sid = local_identity(str(home.with_name('studio')), actor.subject, receipt['request_id'])
        db.create_session(sid, 'local', user_id='foreign')
    elif damage in {'queued', 'started', 'unknown'}:
        admit_session_input(db, epoch=authority.epoch, principal_id=actor.subject, session_id=ref.session_id,
                            request_id='pending', payload={'text': 'never execute'})
        if damage != 'queued':
            claim_session_input(db, epoch=authority.epoch, session_id=ref.session_id)
        if damage == 'unknown':
            db._write_sql("UPDATE session_admissions SET status='unknown'")
    elif damage == 'worker':
        db._write_sql("INSERT INTO worker_executions(execution_id,session_id,kind,owner_epoch,generation,status,adoption_digest) "
                      "VALUES('worker',?,'compute',1,1,'running','digest')", (ref.session_id,))
    else:
        table, column = ('compression_locks', 'session_id') if damage == 'compression' else ('session_turn_leases', 'conversation_id')
        db._write_sql(f'INSERT INTO {table}({column},holder,acquired_at,expires_at) VALUES(?,?,?,?)',
                      (ref.session_id, 'holder', time.time(), time.time() + 600))
    before = snapshot(db)
    with pytest.raises((RuntimeStoreError, ValueError, SessionTurnLeaseLostError, SessionCompressionInProgressError)):
        db.rekey_profile_state('watchdog', 'studio')
    assert snapshot(db) == before
    db.close()


@pytest.mark.asyncio
async def test_config_reference_moves_metadata_without_reading_or_changing_secret_guards(tmp_path, monkeypatch):
    from dataclasses import asdict, replace
    from gateway import run
    from gateway.session_policy import restore_policy
    from gateway.session_policy_credentials import PREFIX, config_reference, recover_config_secrets
    from hermes_state_runtime import RuntimeStoreError
    authority, _, ref, home, root = await scenario(tmp_path, monkeypatch)
    receipt = local_receipt(authority.db, ref.session_id)
    policy = replace(restore_policy(receipt['policy']), config_json=json.dumps({
        'model': {'default': 'fixture', 'provider': 'custom', 'base_url': 'http://127.0.0.1:99/v1', 'api_key': None},
        'session_pins': {'reasoning': True}}))
    encoded = config_reference(authority, ref.session_id, policy, {('model', 'api_key'): 'synthetic-key'})
    receipt['policy'] = asdict(replace(policy, config_secret_ref=encoded))
    save_receipt(authority.db, receipt)
    monkeypatch.setattr(run, '_load_gateway_config', lambda *args: pytest.fail('migration read credentials'))
    authority.db.rekey_profile_state('watchdog', 'studio')
    moved = local_receipt(authority.db, ref.session_id)
    before, after = json.loads(encoded[len(PREFIX):]), json.loads(moved['policy']['config_secret_ref'][len(PREFIX):])
    assert {k: v for k, v in before.items() if k not in {'home', 'profile'}} == {
        k: v for k, v in after.items() if k not in {'home', 'profile'}}
    authority.db.close()
    renamed = home.with_name('studio')
    home.rename(renamed)
    authority, _ = await owner(renamed, root)
    config = {'model': {'default': 'fixture', 'provider': 'custom', 'base_url': 'http://127.0.0.1:99/v1', 'api_key': 'synthetic-key'}}
    monkeypatch.setattr(run, '_load_gateway_config', lambda *args: config)
    restored = restore_policy(moved['policy'])
    assert recover_config_secrets(authority, restored) == {('model', 'api_key'): 'synthetic-key'}
    config['model']['api_key'] = 'changed-key'
    with pytest.raises(RuntimeStoreError, match='launch_credentials_unavailable'):
        recover_config_secrets(authority, restored)
    config['model']['api_key'] = 'synthetic-key'
    altered = replace(restored, config_json=restored.config_json.replace('127.0.0.1:99', '127.0.0.1:100'))
    with pytest.raises(RuntimeStoreError, match='launch_credentials_unavailable'):
        recover_config_secrets(authority, altered)
    source = json.loads(restored.config_secret_ref[len(PREFIX):])
    source['home'] = str(home.with_name('foreign'))
    with pytest.raises(RuntimeStoreError, match='launch_credentials_unavailable'):
        recover_config_secrets(authority, replace(restored, config_secret_ref=PREFIX + json.dumps(source)))
    authority.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['settled', 'failed', 'cancelled', 'queued', 'claimed', 'ambiguous', 'canonical'])
async def test_mailbox_moves_only_settled_receipts_without_replay(tmp_path, monkeypatch, status):
    from tools.bot_live_delivery import _locked, _write, _read
    from hermes_state_runtime import RuntimeStoreError
    authority, actor, ref, home, _ = await scenario(tmp_path, monkeypatch)
    key = 'b' * 32
    record = dict(delivery_id=key, admission_id='retired-terminal', status=status, message='private retained message',
                  profile_home=str(home), session_id=ref.session_id, principal_id=actor.subject, reply='retained reply')
    with _locked(home) as mailbox:
        path = mailbox / (key + '.json')
        _write(path, record)
    before = snapshot(authority.db)
    if status in {'settled', 'failed', 'cancelled'}:
        authority.db.rekey_profile_state('watchdog', 'studio')
        authority.db.rekey_profile_state('watchdog', 'studio')
        after = _read(path)
        assert after == {**record, 'profile_home': str(home.with_name('studio'))}
        assert authority.db._read_all('SELECT * FROM session_admissions') == []
    else:
        with pytest.raises(RuntimeStoreError):
            authority.db.rekey_profile_state('watchdog', 'studio')
        assert _read(path) == record
        assert snapshot(authority.db) == before
    authority.db.close()


@pytest.mark.asyncio
async def test_mailbox_failed_write_resumes_exact_journal_and_blocks_stale_reader(tmp_path, monkeypatch):
    from tools import bot_live_delivery as mailbox
    from hermes_state_runtime import RuntimeStoreError
    from gateway.session_profile_mailbox import JOURNAL
    authority, actor, ref, home, _ = await scenario(tmp_path, monkeypatch)
    key = 'c' * 32
    record = dict(delivery_id=key, admission_id='retired', status='settled', message='once', reply='done',
                  profile_home=str(home), session_id=ref.session_id, principal_id=actor.subject)
    with mailbox._locked(home) as directory:
        path = directory / (key + '.json')
        mailbox._write(path, record)
    write = mailbox._write
    monkeypatch.setattr(mailbox, '_write', lambda *args: (_ for _ in ()).throw(OSError('injected write failure')))
    with pytest.raises(OSError):
        authority.db.rekey_profile_state('watchdog', 'studio')
    assert authority.db._read_one('SELECT value FROM state_meta WHERE key=?', (JOURNAL,))
    with pytest.raises(RuntimeStoreError, match='runtime_coordination_required'):
        restore_local_session(authority, ref.session_id)
    monkeypatch.setattr(mailbox, '_write', write)
    authority.db.rekey_profile_state('watchdog', 'studio')
    assert not authority.db._read_one('SELECT value FROM state_meta WHERE key=?', (JOURNAL,))
    assert mailbox._read(path) == {**record, 'profile_home': str(home.with_name('studio'))}
    authority.db.close()


@pytest.mark.asyncio
async def test_reset_compression_pins_and_settled_admissions_survive_rename(tmp_path, monkeypatch):
    from hermes_state_runtime import admit_session_input, claim_session_input, settle_session_input
    authority, actor, ref, home, root = await scenario(tmp_path, monkeypatch)
    db = authority.db
    db.append_message(ref.session_id, 'user', 'before compression')
    db.set_session_pinned(ref.session_id, True)
    assert db.try_acquire_compression_lock(ref.session_id, 'fixture')
    try:
        db.publish_compression_child(parent_session_id=ref.session_id, child_session_id='compressed', source='gui',
                                     messages=[{'role': 'user', 'content': 'retained summary'}], compression_lock_holder='fixture')
    finally:
        db.release_compression_lock(ref.session_id, 'fixture')
    restore_local_session(authority, ref.session_id)
    entry = authority.runner.session_store.reset_session(local_receipt(db, ref.session_id)['route'])
    db.append_message(entry.session_id, 'user', 'after reset')
    admit_session_input(db, epoch=authority.epoch, principal_id=actor.subject, session_id=ref.session_id,
                        request_id='settled', payload={'text': 'historical input'})
    claimed = claim_session_input(db, epoch=authority.epoch, session_id=ref.session_id)
    settle_session_input(db, epoch=authority.epoch, admission_id=claimed['admission_id'],
                         generation=claimed['generation'], outcome='completed')
    before = local_receipt(db, ref.session_id)
    messages = [tuple(row) for row in db._read_all('SELECT * FROM messages ORDER BY id')]
    admissions = [tuple(row) for row in db._read_all('SELECT * FROM session_admissions ORDER BY seq')]
    db.rekey_profile_state('watchdog', 'studio')
    after = local_receipt(db, ref.session_id)
    assert after['lineage'] == before['lineage'] == [ref.session_id, 'compressed', entry.session_id]
    assert [tuple(row) for row in db._read_all('SELECT * FROM messages ORDER BY id')] == messages
    assert [tuple(row) for row in db._read_all('SELECT * FROM session_admissions ORDER BY seq')] == admissions
    assert db.get_session(ref.session_id)['pinned'] == 1
    db.close()
    renamed = home.with_name('studio')
    home.rename(renamed)
    authority, actor = await owner(renamed, root)
    restored = restore_local_session(authority, ref.session_id)
    from gateway.session_local_recovery import local_history
    assert local_history(authority, restored)[0]['content'] == 'after reset'
    assert authority.db.get_session(ref.session_id)['pinned'] == 1
    authority.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('broken', ['home', 'endpoint', 'entries'])
async def test_invalid_config_reference_refuses_the_whole_migration(tmp_path, monkeypatch, broken):
    from dataclasses import asdict, replace
    from gateway.session_policy import restore_policy
    from gateway.session_policy_credentials import PREFIX, config_reference
    from hermes_state_runtime import RuntimeStoreError
    authority, _, ref, home, _ = await scenario(tmp_path, monkeypatch)
    receipt = local_receipt(authority.db, ref.session_id)
    policy = replace(restore_policy(receipt['policy']), config_json=json.dumps({
        'model': {'default': 'fixture', 'provider': 'custom', 'base_url': 'http://127.0.0.1:99/v1', 'api_key': None}}))
    encoded = config_reference(authority, ref.session_id, policy, {('model', 'api_key'): 'synthetic-key'})
    if broken in {'home', 'entries'}:
        source = json.loads(encoded[len(PREFIX):])
        if broken == 'home':
            source['home'] = str(home.with_name('foreign'))
        else:
            source['entries'] = 'malformed'
        encoded = PREFIX + json.dumps(source)
    else:
        policy = replace(policy, config_json=policy.config_json.replace('127.0.0.1:99', '127.0.0.1:100'))
    receipt['policy'] = asdict(replace(policy, config_secret_ref=encoded))
    save_receipt(authority.db, receipt)
    before = snapshot(authority.db)
    with pytest.raises(RuntimeStoreError, match='launch_credentials_unavailable'):
        authority.db.rekey_profile_state('watchdog', 'studio')
    assert snapshot(authority.db) == before
    authority.db.close()


@pytest.mark.asyncio
async def test_live_repair_retires_and_reopens_served_authority(tmp_path, monkeypatch):
    import asyncio
    from gateway.profile_identity_control import migration_verb
    from hermes_state_runtime import RuntimeStoreError
    authority, _, ref, home, root = await scenario(tmp_path, monkeypatch)
    authority.db.close()
    renamed = home.with_name('studio')
    home.rename(renamed)
    stale, _ = await owner(renamed, root)
    runner, events = stale.runner, []
    runner._served_profile_homes = {'studio': renamed}
    runner._primary_profile_name = 'default'
    lock = asyncio.Lock()
    runner._reconcile_lock = lambda: lock
    runner.session_store._routing_home = root

    async def unserve(name, path):
        assert lock.locked() and stale.retiring
        events.append('retire')
        stale.db.close()
        runner.session_authorities.remove(path)

    async def serve(known, added, removed, changed, **kwargs):
        assert lock.locked() and events == ['retire']
        fresh, actor = await owner(renamed, root)
        assert fresh is not stale
        assert restore_local_session(fresh, ref.session_id).session_id == ref.session_id
        foreign = SessionActor('uid:foreign', str(renamed), actor.capabilities, 'native')
        with pytest.raises(RuntimeStoreError, match='permission_denied'):
            fresh.authorize(foreign, restore_local_session(fresh, ref.session_id), 'session:read')
        events.append('serve')
        fresh.db.close()
        return {'served_profiles': ['studio']}

    runner._unserve_profile = unserve
    runner._record_served_profiles = lambda *args: None
    runner._apply_profile_changes = serve
    response = await asyncio.to_thread(migration_verb(runner), {'old': 'watchdog', 'new': 'studio'})
    assert response['ok'] is True, response
    assert events == ['retire', 'serve']


@pytest.mark.asyncio
@pytest.mark.parametrize('unchanged', [True, False])
async def test_live_repair_restores_only_a_known_unchanged_refusal(tmp_path, monkeypatch, unchanged):
    import asyncio
    from gateway import profile_identity_control as control
    authority, _, _, home, root = await scenario(tmp_path, monkeypatch)
    authority.db.close()
    renamed = home.with_name('studio')
    home.rename(renamed)
    authority, _ = await owner(renamed, root)
    before = snapshot(authority.db)
    runner, events = authority.runner, []
    runner._served_profile_homes = {'studio': renamed}
    runner._primary_profile_name = 'default'
    lock = asyncio.Lock()
    runner._reconcile_lock = lambda: lock

    async def unserve(name, path):
        events.append('retire')
        authority.db.close()

    async def serve(*args, **kwargs):
        events.append('serve')
        with_db = SessionDB(renamed / 'state.db')
        assert snapshot(with_db) == before
        with_db.close()
        return {'served_profiles': ['studio']}

    def refuse(*args):
        raise control.MigrationFailure(ValueError('injected refusal'), unchanged=unchanged)

    runner._unserve_profile = unserve
    runner._record_served_profiles = lambda *args: None
    runner._apply_profile_changes = serve
    monkeypatch.setattr(control, '_migrate_stores', refuse)
    monkeypatch.setattr('gateway.run_runtime.park_profile', lambda *args: events.append('park'))
    response = await asyncio.to_thread(control.migration_verb(runner), {'old': 'watchdog', 'new': 'studio'})
    assert response['ok'] is False
    assert events == (['retire', 'serve'] if unchanged else ['retire', 'park'])
    assert response.get('restored_profile', False) == unchanged


@pytest.mark.asyncio
async def test_control_timeout_keeps_one_inflight_migration(tmp_path, monkeypatch):
    import asyncio
    import threading
    from gateway import profile_identity_control as control
    from tests.waiting import async_wait_until
    gate, done, calls = threading.Event(), threading.Event(), []
    lock = asyncio.Lock()
    runner = SimpleNamespace(session_store=object(), _served_profile_homes={}, _reconcile_lock=lambda: lock)

    def migrate(*args):
        calls.append('start')
        assert gate.wait(5)
        done.set()
        return {'ok': True}

    monkeypatch.setattr(control, '_migrate_stores', migrate)
    monkeypatch.setattr(control, 'CONTROL_WAIT_SECONDS', 0.01)
    handler = control.migration_verb(runner)
    try:
        first = await asyncio.to_thread(handler, {'old': 'watchdog', 'new': 'studio'})
        second = await asyncio.to_thread(handler, {'old': 'watchdog', 'new': 'studio'})
        assert first['pending'] and second['pending']
        assert calls == ['start']
    finally:
        gate.set()
        await async_wait_until(done.is_set, timeout=5, what='timed-out migration to complete')
    assert calls == ['start']


@pytest.mark.asyncio
async def test_cron_creation_and_recovery_reuse_the_migrated_execution(tmp_path, monkeypatch):
    from gateway.session_cron import operation, _create
    from hermes_state_keys import CRON_PRINCIPAL_ID
    from hermes_state_runtime import admit_session_input, claim_session_input, settle_session_input
    authority, _, _, home, root = await scenario(tmp_path, monkeypatch)
    actor = SessionActor(CRON_PRINCIPAL_ID, str(home), frozenset({
        'session:create', 'session:read', 'session:submit', 'session:control'}), 'cron')
    request = 'cron:job:execution'
    params = {'job_id': 'job', 'request_id': 'execution', 'extra_prompt': None}
    monkeypatch.setattr('cron.jobs.get_job', lambda jid: {'id': jid, 'workdir': str(tmp_path), 'prompt': 'retained'})
    monkeypatch.setattr('cron.scheduler._load_cron_job_config', lambda *args: SimpleNamespace(model='fixture'))
    from hermes_state_local import commit_local_session
    staged, _ = _create(authority, actor, params)
    commit_local_session(authority.db, epoch=authority.epoch, receipt=staged)
    ref = restore_local_session(authority, staged['session_id'])
    admit_session_input(authority.db, epoch=authority.epoch, principal_id=actor.subject, session_id=ref.session_id,
                        request_id=request, payload={'text': 'retained'})
    claimed = claim_session_input(authority.db, epoch=authority.epoch, session_id=ref.session_id)
    settle_session_input(authority.db, epoch=authority.epoch, admission_id=claimed['admission_id'],
                         generation=claimed['generation'], outcome='completed')
    authority.db.rekey_profile_state('watchdog', 'studio')
    authority.db.close()
    renamed = home.with_name('studio')
    home.rename(renamed)
    authority, _ = await owner(renamed, root)
    params = {'job_id': 'job', 'request_id': 'execution', 'extra_prompt': None}
    actual = await operation(authority, 'recover', params)
    assert actual['status'] != 'missing', actual
    actor = SessionActor(CRON_PRINCIPAL_ID, str(renamed), actor.capabilities, 'cron')
    assert _create(authority, actor, params)[0].session_id == ref.session_id
    assert len(authority.db._read_all('SELECT * FROM sessions')) == 2
    assert len(authority.db._read_all('SELECT * FROM session_admissions')) == 1
    authority.db.close()


@pytest.mark.asyncio
async def test_partial_cross_store_failure_blocks_startup_until_same_migration_finishes(tmp_path, monkeypatch):
    from gateway.profile_identity_control import _migrate_stores, MigrationFailure
    from gateway.session_profile_mailbox import PENDING
    from hermes_state_runtime import RuntimeStoreError
    authority, _, ref, home, root = await scenario(tmp_path, monkeypatch)
    authority.db.close()
    renamed = home.with_name('studio')
    home.rename(renamed)
    routing = SessionDB(root / 'state.db')
    real_rekey = routing.rekey_profile_state

    def fail_after_preflight(*args, **kwargs):
        if kwargs.get('validate_only'):
            return real_rekey(*args, **kwargs)
        raise OSError('injected root database failure')

    monkeypatch.setattr(routing, 'rekey_profile_state', fail_after_preflight)
    store = SimpleNamespace(_routing_home=root, _routing_db=routing, rekey_profile_routing=lambda *args: 0)
    with pytest.raises(MigrationFailure) as raised:
        _migrate_stores(store, 'watchdog', 'studio')
    assert not raised.value.unchanged
    db = SessionDB(renamed / 'state.db')
    assert db._read_one('SELECT value FROM state_meta WHERE key=?', (PENDING,))
    runner = SimpleNamespace(_session_db=db, session_store=object())
    with pytest.raises(RuntimeStoreError, match='runtime_coordination_required'):
        await initialize_session_authority(runner, profile_id=str(renamed), instance_id='refused')
    db.close()
    monkeypatch.setattr(routing, 'rekey_profile_state', real_rekey)
    assert _migrate_stores(store, 'watchdog', 'studio')['recovered'] is True
    authority, _ = await owner(renamed, root)
    assert restore_local_session(authority, ref.session_id).session_id == ref.session_id
    assert not authority.db._read_one('SELECT value FROM state_meta WHERE key=?', (PENDING,))
    authority.db.close()
    routing.close()
