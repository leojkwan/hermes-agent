"""Recoverable ownership transfer for settled Bot mailbox files, under their owner's lock."""
import copy
import json
from contextlib import contextmanager
from pathlib import Path

from hermes_state_local import POLICY_PREFIX
from hermes_state_runtime import RuntimeStoreError, _json

JOURNAL = 'gateway.profile_rename.v1'
PENDING = 'gateway.profile_rename.pending.v1'


def require_identity_ready(db):
    with db._read_ctx() as conn:
        if conn.execute('SELECT 1 FROM state_meta WHERE key IN (?,?)', (JOURNAL, PENDING)).fetchone():
            raise RuntimeStoreError('runtime_coordination_required', detail='profile identity migration must finish')


def migration_pending(db, old, new, *, start=False, finish=False):
    expected = _json({'old': old, 'new': new})
    def write(conn):
        row = conn.execute('SELECT value FROM state_meta WHERE key=?', (PENDING,)).fetchone()
        if row is not None and row[0] != expected:
            raise RuntimeStoreError('admission_conflict')
        if finish:
            conn.execute('DELETE FROM state_meta WHERE key=?', (PENDING,))
        elif start:
            conn.execute('INSERT OR IGNORE INTO state_meta(key,value) VALUES(?,?)', (PENDING, expected))
        return row is not None
    return db._execute_write(write)


def _owners(conn):
    owners = {}
    for (raw,) in conn.execute('SELECT value FROM state_meta WHERE key GLOB ?', (POLICY_PREFIX + '*',)):
        receipt = json.loads(raw)
        for sid in receipt.get('lineage', [receipt['session_id']]):
            owners[sid] = receipt
    return owners


def _historical_route(route, sid):
    from gateway.session_detached import own_detached_key
    from hermes_constants import PROFILE_ID_RE
    if not isinstance(route, str):
        return False
    parts = route.split(':')
    return (len(parts) >= 2 and parts[0] == 'agent'
            and (parts[1] == 'main~' or PROFILE_ID_RE.fullmatch(parts[1]) is not None)
            and (parts[2:] == ['local', 'dm', sid] or own_detached_key(route, sid)))


def _check_admission(conn, admission_id, record, receipt, identity):
    from gateway.session_automation import local_automation_mismatch
    from hermes_state_keys import admission_fingerprint
    from hermes_state_terminal import terminal_admission
    row = conn.execute('SELECT * FROM session_admissions WHERE admission_id=?', (admission_id,)).fetchone()
    retired = row is None
    row = terminal_admission(conn, admission_id) if retired else dict(row)
    if row is None:
        return  # Historical missing-row compatibility; no payload proof remains.
    sid = record['session_id']
    if not isinstance(row['principal_id'], str):
        raise RuntimeStoreError('admission_conflict')
    route = row['principal_id'].removeprefix('automation:')
    if (row['status'] != 'terminal' or row['target_session_id'] != sid or row['request_id'] != identity
            or not row['principal_id'].startswith('automation:') or not _historical_route(route, sid)):
        raise RuntimeStoreError('admission_conflict')
    if retired:
        return  # Retirement erased the payload; only retained identity metadata is provable.
    try:
        payload = json.loads(row['payload_json'])
    except (TypeError, ValueError) as exc:
        raise RuntimeStoreError('admission_conflict') from exc
    row['payload'] = payload
    if (local_automation_mismatch(row, sid, route) or payload['text'] != record['message']
            or payload['local_automation_v1'].get('target') not in receipt.get('lineage', [receipt['session_id']])
            or row['payload_digest'] != admission_fingerprint(canonical_target=sid,
                payload={'input': payload, 'intent': row['intent']})):
        raise RuntimeStoreError('admission_conflict')


def _check_record(conn, path, record, owners, old_home):
    from gateway.session_bot import _record_shape_error
    if _record_shape_error(path, record) or record['status'] not in {'settled', 'failed', 'cancelled'}:
        raise RuntimeStoreError('unknown_execution', detail='Bot mailbox is not settled')
    pins = [record] if 'profile_home' in record else []
    if 'owner' in record:
        pins.append(record['owner'])
    if not pins:
        raise RuntimeStoreError('profile_mismatch')
    for pin in pins:
        receipt = owners.get(pin.get('session_id'))
        if pin.get('profile_home') != old_home or receipt is None or receipt['profile_id'] != old_home:
            raise RuntimeStoreError('profile_mismatch')
        if 'principal_id' in pin and pin['principal_id'] != receipt['principal_id']:
            raise RuntimeStoreError('permission_denied')
    if admission_id := record.get('admission_id'):
        from gateway.session_bot import bot_request_identity, _retry_identity
        receipt = owners[record['session_id']]
        _check_admission(conn, admission_id, record, receipt, bot_request_identity(record['delivery_id']))
        identity = _retry_identity(record['delivery_id'])
        retry = record.get('retry')
        if retry is not None and (not isinstance(retry, dict) or retry.get('identity') != identity):
            raise RuntimeStoreError('admission_conflict')
        rows = conn.execute('SELECT admission_id FROM session_admissions WHERE target_session_id=? AND request_id=?',
                            (record['session_id'], identity)).fetchall()
        retry_id = record.get('retry_admission_id')
        if 'retry_admission_id' in record and (not isinstance(retry_id, str) or not retry_id
                                               or not isinstance(retry, dict) or retry.get('identity') != identity):
            raise RuntimeStoreError('admission_conflict')
        if len(rows) > 1 or (retry_id and rows and retry_id != rows[0][0]):
            raise RuntimeStoreError('admission_conflict')
        if rows or retry_id:
            _check_admission(conn, rows[0][0] if rows else retry_id, record, receipt, identity)


def _validated_journal(conn, journal, old_home, new_home):
    if (journal['old_home'], journal['new_home']) != (old_home, new_home):
        raise RuntimeStoreError('admission_conflict')
    owners = _owners(conn)
    old_owners = {sid: {**receipt, 'profile_id': old_home} for sid, receipt in owners.items()
                  if receipt['profile_id'] == new_home}
    from tools.bot_live_delivery import _delivery_id
    seen = set()
    for change in journal['files']:
        key = _delivery_id(change['id'])
        if key in seen:
            raise RuntimeStoreError('admission_conflict')
        seen.add(key)
        before, after = change['before'], change['after']
        _check_record(conn, Path(key + '.json'), before, old_owners, old_home)
        expected = copy.deepcopy(before)
        if 'profile_home' in expected:
            expected['profile_home'] = new_home
        if 'owner' in expected:
            expected['owner']['profile_home'] = new_home
        if after != expected:
            raise RuntimeStoreError('admission_conflict')
    return journal


class MailboxTransfer:
    def __init__(self, db, root, old_home, new_home):
        self.db, self.root, self.old_home, self.new_home = db, root, old_home, new_home
        self.journal = None

    def stage(self, conn):
        from tools.bot_live_delivery import _read
        row = conn.execute('SELECT value FROM state_meta WHERE key=?', (JOURNAL,)).fetchone()
        if row is not None:
            self.journal = _validated_journal(conn, json.loads(row[0]), self.old_home, self.new_home)
            return
        if self.root is None:
            return
        changes, owners = [], _owners(conn)
        for path in sorted(self.root.glob('*.json')):
            record = _read(path)
            if record is None:
                raise RuntimeStoreError('storage_unavailable')
            # A complete prior migration leaves settled files owned by the new home.
            pins = [record] if 'profile_home' in record else [record.get('owner', {})]
            if all(pin.get('profile_home') == self.new_home for pin in pins):
                _check_record(conn, path, record, owners, self.new_home)
                continue
            _check_record(conn, path, record, owners, self.old_home)
            after = copy.deepcopy(record)
            if 'profile_home' in after:
                after['profile_home'] = self.new_home
            if 'owner' in after:
                after['owner']['profile_home'] = self.new_home
            changes.append({'id': path.stem, 'before': record, 'after': after})
        if changes:
            self.journal = {'old_home': self.old_home, 'new_home': self.new_home, 'files': changes}
            conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (JOURNAL, _json(self.journal)))

    def finish(self):
        from tools.bot_live_delivery import _delivery_id, _read, _write
        if self.journal is None:
            return
        if self.root is None:
            raise RuntimeStoreError('storage_unavailable')
        paths = []
        for change in self.journal['files']:
            path = self.root / (_delivery_id(change['id']) + '.json')
            current = _read(path)
            if current not in (change['before'], change['after']):
                raise RuntimeStoreError('admission_conflict')
            paths.append((path, change))
        for path, change in paths:
            if _read(path) != change['after']:
                _write(path, change['after'])
            if _read(path) != change['after']:
                raise RuntimeStoreError('storage_unavailable')
        def clear_journal(conn):
            row = conn.execute('SELECT value FROM state_meta WHERE key=?', (JOURNAL,)).fetchone()
            if row is None or json.loads(row[0]) != self.journal:
                raise RuntimeStoreError('admission_conflict')
            conn.execute('DELETE FROM state_meta WHERE key=?', (JOURNAL,))
        self.db._execute_write(clear_journal)


@contextmanager
def mailbox_transfer(db, old, new):
    from tools.bot_live_delivery import _locked, _root
    home = Path(db.db_path).resolve().parent
    if home.parent.name != 'profiles' or home.name not in {old, new}:
        yield MailboxTransfer(db, None, '', '')
        return
    old_home, new_home = str(home.with_name(old)), str(home.with_name(new))
    if not _root(home).exists():
        yield MailboxTransfer(db, None, old_home, new_home)
        return
    with _locked(home) as root:
        yield MailboxTransfer(db, root, old_home, new_home)
