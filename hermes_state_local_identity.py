"""Immutable local creation identity and the current owner's retry index."""
import json

from hermes_state_keys import local_identity
from hermes_state_runtime import RuntimeStoreError

RETRY_PREFIX = 'gateway.local_retry.v1:'


def creation_profile(receipt):
    return receipt.get('creation_profile_id', receipt['profile_id'])


def validate_creation_identity(receipt):
    expected = receipt.get('legacy_session_id') or local_identity(
        creation_profile(receipt), receipt['principal_id'], receipt['request_id'])
    if receipt['session_id'] != expected:
        raise RuntimeStoreError('storage_unavailable')


def resolve_creation_id(db, profile_id, principal_id, request_id):
    candidate = local_identity(profile_id, principal_id, request_id)
    with db._read_ctx() as conn:
        row = conn.execute('SELECT value FROM state_meta WHERE key=?', (RETRY_PREFIX + candidate,)).fetchone()
        if row is None:
            return candidate
        try:
            sid = json.loads(row[0])['session_id']
            from hermes_state_local import POLICY_PREFIX
            stored = conn.execute('SELECT value FROM state_meta WHERE key=?', (POLICY_PREFIX + sid,)).fetchone()
            receipt = json.loads(stored[0])
            validate_creation_identity(receipt)
            if receipt['session_id'] != sid:
                raise ValueError('retry target mismatch')
            if (receipt['profile_id'], receipt['principal_id'], receipt['request_id']) != (
                    profile_id, principal_id, request_id):
                raise ValueError('foreign retry mapping')
            if candidate != sid and conn.execute('SELECT 1 FROM sessions WHERE id=?', (candidate,)).fetchone():
                raise ValueError('retry identity collision')
            return sid
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeStoreError('storage_unavailable') from exc


def bind_creation_retry(conn, receipt):
    from hermes_state_local import POLICY_PREFIX
    if 'legacy_session_id' in receipt:
        return
    sid = receipt['session_id']
    candidate = local_identity(receipt['profile_id'], receipt['principal_id'], receipt['request_id'])
    if candidate != sid and (conn.execute('SELECT 1 FROM sessions WHERE id=?', (candidate,)).fetchone()
                             or conn.execute('SELECT 1 FROM state_meta WHERE key=?', (POLICY_PREFIX + candidate,)).fetchone()):
        raise RuntimeStoreError('admission_conflict')
    key, encoded = RETRY_PREFIX + candidate, json.dumps({'session_id': sid}, sort_keys=True)
    row = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
    if row is not None and row[0] != encoded:
        raise RuntimeStoreError('admission_conflict')
    conn.execute('INSERT OR IGNORE INTO state_meta(key,value) VALUES(?,?)', (key, encoded))
