"""FBL-377: profile repair validates real Bot producers, never rewrites their ledger."""
import json
from types import SimpleNamespace

import pytest

from gateway.session_bot import deliver, recover_bot_deliveries
from gateway.session_local_recovery import restore_local_session
from gateway.session_local_title import title_new_session
from gateway.session_profile_mailbox import JOURNAL, PENDING
from hermes_state import SessionDB
from hermes_state_keys import admission_fingerprint
from hermes_state_local import local_receipt
from hermes_state_runtime import RuntimeStoreError
from tests.gateway.test_local_profile_rename import owner, scenario, snapshot
from tests.gateway.test_session_bot_retry import _quiet
from tests.waiting import async_wait_until
from tools import bot_live_delivery as mailbox


def bind_adapter(authority):
    from gateway.session_local_recovery import local_adapter_map
    authority.runner._adapter_for_source = lambda source: local_adapter_map(authority).get(source.platform)


async def producer(tmp_path, monkeypatch, *, retry=False):
    authority, actor, ref, home, root = await scenario(tmp_path, monkeypatch)
    bind_adapter(authority)
    title_new_session(authority, ref, 'Bot Chat')
    executions = []
    failures = [True] if retry else []

    async def execute(authority, ref, row):
        executions.append(row['admission_id'])
        target = row['payload']['local_automation_v1']['target']
        authority.db.append_message(target, 'user', row['payload']['text'])
        authority.db.append_message(target, 'assistant', 'bounded reply')
        if failures:
            failures.pop()
            authority.pending_results[row['admission_id']] = {
                'result': {'final_response': '', 'messages': [], 'failed': True,
                           'error': 'HTTP 503 server error'}, 'usage': {}}
            return ''
        return 'bounded reply'

    monkeypatch.setattr('gateway.session_finite.execute_finite_admission', execute)
    bot = SimpleNamespace(authority=authority, actor=actor, ref=ref, home=home, root=root, executions=executions)
    await send(bot, 'a' * 32)
    return bot


async def send(bot, key):
    await deliver(SimpleNamespace(authority=bot.authority, actor=bot.actor),
                  dict(id=key, profile=bot.home.name, message='synthetic ' + key))
    await async_wait_until(lambda: _quiet(bot), timeout=10, what='Bot producer and receipts to settle')
    record = mailbox.read_delivery_result(bot.home, key)
    assert record['status'] == 'settled', record
    return record


def rows(db, table):
    return [tuple(row) for row in db._read_all(f'SELECT * FROM {table} ORDER BY rowid')]


def files(home):
    return {path.name: path.read_bytes() for path in mailbox._root(home).glob('*.json')}


@pytest.mark.asyncio
@pytest.mark.parametrize('retry', [False, True])
async def test_real_bot_survives_two_renames_repeat_and_journal_resume(tmp_path, monkeypatch, retry):
    bot = await producer(tmp_path, monkeypatch, retry=retry)
    sid = bot.ref.session_id
    original = local_receipt(bot.authority.db, sid)
    for index, name in enumerate(('studio', 'studio2')):
        db = bot.authority.db
        admissions, history, before = rows(db, 'session_admissions'), rows(db, 'messages'), files(bot.home)
        execution_count = len(bot.executions)
        db.close()
        renamed = bot.home.with_name(name)
        bot.home.rename(renamed)
        db = SessionDB(renamed / 'state.db')
        if index == 1:
            write = mailbox._write
            with monkeypatch.context() as patch:
                patch.setattr(mailbox, '_write', lambda *args: (_ for _ in ()).throw(OSError('interrupted')))
                with pytest.raises(OSError, match='interrupted'):
                    db.rekey_profile_state(bot.home.name, name)
            assert db._read_one('SELECT value FROM state_meta WHERE key=?', (JOURNAL,))
            assert mailbox._write is write
        db.rekey_profile_state(bot.home.name, name)
        db.rekey_profile_state(bot.home.name, name)
        assert rows(db, 'session_admissions') == admissions
        assert rows(db, 'messages') == history
        for filename, raw in before.items():
            expected = json.loads(raw)
            expected['profile_home'] = str(renamed)
            assert json.loads(files(renamed)[filename]) == expected
        assert not db._read_all('SELECT * FROM state_meta WHERE key IN (?,?)', (JOURNAL, PENDING))
        assert local_receipt(db, sid)['principal_id'] == original['principal_id']
        db.close()
        bot.home = renamed
        bot.authority, bot.actor = await owner(renamed, bot.root)
        bind_adapter(bot.authority)
        assert restore_local_session(bot.authority, sid).session_id == sid
        await recover_bot_deliveries(bot.authority)
        await async_wait_until(lambda: _quiet(bot), timeout=10, what='recovery idle')
        assert len(bot.executions) == execution_count
        assert rows(bot.authority.db, 'session_admissions') == admissions
        if index == 0:
            # Compression/reset retain the logical owner while the next delivery names its new tip.
            db = bot.authority.db
            assert db.try_acquire_compression_lock(sid, 'fixture')
            try:
                db.publish_compression_child(parent_session_id=sid, child_session_id='compressed', source='gui',
                    messages=[{'role': 'assistant', 'content': 'synthetic summary'}], compression_lock_holder='fixture')
            finally:
                db.release_compression_lock(sid, 'fixture')
            restore_local_session(bot.authority, sid)
            bot.authority.runner.session_store.reset_session(local_receipt(db, sid)['route'])
            await send(bot, 'b' * 32)
    assert bot.authority.db.get_messages_as_conversation(sid)
    bot.authority.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['controller', 'uid_principal', 'foreign_principal', 'route', 'namespace',
    'request', 'message', 'digest', 'owner', 'target', 'queued', 'unknown', 'retry_identity', 'retry_row',
    'retry_principal', 'retry_id', 'collision'])
async def test_real_bot_damage_refuses_atomically(tmp_path, monkeypatch, damage):
    bot = await producer(tmp_path, monkeypatch, retry=damage.startswith('retry'))
    db, key = bot.authority.db, 'a' * 32
    path = mailbox._root(bot.home) / (key + '.json')
    record = mailbox._read(path)
    row = dict(db._read_one('SELECT * FROM session_admissions WHERE admission_id=?', (record['admission_id'],)))
    payload = json.loads(row['payload_json'])
    descriptor = payload['local_automation_v1']
    if damage == 'controller':
        record['principal_id'] = 'uid:foreign'
    elif damage == 'retry_identity':
        record['retry']['identity'] = 'bot:forged:retry'
    elif damage == 'retry_id':
        record['retry_admission_id'] = record['admission_id']
    elif damage == 'collision':
        from hermes_state_keys import local_identity
        receipt = local_receipt(db, bot.ref.session_id)
        db.create_session(local_identity(str(bot.home.with_name('studio')), bot.actor.subject, receipt['request_id']),
                          'local', user_id='foreign')
    elif damage.startswith('retry'):
        column, value = ('request_id', 'forged') if damage == 'retry_row' else ('principal_id', 'automation:foreign')
        db._write_sql(f'UPDATE session_admissions SET {column}=? WHERE admission_id=?',
                      (value, record['retry_admission_id']))
    else:
        changes = {
            'uid_principal': (row, 'principal_id', bot.actor.subject),
            'foreign_principal': (row, 'principal_id', 'automation:agent:watchdog:local:dm:foreign'),
            'route': (descriptor, 'route', 'agent:watchdog:local:dm:foreign'),
            'namespace': (descriptor, 'route', 'agent:INVALID:local:dm:' + bot.ref.session_id),
            'request': (row, 'request_id', 'forged'),
            'message': (payload, 'text', 'different message'),
            'owner': (descriptor, 'owner', 'foreign'),
            'target': (descriptor, 'target', 'foreign'),
            'queued': (row, 'status', 'queued'),
            'unknown': (row, 'status', 'unknown'),
        }
        if damage in changes:
            target, field, value = changes[damage]
            target[field] = value
        if damage in {'route', 'namespace'}:
            row['principal_id'] = 'automation:' + descriptor['route']
        row['payload_digest'] = 'wrong' if damage == 'digest' else admission_fingerprint(
            canonical_target=row['target_session_id'], payload={'input': payload, 'intent': row['intent']})
        db._write_sql('UPDATE session_admissions SET principal_id=?,request_id=?,status=?,payload_json=?,payload_digest=? WHERE admission_id=?',
            (row['principal_id'], row['request_id'], row['status'], json.dumps(payload), row['payload_digest'], row['admission_id']))
    mailbox._write(path, record)
    before_db, before_files = snapshot(db), files(bot.home)
    with pytest.raises((RuntimeStoreError, ValueError)):
        db.rekey_profile_state('watchdog', 'studio')
    assert snapshot(db) == before_db
    assert files(bot.home) == before_files
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', [None, 'principal_id', 'request_id', 'target_session_id', 'status'])
async def test_retired_metadata_is_checked_without_claiming_payload_proof(tmp_path, monkeypatch, damage):
    from hermes_state_terminal import ADMISSION_PREFIX
    bot = await producer(tmp_path, monkeypatch)
    db = bot.authority.db
    row = dict(db._read_one('SELECT * FROM session_admissions'))
    row['payload_json'] = '{}'  # The retirement contract erases the input, not its identity.
    if damage:
        row[damage] = 'foreign'
    db._write_sql('INSERT INTO state_meta(key,value) VALUES(?,?)',
                  (ADMISSION_PREFIX + row['admission_id'], json.dumps(row)))
    db._write_sql('DELETE FROM session_admissions')
    before_db, before_files = snapshot(db), files(bot.home)
    if damage:
        with pytest.raises(RuntimeStoreError, match='admission_conflict'):
            db.rekey_profile_state('watchdog', 'studio')
        assert snapshot(db) == before_db
        assert files(bot.home) == before_files
    else:
        db.rekey_profile_state('watchdog', 'studio')
        assert json.loads(db._read_one('SELECT value FROM state_meta WHERE key=?',
                          (ADMISSION_PREFIX + row['admission_id'],))[0]) == row
        assert rows(db, 'session_admissions') == []
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['forged_retry_without_intent', 'retry_list', 'retry_list_with_intent',
    'retry_null', 'retry_empty', 'payload_list', 'payload_null', 'payload_invalid_json', 'principal_blob',
    'retired_principal_number'])
async def test_malformed_producer_binding_has_typed_atomic_refusal(tmp_path, monkeypatch, damage):
    from hermes_state_terminal import ADMISSION_PREFIX
    bot = await producer(tmp_path, monkeypatch)
    db, key = bot.authority.db, 'a' * 32
    path = mailbox._root(bot.home) / (key + '.json')
    record = mailbox._read(path)
    retry_values = {'forged_retry_without_intent': 'forged', 'retry_list': [1],
                    'retry_list_with_intent': [1], 'retry_null': None, 'retry_empty': ''}
    if damage in retry_values:
        record['retry_admission_id'] = retry_values[damage]
        if damage == 'retry_list_with_intent':
            record['retry'] = {'identity': 'bot:' + key + ':retry'}
        mailbox._write(path, record)
    elif damage == 'retired_principal_number':
        row = dict(db._read_one('SELECT * FROM session_admissions'))
        row.update(principal_id=7, payload_json='{}')
        db._write_sql('INSERT INTO state_meta(key,value) VALUES(?,?)',
                      (ADMISSION_PREFIX + row['admission_id'], json.dumps(row)))
        db._write_sql('DELETE FROM session_admissions')
    else:
        column, value = {
            'payload_list': ('payload_json', '[]'),
            'payload_null': ('payload_json', 'null'),
            'payload_invalid_json': ('payload_json', '{'),
            'principal_blob': ('principal_id', b'automation:invalid'),
        }[damage]
        db._write_sql(f'UPDATE session_admissions SET {column}=?', (value,))
    before_db, before_files = snapshot(db), files(bot.home)
    with pytest.raises(RuntimeStoreError, match='admission_conflict|permission_denied'):
        db.rekey_profile_state('watchdog', 'studio')
    assert snapshot(db) == before_db
    assert files(bot.home) == before_files
    db.close()


@pytest.mark.asyncio
async def test_forged_retry_with_matching_intent_requires_retained_proof(tmp_path, monkeypatch):
    bot = await producer(tmp_path, monkeypatch)
    db, key = bot.authority.db, 'a' * 32
    path = mailbox._root(bot.home) / (key + '.json')
    record = mailbox._read(path)
    record['retry'] = {'identity': 'bot:' + key + ':retry'}
    record['retry_admission_id'] = 'forged-retry-id'
    mailbox._write(path, record)
    before_db, before_files = snapshot(db), files(bot.home)
    with pytest.raises(RuntimeStoreError, match='admission_conflict'):
        db.rekey_profile_state('watchdog', 'studio')
    assert snapshot(db) == before_db
    assert files(bot.home) == before_files
    db.close()


@pytest.mark.asyncio
async def test_retained_terminal_retry_binding_survives_migration(tmp_path, monkeypatch):
    from hermes_state_terminal import ADMISSION_PREFIX
    bot = await producer(tmp_path, monkeypatch, retry=True)
    db, key = bot.authority.db, 'a' * 32
    path = mailbox._root(bot.home) / (key + '.json')
    record = mailbox._read(path)
    retry_id = record['retry_admission_id']
    row = dict(db._read_one('SELECT * FROM session_admissions WHERE admission_id=?', (retry_id,)))
    row['payload_json'] = '{}'  # Only retained identity, not erased payload, is provable.
    tombstone = json.dumps(row)
    db._write_sql('INSERT INTO state_meta(key,value) VALUES(?,?)', (ADMISSION_PREFIX + retry_id, tombstone))
    db._write_sql('DELETE FROM session_admissions WHERE admission_id=?', (retry_id,))
    admissions, history = rows(db, 'session_admissions'), rows(db, 'messages')
    db.rekey_profile_state('watchdog', 'studio')
    db.rekey_profile_state('watchdog', 'studio')
    assert rows(db, 'session_admissions') == admissions
    assert rows(db, 'messages') == history
    assert db._read_one('SELECT value FROM state_meta WHERE key=?', (ADMISSION_PREFIX + retry_id,))[0] == tombstone
    assert mailbox._read(path) == {**record, 'profile_home': str(bot.home.with_name('studio'))}
    assert not db._read_all('SELECT * FROM state_meta WHERE key IN (?,?)', (JOURNAL, PENDING))
    db.close()


def test_historic_route_accepts_only_canonical_namespace_and_same_logical_conversation():
    from gateway.session_detached import detached_key
    from gateway.session_profile_mailbox import _historical_route
    sid = 'local-synthetic'
    for namespace in ('watchdog', 'studio', 'studio2', 'main', 'main~'):
        route = f'agent:{namespace}:local:dm:{sid}'
        assert _historical_route(route, sid)
        assert _historical_route(detached_key(route, sid), sid)
        assert not _historical_route(route, 'foreign')
        assert not _historical_route(detached_key(route, sid), 'foreign')
    assert not _historical_route('agent:INVALID:detached:' + sid, sid)
    assert not _historical_route('agent:studio:slack:dm:' + sid, sid)
