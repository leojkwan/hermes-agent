"""Profile rename on the owning loop, with no stale served authority during repair."""
import asyncio
import logging
import threading
from pathlib import Path

from hermes_state_runtime import RuntimeStoreError

CONTROL_WAIT_SECONDS = 30


def _require_quiet(authority):
    from gateway.session_runtime_workers import mutation_tasks, physical_workers, side_agents
    if (mutation_tasks(authority) or list(physical_workers(authority)) or side_agents(authority)
            or any(not task.done() for task in getattr(authority, '_bot_receipt_tasks', ()))
            or any(live.task is not None and not live.task.done() for live in authority.sessions.values())):
        raise RuntimeStoreError('session_busy')
    from hermes_state_mutation_guards import require_idle
    with authority.db._read_ctx() as conn:
        ids = [row[0] for row in conn.execute('SELECT id FROM sessions')]
        require_idle(authority.db, conn, ids)


class MigrationFailure(RuntimeError):
    def __init__(self, cause, *, unchanged):
        super().__init__(str(cause))
        self.unchanged = unchanged


def _migrate_stores(store, old, new):
    from gateway.runtime_ownership import exclusive_maintenance
    from gateway.session_profile_mailbox import JOURNAL, migration_pending
    from hermes_state_registry import acquire, release_or_close
    root = Path(store._routing_home)
    target = root / 'profiles' / new
    unchanged, db = True, None
    try:
        if not target.is_dir():
            raise RuntimeStoreError('not_found')
        with exclusive_maintenance([target], purpose='profile-identity-migration'):
            counts = {}
            routing_db = getattr(store, '_routing_db', None)
            if (target / 'state.db').exists():
                db = acquire(target / 'state.db')
                recovered = migration_pending(db, old, new)
                unchanged = not recovered and db._read_one('SELECT 1 FROM state_meta WHERE key=?', (JOURNAL,)) is None
                db.rekey_profile_state(old, new, validate_only=True)
            if routing_db is not None:
                routing_db.rekey_profile_state(old, new, validate_only=True)
            unchanged = False
            if db is not None:
                migration_pending(db, old, new, start=True)
                counts['profile'] = db.rekey_profile_state(old, new)
            if routing_db is not None:
                counts['routing'] = routing_db.rekey_profile_state(old, new)
            rekeyed = store.rekey_profile_routing(old, new)
            if db is not None:
                migration_pending(db, old, new, finish=True)
            return {'ok': True, 'rekeyed': rekeyed, 'db': counts, 'recovered': db is not None and recovered}
    except Exception as exc:
        raise MigrationFailure(exc, unchanged=unchanged) from exc
    finally:
        if db is not None:
            release_or_close(db)


def migration_verb(runner):
    loop = asyncio.get_running_loop()
    pending, pending_lock = {}, threading.Lock()

    async def apply(params):
        from hermes_cli.profiles import _canon_valid
        old, new = _canon_valid(params.get('old') or ''), _canon_valid(params.get('new') or '')
        if old == new or 'default' in (old, new):
            raise ValueError('distinct named profiles required')
        store = runner.session_store
        async with runner._reconcile_lock():
            known = dict(runner._served_profile_homes or {})
            if old in known:
                raise RuntimeStoreError('runtime_coordination_required', detail='old profile is still served')
            served = new in known
            if served:
                from gateway.session_authorities import authority_for_profile_id
                authority = authority_for_profile_id(runner, str(known[new]))
                if authority is None:
                    raise RuntimeStoreError('runtime_coordination_required')
                _require_quiet(authority)
                authority.retiring = True
                await runner._unserve_profile(new, known.pop(new))
                runner._record_served_profiles(getattr(runner, '_primary_profile_name', None) or 'default', list(known.items()))
            try:
                result = await asyncio.to_thread(_migrate_stores, store, old, new)
            except MigrationFailure as exc:
                if served and exc.unchanged:
                    known[new] = Path(store._routing_home) / 'profiles' / new
                    restored = await runner._apply_profile_changes(known, [new], [], [], reason='identity-migration-refused')
                    return {'ok': False, 'error': str(exc), 'restored_profile': new in restored['served_profiles']}
                from gateway.run_runtime import park_profile
                park_profile(runner, new, 'Profile identity migration is incomplete; retry migrate-identity')
                raise
            if served or result.pop('recovered', False):
                known[new] = Path(store._routing_home) / 'profiles' / new
                restored = await runner._apply_profile_changes(known, [new], [], [], reason='identity-migration')
                if new not in restored['served_profiles']:
                    return {'ok': False, 'error': 'identity migrated; profile restart failed'}
            return result

    def forget(key, future):
        with pending_lock:
            if pending.get(key) is future:
                pending.pop(key)

    def handler(params):
        key = (str(params.get('old') or '').strip().lower(), str(params.get('new') or '').strip().lower())
        with pending_lock:
            future = pending.get(key)
            if future is None:
                future = asyncio.run_coroutine_threadsafe(apply(params), loop)
                pending[key] = future
                future.add_done_callback(lambda done: loop.call_soon_threadsafe(forget, key, done))
        try:
            return future.result(timeout=CONTROL_WAIT_SECONDS)
        except TimeoutError:
            return {'ok': False, 'pending': True, 'error': 'profile identity migration is still running'}
        except Exception as exc:
            logging.getLogger(__name__).warning('Profile identity migration failed', exc_info=True)
            return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}

    return handler
