"""Actual inbound, HTTP and executor lifetimes close the safe pause check/use race."""
import asyncio
from contextvars import ContextVar
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.platforms import api_server, api_server_runs
from tests.gateway.restart_test_helpers import make_restart_runner


@pytest.fixture
def fence(monkeypatch):
    from hermes_cli import backend_retirement
    gate = backend_retirement.RetirementFence()
    monkeypatch.setattr(backend_retirement, 'retirement', gate)
    assert api_server_runs.api_worker_live_count() == 0
    yield gate
    assert gate.active_count() == 0
    assert api_server_runs.api_worker_live_count() == 0


@pytest.mark.asyncio
async def test_real_registry_and_inbound_prefix_stay_visible_until_cancel(fence):
    runner, _ = make_restart_runner()
    assert not isinstance(runner._running_agents, dict)  # The production SessionFieldView.
    started = asyncio.Event()
    hold = asyncio.Event()

    async def admit(event):
        started.set()
        await hold.wait()
        return None

    runner._hm_admit_event = admit
    task = asyncio.create_task(runner._handle_message(object()))
    await asyncio.wait_for(started.wait(), 5)
    assert fence.active_count() == 1
    assert not fence.prepare(idle_predicate=lambda: True)['ok']
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fence.active_count() == 0
    permit = fence.prepare(idle_predicate=lambda: True)['token']
    assert await runner._handle_message(object()) is None
    assert fence.cancel(permit)['ok']


@pytest.mark.asyncio
async def test_http_fence_outlives_pending_handoff_and_cancellation(fence):
    adapter = SimpleNamespace(_check_auth=lambda req: None, _draining_response=lambda: None,
                              _pending_agent_requests=0)
    request = SimpleNamespace(path='/v1/chat/completions', headers={})
    handed_off = asyncio.Event()
    hold = asyncio.Event()

    @api_server._admit_api_agent_request
    async def handle(self, request):
        reservation = api_server._api_agent_request_reservation.get()
        api_server._release_pending_api_work(self, reservation)
        handed_off.set()
        await hold.wait()

    task = asyncio.create_task(handle(adapter, request))
    await asyncio.wait_for(handed_off.wait(), 5)
    assert adapter._pending_agent_requests == 0 and fence.active_count() == 1
    assert not fence.prepare(idle_predicate=lambda: True)['ok']
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    permit = fence.prepare(idle_predicate=lambda: True)['token']
    assert (await handle(adapter, request)).status == 503
    assert adapter._pending_agent_requests == 0
    fence.cancel(permit)


@pytest.mark.asyncio
async def test_started_executor_survives_future_cancel_and_preserves_context(fence):
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    finished = asyncio.Event()
    release = threading.Event()
    scope = ContextVar('synthetic-profile', default='default')
    token = scope.set('named-profile')
    observed = []

    def body():
        observed.append(scope.get())
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return 'done'

    def submit(executor, fn):
        def observed_worker():
            try:
                return fn()
            finally:
                loop.call_soon_threadsafe(finished.set)
        return loop.run_in_executor(executor, observed_worker)
    future = api_server_runs._submit_api_worker(SimpleNamespace(run_in_executor=submit), body)
    scope.reset(token)
    # This worker's actual finally runs before the finishing callback in the same executor queue.
    try:
        await asyncio.wait_for(started.wait(), 5)
        future.cancel()
        await asyncio.sleep(0)
        assert api_server_runs.api_worker_live_count() == 1 and fence.active_count() == 1
        assert not fence.prepare(idle_predicate=lambda: True)['ok']
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 5)
    assert observed == ['named-profile']


@pytest.mark.asyncio
async def test_queued_executor_cancel_and_submit_failure_never_leak_or_run(fence):
    loop = asyncio.get_running_loop()
    queued = []
    future = loop.create_future()
    fake = SimpleNamespace(run_in_executor=lambda executor, fn: queued.append(fn) or future)
    called = []
    assert api_server_runs._submit_api_worker(fake, lambda: called.append(True)) is future
    future.cancel()
    await asyncio.sleep(0)
    assert api_server_runs.api_worker_live_count() == 0 and fence.active_count() == 0
    queued[0]()
    assert called == []

    def refuse(*args):
        raise RuntimeError('synthetic executor unavailable')
    with pytest.raises(RuntimeError, match='unavailable'):
        api_server_runs._submit_api_worker(SimpleNamespace(run_in_executor=refuse), lambda: None)
    assert api_server_runs.api_worker_live_count() == 0 and fence.active_count() == 0


@pytest.mark.asyncio
async def test_internal_wake_reserves_before_session_resolution(fence, monkeypatch):
    entered = asyncio.Event()
    hold = asyncio.Event()

    async def resolve(self, sid):
        entered.set()
        await hold.wait()

    monkeypatch.setattr(api_server_runs, '_resolve_live_session_id', resolve)
    # Existing profile scope seam is supplied, but the actual wake body reaches resolution.
    adapter = SimpleNamespace(_draining_response=lambda: None, _concurrency_limited_response=lambda: None)
    from contextlib import nullcontext
    scope = SimpleNamespace(_api_request_profile=ContextVar('wake-profile', default='default'))
    task = asyncio.create_task(api_server_runs.run_internal_session_turn(
        adapter, session_id='owned', text='wake', profile='default', _api_server=scope))
    await asyncio.wait_for(entered.wait(), 5)
    assert fence.active_count() == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize('pending,inflight', [(-1, 1), (True, 0), ('0', 0)])
def test_strict_api_count_never_coerces_invalid_metadata(pending, inflight):
    adapter = object.__new__(api_server.APIServerAdapter)
    adapter._pending_agent_requests = pending
    adapter._inflight_agent_runs = inflight
    adapter._active_run_tasks = {}
    with pytest.raises(RuntimeError, match='invalid'):
        adapter.active_agent_work_count(strict=True)


@pytest.mark.asyncio
async def test_canceled_inbound_keeps_actual_gateway_worker_visible(fence):
    loop = asyncio.get_running_loop()
    runner, _ = make_restart_runner()
    started = asyncio.Event()
    finished = asyncio.Event()
    release = threading.Event()
    runner._running_cron_job_count = lambda: 0
    runner._deferred_agent_workers = {}
    runner.stop = AsyncMock()
    with ThreadPoolExecutor(max_workers=1) as pool:
        runner._get_executor = lambda: pool
        def work():
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5)
        async def admit(event):
            await runner._run_in_executor_with_context(work)
            return None
        runner._hm_admit_event = admit
        inbound = asyncio.create_task(runner._handle_message(object()))
        try:
            await asyncio.wait_for(started.wait(), 5)
            inbound.cancel()
            with pytest.raises(asyncio.CancelledError):
                await inbound
            assert not runner._running_agents and fence.active_count() == 1
            assert runner.request_restart(graceful_only=True, via_service=True, timeout_seconds=0)
            await runner._restart_task
            runner.stop.assert_not_awaited()
            assert runner.graceful_restart_status()['reason'] == 'timeout'
        finally:
            release.set()
            pool.submit(lambda: loop.call_soon_threadsafe(finished.set))
            await asyncio.wait_for(finished.wait(), 5)
    assert fence.active_count() == 0


@pytest.mark.asyncio
async def test_cron_detach_cancel_holds_thread_and_profile_context(fence, monkeypatch, tmp_path):
    loop = asyncio.get_running_loop()
    adapter = object.__new__(api_server.APIServerAdapter)
    adapter._pending_agent_requests = 0
    adapter.gateway_runner = SimpleNamespace(adapters={})
    adapter._draining_response = lambda: None
    tasks = []
    adapter._track_background_task = lambda task, **kwargs: tasks.append(task)
    monkeypatch.setattr('plugins.cron_providers.chronos.verify.get_fire_verifier',
                        lambda: lambda **kwargs: {'purpose': 'cron_fire'})
    profiles = []
    pool = ThreadPoolExecutor(max_workers=1)
    real_submit = api_server_runs._submit_api_worker
    def submit(loop, fn):
        fixture_loop = SimpleNamespace(run_in_executor=lambda executor, body: loop.run_in_executor(pool, body))
        return real_submit(fixture_loop, fn)
    monkeypatch.setattr(api_server_runs, '_submit_api_worker', submit)
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override, get_hermes_home
    homes = {}
    for profile in ('profile-a', 'profile-b'):
        homes[profile] = tmp_path / profile
        homes[profile].mkdir()
    for profile in ('profile-a', 'profile-b', 'profile-a'):
        started = asyncio.Event()
        finished = asyncio.Event()
        release = threading.Event()
        # The real handler uses the split claim followed by detached actual executor work.
        class Provider:
            def claim_fire(self, job_id):
                profiles.append(('claim', api_server._api_request_profile.get(), get_hermes_home()))
                return {'id': job_id}
            def fire_claimed(self, job, **kwargs):
                profiles.append(('fire', api_server._api_request_profile.get(), get_hermes_home()))
                loop.call_soon_threadsafe(started.set)
                assert release.wait(5)
        monkeypatch.setattr('cron.scheduler_provider.resolve_cron_scheduler', Provider)
        token = api_server._api_request_profile.set(profile)
        home_token = set_hermes_home_override(homes[profile])
        request = SimpleNamespace(headers={'Authorization': 'Bearer synthetic'}, app={})
        async def json_body():
            return {'job_id': 'owned-synthetic'}
        request.json = json_body
        try:
            response = await adapter._handle_cron_fire(request)
            assert response.status == 202
            await asyncio.wait_for(started.wait(), 5)
            tasks[-1].cancel()
            await asyncio.sleep(0)
            assert api_server_runs.api_worker_live_count() == 1
            assert not fence.prepare(idle_predicate=lambda: True)['ok']
        finally:
            api_server._api_request_profile.reset(token)
            release.set()
            reset_hermes_home_override(home_token)
            pool.submit(lambda: loop.call_soon_threadsafe(finished.set))
            await asyncio.wait_for(finished.wait(), 5)
    pool.shutdown(wait=True)
    assert profiles == [(kind, profile, homes[profile]) for profile in ('profile-a', 'profile-b', 'profile-a')
                        for kind in ('claim', 'fire')]
