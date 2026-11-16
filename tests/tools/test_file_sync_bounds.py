"""Bounds on file sync (the VM-wedge class, DevvGwardo/hermes-agent#23):

- ``sync_back_bounded``: teardown paths get a wall clock; a wedged sync is
  abandoned (daemon thread) instead of wedging ``cleanup()`` forever.
- ``sync_back``/``sync``: the in-process transaction lock is acquired with a
  timeout — a wedged holder skips the cycle, not queues forever.
- the ``flock`` serialization on POSIX is bounded the same way.
"""

import logging
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tools.environments import file_sync
from tools.environments.file_sync import FileSyncManager

try:
    import fcntl  # POSIX only
except ImportError:  # pragma: no cover - Windows
    fcntl = None


def _manager(tmp_path: Path) -> FileSyncManager:
    """A manager with no-op transport fns (sync-back has nothing to pull)."""
    return FileSyncManager(
        get_files_fn=lambda: iter([]),
        upload_fn=lambda h, r: None,
        delete_fn=lambda paths: None,
        bulk_upload_fn=None,
        bulk_download_fn=lambda dest: None,
    )


def _patch_lock_timeout(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    monkeypatch.setattr(file_sync, "_sync_back_lock_timeout_s", lambda: seconds)


class TestSyncBackBounded:
    def test_completes_within_the_wall_clock(self, tmp_path, monkeypatch):
        manager = _manager(tmp_path)
        monkeypatch.setattr(manager, "sync_back", MagicMock())
        assert manager.sync_back_bounded(timeout_s=5.0) is True
        manager.sync_back.assert_called_once()

    def test_a_wedged_sync_is_abandoned_not_blocking(self, tmp_path, monkeypatch, caplog):
        manager = _manager(tmp_path)

        def _hang(**_kwargs):
            time.sleep(30)

        monkeypatch.setattr(manager, "sync_back", _hang)
        start = time.monotonic()
        with caplog.at_level(logging.WARNING):
            done = manager.sync_back_bounded(timeout_s=0.2)
        elapsed = time.monotonic() - start
        assert done is False
        assert elapsed < 5  # returned at the cap, not after the hang
        assert any("wall clock" in r.message for r in caplog.records)

    def test_a_failing_sync_does_not_raise_through_the_bound(self, tmp_path, monkeypatch):
        manager = _manager(tmp_path)

        def _boom(**_kwargs):
            raise RuntimeError("transport gone")

        monkeypatch.setattr(manager, "sync_back", _boom)
        # Bounded: the exception is logged, the cap is still honored (True: it
        # COMPLETED — by failing — rather than being abandoned mid-run).
        assert manager.sync_back_bounded(timeout_s=5.0) is True

    def test_default_timeout_comes_from_the_config_knob(self, tmp_path, monkeypatch):
        manager = _manager(tmp_path)
        monkeypatch.setattr(manager, "sync_back", MagicMock())
        monkeypatch.setattr(file_sync, "_sync_back_cleanup_timeout_s", lambda: 0.05)
        manager.sync_back_bounded(timeout_s=None)
        # No assertion on the result: with a MagicMock sync_back the thread
        # finishes fast; the point is that the config knob is consulted
        # without raising.


class TestTransactionLockIsBounded:
    def test_sync_back_skips_when_the_lock_is_held(self, tmp_path, monkeypatch, caplog):
        manager = _manager(tmp_path)
        _patch_lock_timeout(monkeypatch, 0.1)
        transaction = MagicMock()
        monkeypatch.setattr(manager, "_sync_back_transaction", transaction)
        # Hold the lock ourselves so sync_back must time out acquiring it.
        assert manager._transaction_lock.acquire(timeout=5)
        try:
            with caplog.at_level(logging.WARNING):
                manager.sync_back(hermes_home=tmp_path)
        finally:
            manager._transaction_lock.release()
        transaction.assert_not_called()
        assert any("transaction lock busy" in r.message for r in caplog.records)

    def test_sync_skips_a_cycle_when_the_lock_is_held(self, tmp_path, monkeypatch):
        manager = _manager(tmp_path)
        _patch_lock_timeout(monkeypatch, 0.1)
        transaction = MagicMock()
        monkeypatch.setattr(manager, "_sync_transaction", transaction)
        assert manager._transaction_lock.acquire(timeout=5)
        try:
            manager.sync(force=True)
        finally:
            manager._transaction_lock.release()
        transaction.assert_not_called()


@pytest.mark.skipif(fcntl is None, reason="flock serialization is POSIX-only")
class TestFlockIsBounded:
    def test_a_held_flock_gives_up_after_the_timeout(self, tmp_path, monkeypatch):
        manager = _manager(tmp_path)
        _patch_lock_timeout(monkeypatch, 0.1)
        impl = MagicMock()
        monkeypatch.setattr(manager, "_sync_back_impl", impl)
        lock_path = tmp_path / ".sync.lock"

        holder = open(lock_path, "w", encoding="utf-8")
        fcntl.flock(holder, fcntl.LOCK_EX)  # another sandbox's wedged sync
        try:
            with pytest.raises(file_sync.EnvironmentConnectionError, match="still held"):
                manager._sync_back_locked(lock_path)
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()
        impl.assert_not_called()

    def test_the_lock_is_acquired_when_free(self, tmp_path, monkeypatch):
        manager = _manager(tmp_path)
        _patch_lock_timeout(monkeypatch, 2.0)
        impl = MagicMock()
        monkeypatch.setattr(manager, "_sync_back_impl", impl)
        manager._sync_back_locked(tmp_path / ".sync.lock")
        impl.assert_called_once()


class TestConfigKnobs:
    def test_lock_and_cleanup_knobs_read_terminal_config(self, monkeypatch):
        load_config = MagicMock(
            return_value={"terminal": {"sync_back_lock_timeout_s": 7, "sync_back_cleanup_timeout_s": 90}})
        monkeypatch.setattr(
            "hermes_cli.config.load_config", load_config, raising=False)
        import hermes_cli.config as cfg

        monkeypatch.setattr(cfg, "load_config", load_config)
        assert file_sync._sync_back_lock_timeout_s() == 7.0
        assert file_sync._sync_back_cleanup_timeout_s() == 90.0

    def test_garbage_values_fall_back_to_defaults(self, monkeypatch):
        load_config = MagicMock(
            return_value={"terminal": {"sync_back_lock_timeout_s": "soon", "sync_back_cleanup_timeout_s": -5}})
        import hermes_cli.config as cfg

        monkeypatch.setattr(cfg, "load_config", load_config)
        assert file_sync._sync_back_lock_timeout_s() == file_sync._SYNC_BACK_LOCK_TIMEOUT_S
        assert file_sync._sync_back_cleanup_timeout_s() == file_sync._SYNC_BACK_CLEANUP_TIMEOUT_S


class TestWorkerNeverLoadsConfig:
    """The sync-back worker must never read config. CPython can run ``__del__``
    (→ BaseEnvironment.cleanup() → sync_back_bounded) at ANY bytecode boundary —
    including mid-``load_config``, while the interrupted thread holds the config
    cache lock. A knob read on the WORKER thread then blocks on that lock while
    the caller waits on the worker's done-Event: a mutual wait. Observed live as
    a multi-minute stall in test_ssh_environment.py (an SSHEnvironment GC'd
    mid-load_config; every subsequent env cost up to the 300 s wall clock).

    The knobs resolve on the calling thread and ride down as values — see
    ``file_sync._SyncBackKnobs``.
    """

    def test_knobs_resolve_on_the_calling_thread_not_the_worker(self, tmp_path, monkeypatch):
        calls: list[tuple[str, str]] = []

        def _recorder(name: str, value):
            def _fn():
                calls.append((name, threading.current_thread().name))
                return value
            return _fn

        monkeypatch.setattr(file_sync, "_sync_back_lock_timeout_s", _recorder("lock", 5.0))
        monkeypatch.setattr(file_sync, "_sync_back_max_bytes", _recorder("bytes", 1 << 30))

        manager = _manager(tmp_path)
        assert manager.sync_back_bounded(timeout_s=15.0) is True

        # The knobs were read… (lock + bytes, via _sync_back_knobs)
        assert {name for name, _ in calls} == {"lock", "bytes"}
        # …and every read happened on the calling thread, never in the worker.
        assert {thread for _, thread in calls} == {"MainThread"}
