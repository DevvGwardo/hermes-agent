"""Regression tests for dashboard profile-scoped skills/toolsets management.

"Set as active" on the Profiles page only flips the sticky ``active_profile``
file (future CLI/gateway runs) — it never retargets the running dashboard
process. Before the ``profile`` parameter existed, toggling a skill after
"activating" a profile silently wrote into the dashboard's own config.
These tests pin the new behavior: reads and writes land in the REQUESTED
profile's HERMES_HOME, and the dashboard's own profile stays untouched.
"""
import pytest
import hermes_yaml as yaml
import hermes_cli.web_server_gateway as _web_server_gateway
import hermes_cli.web_server_profiles as _web_server_profiles


def _write_skill(skills_dir, name, description="test skill"):
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


@pytest.fixture
def isolated_profiles(tmp_path, monkeypatch, _isolate_hermes_home):
    """Isolated default home + one named profile, each with its own skills."""
    from hermes_constants import get_hermes_home
    from hermes_cli import profiles

    default_home = get_hermes_home()
    profiles_root = default_home / "profiles"
    worker_home = profiles_root / "worker_alpha"
    for home in (default_home, worker_home):
        (home / "skills").mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")

    _write_skill(default_home / "skills", "dashboard-skill")
    _write_skill(worker_home / "skills", "worker-skill")

    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: default_home)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: profiles_root)
    return {"default": default_home, "worker_alpha": worker_home}


@pytest.fixture
def client(monkeypatch, isolated_profiles):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    import hermes_state
    from hermes_constants import get_hermes_home
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")
    c = TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return c


def _load_cfg(home):
    return yaml.safe_load((home / "config.yaml").read_text()) or {}


class TestProfileScopedSkills:


    def test_toggle_writes_into_target_profile_only(self, client, isolated_profiles):
        resp = client.put(
            "/api/skills/toggle",
            json={"name": "worker-skill", "enabled": False, "profile": "worker_alpha"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "name": "worker-skill", "enabled": False}

        worker_cfg = _load_cfg(isolated_profiles["worker_alpha"])
        assert "worker-skill" in worker_cfg.get("skills", {}).get("disabled", [])
        # The dashboard's own config must stay untouched — this was the bug.
        default_cfg = _load_cfg(isolated_profiles["default"])
        assert "worker-skill" not in default_cfg.get("skills", {}).get("disabled", [])



    def test_scope_restores_module_globals(self, client, isolated_profiles):
        """The SKILLS_DIR swap is per-request; the module global must be
        restored even after a scoped call (cron-style locked swap)."""
        import tools.skills_tool as skills_tool

        before = skills_tool.SKILLS_DIR
        client.get("/api/skills", params={"profile": "worker_alpha"})
        assert skills_tool.SKILLS_DIR == before


class TestProfileScopedHubActions:
    def test_hub_install_spawns_with_profile_flag(
        self, client, isolated_profiles, monkeypatch
    ):
        """Hub installs must go through a fresh ``hermes -p <profile>``
        subprocess — the in-process scope can't reach skills_hub's
        import-time SKILLS_DIR binding."""
        import hermes_cli.web_server as web_server

        calls = []

        class _FakeProc:
            pid = 4242

        def _fake_spawn(subcommand, name):
            calls.append((list(subcommand), name))
            return _FakeProc()

        monkeypatch.setattr(_web_server_gateway, "_spawn_hermes_action", _fake_spawn)
        resp = client.post(
            "/api/skills/hub/install",
            json={"identifier": "official/demo", "profile": "worker_alpha"},
        )
        assert resp.status_code == 200
        assert calls == [
            (
                ["-p", "worker_alpha", "skills", "install", "official/demo", "--yes"],
                _web_server_profiles._hub_action_name("install", "official/demo"),
            )
        ]


    def test_hub_install_unknown_profile_404(self, client, isolated_profiles):
        resp = client.post(
            "/api/skills/hub/install",
            json={"identifier": "official/demo", "profile": "ghost"},
        )
        assert resp.status_code == 404


class TestSkillReadinessInListing:
    """GET /api/skills reports per-skill readiness from frontmatter + the
    REQUESTED profile's .env — names and is_set only, never values."""

    _FRONTMATTER = (
        "---\nname: needs-setup\ndescription: needs things\n"
        "required_environment_variables:\n"
        "  - name: DEMO_API_KEY\n    prompt: Your Demo API key\n    help: https://demo.example/keys\n"
        "  - name: DEMO_OPTIONAL\n    optional: true\n"
        "prerequisites:\n  commands: [definitely-not-a-real-binary-xyz]\n"
        "required_credential_files:\n  - demo/token.json\n"
        "---\n\n# needs-setup\n")

    def _write(self, home):
        d = home / "skills" / "needs-setup"
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(self._FRONTMATTER, encoding="utf-8")

    def _get(self, client, name, **params):
        resp = client.get("/api/skills", params=params)
        assert resp.status_code == 200
        return next(s for s in resp.json() if s["name"] == name)

    def test_missing_requirements_reported(self, client, isolated_profiles, monkeypatch):
        monkeypatch.delenv("DEMO_API_KEY", raising=False)
        self._write(isolated_profiles["worker_alpha"])
        skill = self._get(client, "needs-setup", profile="worker_alpha")
        assert skill["setup_needed"] is True
        assert skill["missing_env"] == ["DEMO_API_KEY"]
        assert skill["missing_commands"] == ["definitely-not-a-real-binary-xyz"]
        assert skill["missing_credential_files"] == ["demo/token.json"]
        keys = {e["key"]: e for e in skill["required_env"]}
        assert keys["DEMO_API_KEY"] == {
            "key": "DEMO_API_KEY", "is_set": False, "optional": False,
            "url": "https://demo.example/keys", "description": "Your Demo API key"}
        assert keys["DEMO_OPTIONAL"]["optional"] is True
        assert "_requirements" not in skill

    def test_readiness_uses_target_profile_env_and_hides_values(
            self, client, isolated_profiles, monkeypatch):
        monkeypatch.delenv("DEMO_API_KEY", raising=False)
        worker = isolated_profiles["worker_alpha"]
        self._write(worker)
        (worker / ".env").write_text("DEMO_API_KEY=sekrit-value-123\n", encoding="utf-8")
        (worker / "demo").mkdir()
        (worker / "demo" / "token.json").write_text("{}", encoding="utf-8")

        resp = client.get("/api/skills", params={"profile": "worker_alpha"})
        assert "sekrit-value-123" not in resp.text
        skill = next(s for s in resp.json() if s["name"] == "needs-setup")
        assert skill["setup_needed"] is False
        assert skill["missing_env"] == []
        assert skill["missing_credential_files"] == []
        assert next(e for e in skill["required_env"] if e["key"] == "DEMO_API_KEY")["is_set"] is True
        # Missing commands are advisory — they don't flip setup_needed.
        assert skill["missing_commands"] == ["definitely-not-a-real-binary-xyz"]

        # The dashboard's own profile doesn't see the worker's .env.
        self._write(isolated_profiles["default"])
        own = self._get(client, "needs-setup")
        assert own["missing_env"] == ["DEMO_API_KEY"]

    def test_plain_skill_is_ready(self, client, isolated_profiles):
        skill = self._get(client, "worker-skill", profile="worker_alpha")
        assert skill["setup_needed"] is False
        assert skill["missing_env"] == skill["missing_commands"] == []
        assert skill["missing_credential_files"] == skill["required_env"] == []
