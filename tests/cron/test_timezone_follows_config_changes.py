"""A long-running gateway / dashboard must see a time-zone change without a restart.

Before: hermes_time cached the first resolved zone per config PATH (and the gateway pinned it into
HERMES_TIMEZONE at startup), so an agent that started on UTC kept computing cron fire times in UTC
after the user set America/New_York from the app: a "28 20 * * 1-5" routine was scheduled for
20:28 UTC, four hours late.
"""
import os
import time
from zoneinfo import ZoneInfo

import pytest

import hermes_time


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    monkeypatch.delenv("HERMES_TIMEZONE_FROM_CONFIG", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    hermes_time.reset_cache()
    yield
    hermes_time.reset_cache()


def _write_config(tmp_path, text):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(text, encoding="utf-8")
    # Distinct mtime even on coarse-timestamp filesystems.
    stamp = time.time() + (0 if not hasattr(_write_config, "n") else _write_config.n)
    _write_config.n = getattr(_write_config, "n", 0) + 1
    os.utime(cfg, (stamp, stamp))


def test_zone_change_in_config_is_seen_without_restart(tmp_path):
    _write_config(tmp_path, "model: x\n")
    assert hermes_time.get_timezone_name() == ""
    _write_config(tmp_path, "timezone: America/New_York\n")
    assert hermes_time.get_timezone_name() == "America/New_York"
    assert hermes_time.now().tzinfo == ZoneInfo("America/New_York")


def test_gateway_startup_copy_does_not_pin_the_zone(tmp_path, monkeypatch):
    _write_config(tmp_path, "timezone: UTC\n")
    # gateway/run.py bridges config.yaml's zone into the env and marks the copy.
    monkeypatch.setenv("HERMES_TIMEZONE", "UTC")
    monkeypatch.setenv("HERMES_TIMEZONE_FROM_CONFIG", "UTC")
    assert hermes_time.get_timezone_name() == "UTC"
    _write_config(tmp_path, "timezone: America/New_York\n")
    assert hermes_time.get_timezone_name() == "America/New_York"


def test_a_real_env_override_still_wins(tmp_path, monkeypatch):
    _write_config(tmp_path, "timezone: America/New_York\n")
    monkeypatch.setenv("HERMES_TIMEZONE", "Asia/Tokyo")
    assert hermes_time.get_timezone_name() == "Asia/Tokyo"


def test_next_cron_fire_uses_the_new_zone(tmp_path):
    from cron.jobs import compute_next_run

    _write_config(tmp_path, "model: x\n")
    utc_next = compute_next_run({"kind": "cron", "expr": "28 20 * * 1-5"})
    assert utc_next.endswith("+00:00")
    _write_config(tmp_path, "timezone: America/New_York\n")
    ny_next = compute_next_run({"kind": "cron", "expr": "28 20 * * 1-5"})
    assert ny_next.endswith(("-04:00", "-05:00"))
