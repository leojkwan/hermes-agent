"""Validate and retarget local receipts inside the existing profile rekey transaction."""
import copy
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

from hermes_state_local import POLICY_PREFIX
from hermes_state_local_identity import bind_creation_retry, creation_profile, validate_creation_identity
from hermes_state_runtime import RuntimeStoreError, _json


def _validate_reference_metadata(source):
    paths = {}
    for field, pattern in (('entries', r'sha256:[0-9a-f]{16}'), ('bound', r'[0-9a-f]{64}')):
        rows = source.get(field, []) if field == 'bound' else source[field]
        if not isinstance(rows, list):
            raise RuntimeStoreError('launch_credentials_unavailable')
        seen = set()
        for row in rows:
            if not isinstance(row, list) or len(row) != 2:
                raise RuntimeStoreError('launch_credentials_unavailable')
            path, digest = row
            if (not isinstance(path, list) or not path or any(type(part) not in (str, int, type(None)) for part in path)
                    or not isinstance(digest, str) or re.fullmatch(pattern, digest) is None or tuple(path) in seen):
                raise RuntimeStoreError('launch_credentials_unavailable')
            seen.add(tuple(path))
        paths[field] = seen
    if ('bound' in source and paths['entries'] != paths['bound']) or re.fullmatch(r'[0-9a-f]{64}', source['policy']) is None:
        raise RuntimeStoreError('launch_credentials_unavailable')


def _retarget_config_ref(policy, old_home, new_home, sid):
    from gateway.session_policy import restore_policy
    from gateway.session_policy_credentials import PREFIX, _check_scope
    restored = restore_policy(policy)
    ref = restored.config_secret_ref
    if ref is None or not ref.startswith(PREFIX):
        return
    source = json.loads(ref[len(PREFIX):])
    _validate_reference_metadata(source)
    if source['session'] != sid:
        raise RuntimeStoreError('launch_credentials_unavailable')
    _check_scope(SimpleNamespace(profile_id=old_home), source, restored, Path(old_home), ())
    source.update(home=new_home, profile=new_home)
    policy['config_secret_ref'] = PREFIX + json.dumps(source, sort_keys=True)


def _validate_receipt(db, conn, key, receipt, old, old_home, new):
    from gateway.config import Platform
    from gateway.session import SessionEntry, SessionSource
    from gateway.session_policy import restore_policy
    from hermes_state_local_lineage import validate_local_lineage
    from hermes_state_mutation_guards import require_idle
    sid = receipt['session_id']
    if key != POLICY_PREFIX + sid or receipt['profile_id'] != old_home:
        raise RuntimeStoreError('profile_mismatch')
    validate_creation_identity(receipt)
    entry = SessionEntry.from_dict(receipt['entry'])
    source = SessionSource(platform=Platform.LOCAL, chat_id=sid, user_id=receipt['principal_id'],
                           chat_type='dm', profile=old)
    route = 'agent:' + ('main~' if old == 'main' else old) + ':local:dm:' + sid
    if receipt['route'] != route or entry.session_key != route:
        raise RuntimeStoreError('storage_unavailable')
    origin = entry.origin.to_dict()
    if 'profile' not in receipt['entry']['origin']:
        origin['profile'] = old
    if origin != source.to_dict():
        raise RuntimeStoreError('profile_mismatch')
    row = conn.execute('SELECT session_key,chat_id,user_id,origin_json FROM sessions WHERE id=?', (entry.session_id,)).fetchone()
    new_route = 'agent:' + ('main~' if new == 'main' else new) + ':local:dm:' + sid
    if row is None or tuple(row)[:3] not in ((route, sid, receipt['principal_id']),
                                           (new_route, sid, receipt['principal_id'])):
        raise RuntimeStoreError('storage_unavailable')
    stored_origin = json.loads(row['origin_json'])
    expected_origin = source.to_dict()
    expected_origin['profile'] = new if row['session_key'] == new_route else old
    if stored_origin != expected_origin:
        raise RuntimeStoreError('profile_mismatch')
    validate_local_lineage(conn, receipt)
    require_idle(db, conn, receipt.get('lineage', [sid]))
    restore_policy(receipt['policy'])


def rekey_local_receipts(db, conn, old, new):
    home = Path(db.db_path).resolve().parent
    # A routing index has other profiles' rows but never owns their private receipts.
    root = home.parent if home.parent.name == 'profiles' else home
    profiles = root if home.parent.name == 'profiles' else root / 'profiles'
    old_home, new_home = str(profiles / old), str(profiles / new)
    old_ns, new_ns = 'agent:' + ('main~' if old == 'main' else old) + ':', 'agent:' + ('main~' if new == 'main' else new) + ':'
    from hermes_state_mutation_guards import require_idle
    if str(home) in (old_home, new_home):
        require_idle(db, conn, [row[0] for row in conn.execute('SELECT id FROM sessions')])
        now = time.time()
        for table in ('compression_locks', 'session_turn_leases'):
            if conn.execute(f'SELECT 1 FROM {table} WHERE expires_at>? LIMIT 1', (now,)).fetchone():
                raise RuntimeStoreError('runtime_coordination_required')
    changed = []
    for key, raw in conn.execute('SELECT key,value FROM state_meta WHERE key GLOB ?', (POLICY_PREFIX + '*',)).fetchall():
        receipt = json.loads(raw)
        if not isinstance(receipt, dict):
            raise RuntimeStoreError('storage_unavailable')
        if receipt.get('profile_id') != old_home:
            if str(receipt.get('route', '')).startswith(old_ns):
                raise RuntimeStoreError('profile_mismatch')
            continue
        if str(home) not in (old_home, new_home):
            raise RuntimeStoreError('profile_mismatch')
        _validate_receipt(db, conn, key, receipt, old, old_home, new)
        updated = copy.deepcopy(receipt)
        updated['creation_profile_id'] = creation_profile(receipt)
        updated['profile_id'] = new_home
        updated['route'] = new_ns + receipt['route'][len(old_ns):]
        updated['entry']['session_key'] = updated['route']
        updated['entry']['origin']['profile'] = new
        _retarget_config_ref(updated['policy'], old_home, new_home, receipt['session_id'])
        bind_creation_retry(conn, updated)
        conn.execute('UPDATE state_meta SET value=? WHERE key=?', (_json(updated), key))
        if 'legacy_session_id' in receipt:
            from hermes_state_local_migration import LEGACY_PREFIX
            binding = {k: updated[k] for k in ('session_id', 'profile_id', 'principal_id', 'legacy_session_id')}
            conn.execute('UPDATE state_meta SET value=? WHERE key=?', (_json(binding), LEGACY_PREFIX + receipt['session_id']))
        changed.append(updated)
    return changed
