"""An explicitly selected safe pause never escalates an incomplete drain."""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from tests.gateway.restart_test_helpers import make_restart_runner


@pytest.fixture
def safe_runner(monkeypatch):
    from hermes_cli import backend_retirement
    runner, _ = make_restart_runner()
    fence = backend_retirement.RetirementFence()
    monkeypatch.setattr(backend_retirement, 'retirement', fence)
    runner._running_cron_job_count = lambda: 0
    runner._deferred_agent_workers = {}
    runner.stop = AsyncMock()
    runner._launch_detached_restart_command = AsyncMock()
    runner._mark_api_runs_shutdown_requested = MagicMock()
    return runner, fence


@pytest.mark.asyncio
async def test_busy_zero_budget_aborts_and_restores_intake(safe_runner):
    runner, fence = safe_runner
    agent = MagicMock()
    runner._running_agents['owned-turn'] = agent
    assert runner.request_restart(graceful_only=True, via_service=True, timeout_seconds=0)
    await runner._restart_task
    assert runner.graceful_restart_status()['state'] == 'aborted'
    assert runner._running and not runner._draining
    assert not runner._restart_requested and not runner._restart_task_started
    runner.stop.assert_not_awaited()
    runner._launch_detached_restart_command.assert_not_awaited()
    runner._mark_api_runs_shutdown_requested.assert_not_called()
    agent.interrupt.assert_not_called()
    assert fence.acquire()
    fence.release()


@pytest.mark.asyncio
async def test_idle_safe_commit_closes_admission_before_stop(safe_runner):
    runner, fence = safe_runner
    async def stop(**kwargs):
        assert kwargs == {'restart': True, 'detached_restart': False, 'service_restart': True}
        assert not fence.acquire()
    runner.stop.side_effect = stop
    assert runner.request_restart(graceful_only=True, via_service=True)
    await runner._restart_task
    assert runner.graceful_restart_status()['state'] == 'stopped'
    runner.stop.assert_awaited_once()
    runner._launch_detached_restart_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_positive_timeout_keeps_work_and_never_stops(safe_runner):
    runner, fence = safe_runner
    assert fence.acquire()
    try:
        assert runner.request_restart(graceful_only=True, via_service=True, timeout_seconds=2)
        await asyncio.wait_for(runner._restart_task, timeout=5)
        assert runner.graceful_restart_status()['reason'] == 'timeout'
        runner.stop.assert_not_awaited()
        assert runner._running and not runner._draining and fence.active_count() == 1
    finally:
        fence.release()


@pytest.mark.asyncio
async def test_cancellation_is_operation_bound_and_restores_prior_intake(safe_runner):
    runner, fence = safe_runner
    runner._draining = True  # An independent external drain remains intact.
    assert fence.acquire()
    try:
        assert runner.request_restart(graceful_only=True, via_service=True, timeout_seconds=30)
        operation = runner.graceful_restart_status()['operation_id']
        assert not runner.cancel_graceful_restart('another-attempt')
        assert runner.cancel_graceful_restart(operation)
        await runner._restart_task
        assert runner.graceful_restart_status()['reason'] == 'cancelled'
        assert runner._draining and runner._running and not runner._restart_requested
        runner.stop.assert_not_awaited()
    finally:
        fence.release()


@pytest.mark.asyncio
async def test_independent_shutdown_ownership_is_never_restored_over(safe_runner):
    runner, fence = safe_runner
    assert fence.acquire()
    try:
        runner.request_restart(graceful_only=True, via_service=True, timeout_seconds=30)
        runner._stop_task = asyncio.create_task(asyncio.sleep(0))
        runner._running = False
        runner._draining = True
        await runner._restart_task
        assert not runner._running and runner._draining
        runner.stop.assert_not_awaited()
        await runner._stop_task
    finally:
        fence.release()


@pytest.mark.asyncio
@pytest.mark.parametrize('signal_stop', [False, True])
async def test_independent_real_stop_retains_ordinary_exit_verdict(safe_runner, tmp_path, monkeypatch, signal_stop):
    from gateway import run, status
    runner, fence = safe_runner
    monkeypatch.setattr(run, '_hermes_home', tmp_path)
    marker = tmp_path / 'planned-restart.json'
    monkeypatch.setattr(run, '_planned_restart_notification_path', lambda: marker)
    monkeypatch.setattr(run, '_shutdown_gateway_health_export', MagicMock())
    monkeypatch.setattr(status, 'remove_pid_file', MagicMock())
    monkeypatch.setattr(status, 'release_gateway_runtime_lock', MagicMock())
    monkeypatch.setattr(status, 'flush_runtime_status_async', AsyncMock(return_value=True))
    cls = type(runner)
    async def teardown(self):
        self._running = False
        self._signal_initiated_shutdown = signal_stop
        ctx = self._StopContext(deferred_count=lambda: 0, started_at=time.monotonic())
        await self._stop_persist_exit_state(ctx)
    monkeypatch.setattr(cls, '_stop_impl', teardown)
    assert fence.acquire()
    try:
        assert runner.request_restart(graceful_only=True, via_service=True, timeout_seconds=30)
        assert not runner._restart_requested and not runner._restart_via_service
        await cls.stop(runner)  # Actual stop ownership and actual exit-state persistence.
        await runner._restart_task
        assert runner._exit_code != 75
        assert not marker.exists() and (tmp_path / '.clean_shutdown').exists()
        runner._update_runtime_status.assert_called_with('running' if signal_stop else 'stopped', None)
        assert runner.graceful_restart_status()['reason'] == 'shutdown-owned-elsewhere'
    finally:
        fence.release()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['cron', 'api', 'deferred'])
async def test_unknown_work_fails_closed(safe_runner, kind):
    runner, _ = safe_runner
    def unreadable(*args, **kwargs):
        raise RuntimeError('synthetic unavailable counter')
    if kind == 'cron':
        runner._running_cron_job_count = unreadable
    elif kind == 'api':
        runner.adapters[Platform.API_SERVER] = MagicMock(active_agent_work_count=unreadable)
    else:
        runner._deferred_agent_workers = {'worker': MagicMock(done=unreadable)}
    runner.request_restart(graceful_only=True, via_service=True)
    await runner._restart_task
    assert runner.graceful_restart_status()['reason'] == 'work-unreadable'
    assert runner._running and not runner._draining
    runner.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_final_permit_aborts_without_stop(safe_runner, monkeypatch):
    runner, fence = safe_runner
    clock = [10.0]
    fence._now = lambda: clock[0]
    commit = fence.commit
    def expire(token):
        clock[0] += 30
        return commit(token)
    monkeypatch.setattr(fence, 'commit', expire)
    runner.request_restart(graceful_only=True, via_service=True)
    await runner._restart_task
    assert runner.graceful_restart_status()['reason'] == 'permit-expired'
    assert runner._running and not runner._draining
    runner.stop.assert_not_awaited()
    assert fence.acquire()
    fence.release()


@pytest.mark.asyncio
async def test_safe_mode_refuses_detached_force_helper(safe_runner):
    runner, _ = safe_runner
    assert not runner.request_restart(graceful_only=True, detached=True)
    runner.stop.assert_not_awaited()
    runner._launch_detached_restart_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_work_between_sample_and_freeze_aborts(safe_runner, monkeypatch):
    runner, fence = safe_runner
    real_prepare = fence.prepare
    def concurrent_admission(**kwargs):
        assert fence.acquire()
        return real_prepare(**kwargs)
    monkeypatch.setattr(fence, 'prepare', concurrent_admission)
    runner.request_restart(graceful_only=True, via_service=True)
    try:
        await runner._restart_task
        assert runner.graceful_restart_status()['reason'] == 'idle-proof-refused'
        runner.stop.assert_not_awaited()
        assert not runner._draining and runner._running
    finally:
        fence.release()


@pytest.mark.asyncio
async def test_task_cancellation_after_wait_starts_restores_intake(safe_runner):
    runner, fence = safe_runner
    entered = asyncio.Event()
    def busy():
        entered.set()
        return 1
    runner._strict_restart_work_count = busy
    runner.request_restart(graceful_only=True, via_service=True, timeout_seconds=30)
    await asyncio.wait_for(entered.wait(), 5)
    runner._restart_task.cancel()
    await runner._restart_task
    assert runner.graceful_restart_status()['reason'] == 'cancelled'
    assert not runner._draining and runner._running
    runner.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_cancelled_before_first_step_restores_intake(safe_runner):
    runner, _ = safe_runner
    runner.request_restart(graceful_only=True, via_service=True)
    runner._restart_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner._restart_task
    assert runner.graceful_restart_status()['reason'] == 'cancelled'
    assert not runner._draining and not runner._restart_task_started
    runner.stop.assert_not_awaited()


@pytest.mark.platforms('posix')
@pytest.mark.asyncio
async def test_native_control_socket_safe_start_status_cancel(safe_runner, tmp_path, monkeypatch):
    from gateway.control_socket import query_gateway_control
    from gateway.run import _start_gateway_start_control_socket
    runner, fence = safe_runner
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    assert fence.acquire()
    server = await _start_gateway_start_control_socket(runner)
    assert server is not None
    async def query(params):
        return await asyncio.wait_for(asyncio.to_thread(
            query_gateway_control, tmp_path, 'pause-for-update', params=params, timeout=5), 8)
    try:
        started = await query({'graceful_only': True, 'timeout_seconds': 30})
        assert started['pausing'] and started['state'] == 'draining'
        operation = started['operation_id']
        status = await query({'graceful_only': True, 'action': 'status'})
        assert status['operation_id'] == operation
        wrong = await query({'graceful_only': True, 'action': 'cancel', 'operation_id': 'wrong'})
        assert not wrong['cancelled']
        cancelled = await query({'graceful_only': True, 'action': 'cancel', 'operation_id': operation})
        assert cancelled['cancelled']
        await asyncio.wait_for(runner._restart_task, 5)
        status = await query({'graceful_only': True, 'action': 'status'})
        assert status == {'operation_id': operation, 'state': 'aborted', 'reason': 'cancelled'}
        assert runner._running and not runner._draining
        runner.stop.assert_not_awaited()
        # No-params callers retain the original default path and kwargs.
        runner.request_restart = MagicMock(return_value=True)
        assert (await query({}))['pausing']
        runner.request_restart.assert_called_once_with(detached=False, via_service=True)
    finally:
        await server.stop()
        fence.release()
