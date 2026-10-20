"""Tests for the SSH remote execution environment backend."""

import json
import os
import subprocess
from unittest.mock import MagicMock

import pytest

from tools.environments.ssh import SSHEnvironment
from tools.environments import ssh as ssh_env

_SSH_HOST = os.getenv("TERMINAL_SSH_HOST", "")
_SSH_USER = os.getenv("TERMINAL_SSH_USER", "")
_SSH_PORT = int(os.getenv("TERMINAL_SSH_PORT", "22"))
_SSH_KEY = os.getenv("TERMINAL_SSH_KEY", "")

_has_ssh = bool(_SSH_HOST and _SSH_USER)

requires_ssh = pytest.mark.skipif(
    not _has_ssh,
    reason="TERMINAL_SSH_HOST / TERMINAL_SSH_USER not set",
)


def _run(command, task_id="ssh_test", **kwargs):
    from tools.terminal_tool import terminal_tool
    return json.loads(terminal_tool(command, task_id=task_id, **kwargs))


def _cleanup(task_id="ssh_test"):
    from tools.terminal_tool_lifecycle import cleanup_vm
    cleanup_vm(task_id)


class TestBuildSSHCommand:

    @pytest.fixture(autouse=True)
    def _mock_connection(self, monkeypatch):
        monkeypatch.setattr("tools.environments.ssh.subprocess.run",
                            lambda *a, **k: subprocess.CompletedProcess([], 0))
        monkeypatch.setattr("tools.environments.ssh.subprocess.Popen",
                            lambda *a, **k: MagicMock(stdout=iter([]),
                                                      stderr=iter([]),
                                                      stdin=MagicMock()))
        monkeypatch.setattr("tools.environments.base.time.sleep", lambda _: None)

    def test_base_flags(self, monkeypatch):
        # ControlMaster flags are POSIX-only (#73927): assert them only
        # where multiplexing is enabled so the test passes on Windows too.
        monkeypatch.setattr(ssh_env, "_SSH_MULTIPLEX", True)
        env = SSHEnvironment(host="h", user="u")
        cmd = " ".join(env._build_ssh_command())
        for flag in ("ControlMaster=auto", "ControlPersist=300",
                      "BatchMode=yes", "StrictHostKeyChecking=accept-new"):
            assert flag in cmd

    def test_controlmaster_gated_off_on_windows(self, monkeypatch):
        """#73927: Windows OpenSSH has no Unix-domain ControlMaster, so the
        ControlPath/ControlMaster/ControlPersist options must be omitted —
        passing them fails the connection with 'getsockname failed'."""
        monkeypatch.setattr(ssh_env, "_SSH_MULTIPLEX", False)
        env = SSHEnvironment(host="h", user="u")
        cmd = " ".join(env._build_ssh_command())
        assert "ControlMaster" not in cmd
        assert "ControlPath" not in cmd
        assert "ControlPersist" not in cmd
        # Non-multiplex flags must still be present — the backend works,
        # just without connection pooling.
        assert "BatchMode=yes" in cmd
        assert "StrictHostKeyChecking=accept-new" in cmd
        assert env._build_ssh_command()[-1] == "u@h"


    def test_user_host_suffix(self):
        env = SSHEnvironment(host="h", user="u")
        assert env._build_ssh_command()[-1] == "u@h"

    def _capture_run_bash(self, monkeypatch, env, cmd="echo ok"):
        captured = {}

        def _fake_popen(cmd, stdin_data=None, **kwargs):
            captured["cmd"], captured["env"] = cmd, kwargs.get("env")
            return MagicMock()

        monkeypatch.setattr(ssh_env, "_popen_bash", _fake_popen)
        env._run_bash(cmd)
        return captured

    def test_run_bash_forwards_passthrough_by_sendenv_never_in_remote_argv(self, monkeypatch):
        """#14091: allowlisted names travel as ``-o SendEnv=NAME`` with values only in the ssh client env;
        provider credentials on the allowlist stay behind; a .env value fills an unset shell var."""
        import tools.env_passthrough as env_passthrough

        env = SSHEnvironment(host="h", user="u")
        monkeypatch.setenv("NEXTCLOUD_URL", "https://next.example")
        monkeypatch.delenv("NEXTCLOUD_PASS", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-forward")
        monkeypatch.setattr(env_passthrough, "get_all_passthrough",
                            lambda: frozenset({"NEXTCLOUD_URL", "NEXTCLOUD_PASS", "OPENAI_API_KEY"}))
        monkeypatch.setattr(ssh_env, "_load_hermes_env_vars", lambda: {"NEXTCLOUD_PASS": "from-dotenv"})

        captured = self._capture_run_bash(monkeypatch, env)

        sent = {a.split("=", 1)[1] for a in captured["cmd"] if a.startswith("SendEnv=")}
        assert sent == {"NEXTCLOUD_URL", "NEXTCLOUD_PASS"}
        assert captured["env"]["NEXTCLOUD_URL"] == "https://next.example"
        assert captured["env"]["NEXTCLOUD_PASS"] == "from-dotenv"
        remote_text = " ".join(captured["cmd"])
        assert "https://next.example" not in remote_text and "from-dotenv" not in remote_text
        assert "sk-must-not-forward" not in remote_text

    def test_run_bash_without_passthrough_inherits_env_unchanged(self, monkeypatch):
        import tools.env_passthrough as env_passthrough

        monkeypatch.setattr(env_passthrough, "get_all_passthrough", lambda: frozenset())
        captured = self._capture_run_bash(monkeypatch, SSHEnvironment(host="h", user="u"))
        assert not any(a.startswith("SendEnv=") for a in captured["cmd"])
        assert captured["env"] is None


class TestControlSocketPath:
    """Regression tests for issue #11840.

    macOS caps Unix domain socket paths at 104 bytes (sun_path). SSH
    appends a 16-byte random suffix to the control socket path when
    operating in ControlMaster mode. An IPv6 host embedded in the
    filename plus the deeply-nested macOS $TMPDIR easily blows past
    the limit, causing every tool call to fail immediately.
    """

    @pytest.fixture(autouse=True)
    def _mock_connection(self, monkeypatch):
        monkeypatch.setattr("tools.environments.ssh.subprocess.run",
                            lambda *a, **k: subprocess.CompletedProcess([], 0))
        monkeypatch.setattr("tools.environments.ssh.subprocess.Popen",
                            lambda *a, **k: MagicMock(stdout=iter([]),
                                                      stderr=iter([]),
                                                      stdin=MagicMock()))
        monkeypatch.setattr("tools.environments.base.time.sleep", lambda _: None)

    # SSH appends ``.XXXXXXXXXXXXXXXX`` (17 bytes) to the ControlPath in
    # ControlMaster mode; the macOS sun_path field is 104 bytes including
    # the NUL terminator, so the usable path length is 103 bytes.
    _SSH_CONTROLMASTER_SUFFIX = 17
    _MAX_SUN_PATH = 103

    def test_fits_under_macos_socket_limit_with_ipv6_host(self, monkeypatch):
        """A realistic macOS $TMPDIR + IPv6 host must still produce a
        control socket path that fits once SSH appends its ControlMaster
        suffix (see issue #11840)."""
        # Simulate the macOS $TMPDIR shape from the issue traceback —
        # 48 bytes, the typical length of ``/var/folders/XX/YYYYYYYYY/T``.
        fake_tmp = "/var/folders/2t/wbkw5yb158jc3zhswgl7tz9c0000gn/T"
        monkeypatch.setattr("tools.environments.ssh.tempfile.gettempdir",
                            lambda: fake_tmp)
        # The simulated path doesn't exist on the test host — skip the
        # real mkdir so __init__ can proceed.
        from pathlib import Path as _Path
        monkeypatch.setattr(_Path, "mkdir", lambda *a, **k: None)

        env = SSHEnvironment(
            host="9373:9b91:4480:558d:708e:e601:24e8:d8d0",
            user="hermes",
            port=22,
        )

        total_len = len(str(env.control_socket)) + self._SSH_CONTROLMASTER_SUFFIX
        assert total_len <= self._MAX_SUN_PATH, (
            f"control socket path would exceed the {self._MAX_SUN_PATH}-byte "
            f"Unix domain socket limit once SSH appends its 16-byte suffix: "
            f"{env.control_socket} (+{self._SSH_CONTROLMASTER_SUFFIX} = {total_len})"
        )

    def test_path_is_deterministic_across_instances(self):
        """Same (user, host, port) must yield the same control socket so
        ControlMaster reuse works across reconnects."""
        first = SSHEnvironment(host="example.com", user="alice", port=2222)
        second = SSHEnvironment(host="example.com", user="alice", port=2222)
        assert first.control_socket == second.control_socket

    def test_path_differs_for_different_targets(self):
        """Different (user, host, port) triples must produce different paths."""
        base = SSHEnvironment(host="h", user="u", port=22).control_socket
        assert SSHEnvironment(host="h", user="u", port=23).control_socket != base
        assert SSHEnvironment(host="h", user="v", port=22).control_socket != base
        assert SSHEnvironment(host="g", user="u", port=22).control_socket != base


class TestTerminalToolConfig:


    def test_ssh_persistent_respects_config(self, monkeypatch):
        """TERMINAL_PERSISTENT_SHELL=false disables SSH persistent by default."""
        monkeypatch.delenv("TERMINAL_SSH_PERSISTENT", raising=False)
        monkeypatch.setenv("TERMINAL_PERSISTENT_SHELL", "false")
        from tools.terminal_tool import _get_env_config
        assert _get_env_config()["ssh_persistent"] is False


class TestSSHPreflight:
    def test_ensure_ssh_available_raises_clear_error_when_missing(self, monkeypatch):
        monkeypatch.setattr(ssh_env.shutil, "which", lambda _name: None)

        with pytest.raises(RuntimeError, match="SSH is not installed or not in PATH"):
            ssh_env._ensure_ssh_available()


    def test_ssh_environment_connects_when_ssh_exists(self, monkeypatch):
        called = {"count": 0}

        monkeypatch.setattr(ssh_env.shutil, "which", lambda _name: "/usr/bin/ssh")

        def _fake_establish(self):
            called["count"] += 1

        monkeypatch.setattr(ssh_env.SSHEnvironment, "_establish_connection", _fake_establish)
        monkeypatch.setattr(ssh_env.SSHEnvironment, "_detect_remote_home", lambda self: "/home/alice")
        monkeypatch.setattr(ssh_env.SSHEnvironment, "_ensure_remote_dirs", lambda self: None)
        monkeypatch.setattr(ssh_env.SSHEnvironment, "init_session", lambda self: None)
        monkeypatch.setattr(ssh_env, "FileSyncManager", lambda **kw: type("M", (), {"sync": lambda self, **k: None})())

        env = ssh_env.SSHEnvironment(host="example.com", user="alice")

        assert called["count"] == 1
        assert env.host == "example.com"
        assert env.user == "alice"


@pytest.fixture
def _mock_ssh_runtime(monkeypatch, tmp_path):
    hooks = {
        "_establish_connection": MagicMock(),
        "_detect_remote_home": MagicMock(return_value="/home/alice"),
        "_ensure_remote_dirs": MagicMock(),
        "init_session": MagicMock(),
    }
    monkeypatch.setattr(ssh_env.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(ssh_env.shutil, "which", lambda _name: "/usr/bin/ssh")
    for name, hook in hooks.items():
        monkeypatch.setattr(ssh_env.SSHEnvironment, name, hook)
    hooks["sync_factory"] = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(ssh_env, "FileSyncManager", hooks["sync_factory"])
    return hooks


class TestSSHProbeOnly:
    def test_probe_only_skips_state_sync_and_session_setup(self, _mock_ssh_runtime):
        env = ssh_env.SSHEnvironment(host="example.com", user="alice", probe_only=True)
        env._before_execute()
        env.cleanup()

        _mock_ssh_runtime["_establish_connection"].assert_called_once_with()
        _mock_ssh_runtime["_detect_remote_home"].assert_not_called()
        _mock_ssh_runtime["_ensure_remote_dirs"].assert_not_called()
        _mock_ssh_runtime["sync_factory"].assert_not_called()
        _mock_ssh_runtime["init_session"].assert_not_called()

    def test_probe_only_control_socket_is_isolated(self, monkeypatch, _mock_ssh_runtime):
        control_exit_calls = []

        def _fake_run(*args, **kwargs):
            control_exit_calls.append(args[0])
            return subprocess.CompletedProcess([], 0)

        monkeypatch.setattr(ssh_env.subprocess, "run", _fake_run)

        normal = ssh_env.SSHEnvironment(host="example.com", user="alice")
        first_probe = ssh_env.SSHEnvironment(host="example.com", user="alice", probe_only=True)
        second_probe = ssh_env.SSHEnvironment(host="example.com", user="alice", probe_only=True)

        assert first_probe.control_socket != normal.control_socket
        assert second_probe.control_socket != first_probe.control_socket
        assert len(first_probe.control_socket.name) == len(normal.control_socket.name)

        normal.control_socket.touch()
        first_probe.control_socket.touch()
        first_probe.cleanup()

        assert normal.control_socket.exists()
        assert not first_probe.control_socket.exists()
        assert len(control_exit_calls) == 1


def _setup_ssh_env(monkeypatch, persistent: bool):
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", _SSH_HOST)
    monkeypatch.setenv("TERMINAL_SSH_USER", _SSH_USER)
    monkeypatch.setenv("TERMINAL_SSH_PERSISTENT", "true" if persistent else "false")
    if _SSH_PORT != 22:
        monkeypatch.setenv("TERMINAL_SSH_PORT", str(_SSH_PORT))
    if _SSH_KEY:
        monkeypatch.setenv("TERMINAL_SSH_KEY", _SSH_KEY)


@requires_ssh
class TestOneShotSSH:

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        _setup_ssh_env(monkeypatch, persistent=False)
        yield
        _cleanup()

    def test_echo(self):
        r = _run("echo hello")
        assert r["exit_code"] == 0
        assert "hello" in r["output"]


    def test_state_does_not_persist(self):
        _run("export HERMES_ONESHOT_TEST=yes")
        r = _run("echo $HERMES_ONESHOT_TEST")
        assert r["output"].strip() == ""


@requires_ssh
class TestPersistentSSH:

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        _setup_ssh_env(monkeypatch, persistent=True)
        yield
        _cleanup()

    def test_echo(self):
        r = _run("echo hello-persistent")
        assert r["exit_code"] == 0
        assert "hello-persistent" in r["output"]

    def test_env_var_persists(self):
        _run("export HERMES_PERSIST_TEST=works")
        r = _run("echo $HERMES_PERSIST_TEST")
        assert r["output"].strip() == "works"


    def test_large_output(self):
        r = _run("seq 1 1000")
        assert r["exit_code"] == 0
        lines = r["output"].strip().splitlines()
        assert len(lines) == 1000
        assert lines[0] == "1"
        assert lines[-1] == "1000"


class TestRemoteKillOnTimeout:
    """A timed-out or interrupted command must stop on the remote host, not only the local ssh
    client: with no pty sshd does not signal it, so a silent loop ran on for good."""

    @pytest.fixture(autouse=True)
    def _mock_connection(self, monkeypatch):
        monkeypatch.setattr("tools.environments.ssh.subprocess.run",
                            lambda *a, **k: subprocess.CompletedProcess([], 0))
        monkeypatch.setattr("tools.environments.base.time.sleep", lambda _: None)
        self.helpers = []
        monkeypatch.setattr("tools.environments.ssh.subprocess.Popen",
                            lambda cmd, **k: self.helpers.append((cmd, k)) or MagicMock())

    def _spawn(self, monkeypatch, env, cmd="sleep 600"):
        captured = {}

        def _fake_popen(cmd, stdin_data=None, **kwargs):
            captured["cmd"] = cmd
            return MagicMock()

        monkeypatch.setattr(ssh_env, "_popen_bash", _fake_popen)
        proc = env._run_bash(cmd)
        return proc, captured["cmd"]

    def test_every_command_carries_a_unique_token_in_its_remote_argv(self, monkeypatch):
        env = SSHEnvironment(host="h", user="u")
        p1, cmd1 = self._spawn(monkeypatch, env)
        p2, _ = self._spawn(monkeypatch, env)
        assert p1._hermes_remote_token.startswith("hermes-call-")
        assert p1._hermes_remote_token != p2._hermes_remote_token
        assert f": {p1._hermes_remote_token}; " in cmd1[-1]
        assert cmd1[-1].endswith("sleep 600'")

    def test_kill_hands_the_remote_kill_to_one_bounded_detached_helper(self, monkeypatch):
        env = SSHEnvironment(host="h", user="u")
        proc, _ = self._spawn(monkeypatch, env)
        self.helpers.clear()
        env._kill_process(proc)
        env._kill_process(proc)  # the base backstop may call it again: one remote kill only
        assert proc.kill.call_count == 2
        (cmd, kwargs), = self.helpers
        # A python helper that runs ssh under a wall clock, in its own session (survives a hard exit).
        assert cmd[:2] == [ssh_env.sys.executable, "-c"] and "timeout=" in cmd[2]
        assert cmd[3] == str(ssh_env._REMOTE_KILL_TIMEOUT_S) and cmd[4] == "ssh"
        assert kwargs.get("start_new_session") is True
        assert cmd[-4:-1] == ["u@h", "bash", "-c"]
        token = proc._hermes_remote_token
        remote = cmd[-1]
        assert token not in remote  # matches the token, not the kill script's own argv
        assert f"[h]{token[1:]}" in remote
        assert "!= 0 ] || exit 0" in remote  # no caller-group exemption on another host
        assert "kill -TERM" in remote and "kill -KILL" in remote

    def _with_output(self, env, proc, *chunks, max_chars=10_000):
        from tools.environments.base_output import _BoundedOutputCollector

        out = _BoundedOutputCollector(max_chars)
        out.watch_cwd_marker(env._cwd_marker)
        for c in chunks:
            out.append(c)
        proc._hermes_wait_output = out
        return out

    def test_the_marker_counts_even_when_truncation_drops_it(self, monkeypatch):
        """60K chars before the wrapper finished and 60K after (a background job still writing):
        the bounded window loses the marker line, the streamed watch does not."""
        env = SSHEnvironment(host="h", user="u")
        proc, _ = self._spawn(monkeypatch, env)
        m = env._cwd_marker
        out = self._with_output(env, proc, "x" * 60_000, f"\n{m}/opt/data/workspace{m}\n", "y" * 60_000, max_chars=20_000)
        assert m not in out.render()
        self.helpers.clear()
        env._kill_process(proc)
        assert self.helpers == []

    def test_an_echo_of_the_wrappers_argv_is_not_the_marker(self, monkeypatch):
        """``ps -o args=`` of the wrapper prints the printf format: literal \\n and %s, no path."""
        env = SSHEnvironment(host="h", user="u")
        proc, _ = self._spawn(monkeypatch, env)
        m = env._cwd_marker
        self._with_output(env, proc, f"bash -c ... printf '\\n{m}%s{m}\\n' \"$(pwd -P)\"\n")
        self.helpers.clear()
        env._kill_process(proc)
        assert len(self.helpers) == 1

    def test_a_marker_split_across_chunks_is_seen(self, monkeypatch):
        env = SSHEnvironment(host="h", user="u")
        proc, _ = self._spawn(monkeypatch, env)
        m = env._cwd_marker
        line = f"\n{m}/opt/data/workspace{m}\n"
        self._with_output(env, proc, "out" + line[:7], line[7:20], line[20:])
        self.helpers.clear()
        env._kill_process(proc)
        assert self.helpers == []

    def test_a_finished_wrapper_only_kills_the_client(self, monkeypatch):
        """The wrapper printed its closing CWD marker: what holds the channel is a job it left in
        the background (``cmd &``, the process registry's ``( nohup … ) &``), which must survive."""
        env = SSHEnvironment(host="h", user="u")
        proc, _ = self._spawn(monkeypatch, env)
        self._with_output(env, proc, f"12345\n{env._cwd_marker}/opt/data/workspace{env._cwd_marker}\n")
        self.helpers.clear()
        env._kill_process(proc)
        assert self.helpers == []
        proc.kill.assert_called_once()

    def test_a_still_running_wrapper_is_killed_remotely(self, monkeypatch):
        env = SSHEnvironment(host="h", user="u")
        proc, _ = self._spawn(monkeypatch, env)
        self._with_output(env, proc, "still waiting...\n")
        self.helpers.clear()
        env._kill_process(proc)
        assert len(self.helpers) == 1

    def test_the_kill_is_marked_only_once_a_helper_exists(self, monkeypatch):
        env = SSHEnvironment(host="h", user="u")
        proc, _ = self._spawn(monkeypatch, env)

        def _boom(*a, **k):
            raise OSError("fork failed")

        monkeypatch.setattr("tools.environments.ssh.subprocess.Popen", _boom)
        env._kill_process(proc)
        assert getattr(proc, "_hermes_remote_kill_started", None) is not True
        monkeypatch.setattr("tools.environments.ssh.subprocess.Popen",
                            lambda cmd, **k: self.helpers.append((cmd, k)) or MagicMock())
        self.helpers.clear()
        env._force_kill_process(proc)  # the hard-exit path still gets its helper
        assert len(self.helpers) == 1

    def test_the_helper_is_reaped(self, monkeypatch):
        env = SSHEnvironment(host="h", user="u")
        proc, _ = self._spawn(monkeypatch, env)
        reaped = []
        monkeypatch.setattr(ssh_env, "_reap_in_background", reaped.append)
        env._kill_process(proc)
        assert len(reaped) == 1

    def test_a_loopback_target_spares_the_callers_group(self, monkeypatch):
        env = SSHEnvironment(host="127.0.0.1", user="u")
        proc, _ = self._spawn(monkeypatch, env)
        self.helpers.clear()
        env._kill_process(proc)
        assert f"!= {os.getpgrp()} ] || exit 0" in self.helpers[0][0][-1]

    def test_the_hard_exit_path_uses_the_same_non_blocking_kill(self, monkeypatch):
        env = SSHEnvironment(host="h", user="u")
        proc, _ = self._spawn(monkeypatch, env)
        waited = []
        monkeypatch.setattr("tools.environments.ssh.subprocess.run",
                            lambda *a, **k: waited.append(a) or subprocess.CompletedProcess([], 0))
        self.helpers.clear()
        env._force_kill_process(proc)
        # (A GC'd env from another test may run its own cleanup here: only the kill counts.)
        assert not any("kill -TERM" in str(c) for c in waited)
        assert len(self.helpers) == 1
        proc.kill.assert_called_once()

    def test_a_helper_that_cannot_start_still_kills_the_client(self, monkeypatch):
        env = SSHEnvironment(host="h", user="u")
        proc, _ = self._spawn(monkeypatch, env)

        def _boom(*a, **k):
            raise OSError("no python")

        monkeypatch.setattr("tools.environments.ssh.subprocess.Popen", _boom)
        env._kill_process(proc)
        proc.kill.assert_called_once()

    def test_a_handle_without_a_token_only_kills_the_client(self, monkeypatch):
        env = SSHEnvironment(host="h", user="u")
        self.helpers.clear()
        proc = MagicMock(spec=["kill", "poll", "wait"])
        env._kill_process(proc)
        proc.kill.assert_called_once()
        assert self.helpers == []


_POSIX_PGREP = pytest.mark.skipif(os.name == "nt" or not ssh_env.shutil.which("pgrep"), reason="needs a POSIX host with pgrep")


def _spawn_group(argv):
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _killpg_quiet(pid):
    try:
        os.killpg(pid, 9)
    except (ProcessLookupError, PermissionError):
        pass


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


@_POSIX_PGREP
@pytest.mark.parametrize("shell", ["bash", "dash", "sh"])
def test_remote_kill_script_stops_a_silent_loop_and_its_children(shell):
    """The real script against a real process group: the shape of the 17h incident (a loop that
    never writes output), plus a child in the same group. Run with the shells sshd may use."""
    import time

    if not ssh_env.shutil.which(shell):
        pytest.skip(f"{shell} not installed")
    token = ssh_env._new_call_token()
    victim = _spawn_group(["bash", "-c", f": {token}; sleep 300 & while true; do sleep 1; done"])
    try:
        time.sleep(0.5)
        out = subprocess.run([shell, "-c", ssh_env._remote_kill_script(token)], capture_output=True, text=True, timeout=15)
        assert out.returncode == 0, out.stderr
        victim.wait(timeout=5)
        time.sleep(0.3)
        assert not _group_alive(victim.pid)
    finally:
        _killpg_quiet(victim.pid)
    # Nothing left to find: a second run is a no-op.
    assert subprocess.run([shell, "-c", ssh_env._remote_kill_script(token)], timeout=15).returncode == 0


@_POSIX_PGREP
def test_remote_kill_script_spares_a_local_ssh_client_carrying_the_token(tmp_path):
    """Loopback target (hermes-deploy's tool sandbox): the local ssh CLIENT's argv carries the token
    as well. The kill must skip it (its comm may be a full path, as on macOS) and stop only the
    remote command."""
    import shutil as _sh
    import time

    token = ssh_env._new_call_token()
    fake_ssh = tmp_path / "ssh"
    _sh.copy(_sh.which("bash"), fake_ssh)
    client = _spawn_group([str(fake_ssh), "-c", "sleep 300", f"bash -c ': {token}; ...'"])
    victim = _spawn_group(["bash", "-c", f": {token}; while true; do sleep 1; done"])
    try:
        time.sleep(0.5)
        assert client.pid < victim.pid  # the client is found first, as on the VM
        subprocess.run(["bash", "-c", ssh_env._remote_kill_script(token)], timeout=15, check=True)
        victim.wait(timeout=5)
        assert client.poll() is None, "the local ssh client's group was killed"
    finally:
        _killpg_quiet(client.pid)
        _killpg_quiet(victim.pid)


@_POSIX_PGREP
def test_remote_kill_script_spares_the_callers_group():
    import time

    token = ssh_env._new_call_token()
    victim = _spawn_group(["bash", "-c", f": {token}; while true; do sleep 1; done"])
    try:
        time.sleep(0.5)
        subprocess.run(["bash", "-c", ssh_env._remote_kill_script(token, spare_pgid=victim.pid)], timeout=15, check=True)
        assert victim.poll() is None
    finally:
        _killpg_quiet(victim.pid)
