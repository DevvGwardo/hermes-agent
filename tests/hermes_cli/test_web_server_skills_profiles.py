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
        from tools import skills_tool

        before = skills_tool.SKILLS_DIR
        client.get("/api/skills", params={"profile": "worker_alpha"})
        assert skills_tool.SKILLS_DIR == before

    @pytest.mark.parametrize("method,path,body", [
        ("PUT", "/api/learning/node", {"id": "shared-skill", "content": "EDITED"}),
        ("DELETE", "/api/learning/node", {"id": "shared-skill"}),
        ("PUT", "/api/skills/content", {"name": "shared-skill", "content": "EDITED"}),
        ("POST", "/api/skills", {"name": "shared-skill-2", "content": "EDITED"}),
    ])
    def test_query_profile_scopes_skill_writes(self, client, isolated_profiles, method, path, body):
        """A shared-backend Desktop names the profile in the query only; the write must land in
        that profile and leave the same-named skill of the dashboard's own profile alone."""
        skill_md = (
            "---\nname: {name}\ndescription: edited\n---\n\n# EDITED\n").format(
                name=body.get("name", body.get("id")))
        if "content" in body:
            body = {**body, "content": skill_md}
        for home in isolated_profiles.values():
            _write_skill(home / "skills", "shared-skill")
        default_md = isolated_profiles["default"] / "skills" / "shared-skill" / "SKILL.md"
        before = default_md.read_text()

        resp = client.request(method, path, params={"profile": "worker_alpha"}, json=body)

        assert resp.status_code == 200, resp.text
        worker_skills = isolated_profiles["worker_alpha"] / "skills"
        if method == "DELETE":
            assert not (worker_skills / "shared-skill").exists()
        else:
            target = body.get("name", body.get("id"))
            assert "# EDITED" in (worker_skills / target / "SKILL.md").read_text()
        assert default_md.read_text() == before
        assert not (isolated_profiles["default"] / "skills" / "shared-skill-2").exists()

    def test_lock_round_trips_in_target_profile_and_list_carries_it(
            self, client, isolated_profiles, monkeypatch):
        """PUT /api/skills/lock keys the record by DIRECTORY (what the write guards resolve)
        in the requested profile; GET /api/skills maps it back onto the row's load name."""
        from tools import skill_usage

        worker_skills = isolated_profiles["worker_alpha"] / "skills"
        d = worker_skills / "dir-name"  # declared name differs from the directory
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: shown-name\ndescription: x\n---\n\n# x\n")

        resp = client.put("/api/skills/lock", params={"profile": "worker_alpha"},
                          json={"name": "shown-name", "locked": True})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "name": "shown-name", "locked": True}
        monkeypatch.setenv("HERMES_HOME", str(isolated_profiles["worker_alpha"]))
        assert skill_usage.is_locked("dir-name")
        monkeypatch.setenv("HERMES_HOME", str(isolated_profiles["default"]))
        assert not skill_usage.is_locked("dir-name")

        rows = client.get("/api/skills", params={"profile": "worker_alpha"}).json()
        assert {r["name"]: r["locked"] for r in rows} == {"worker-skill": False, "shown-name": True}

        resp = client.put("/api/skills/lock", params={"profile": "worker_alpha"},
                          json={"name": "shown-name", "locked": False})
        assert resp.status_code == 200
        monkeypatch.setenv("HERMES_HOME", str(isolated_profiles["worker_alpha"]))
        assert not skill_usage.is_locked("dir-name")

    def test_lock_unknown_skill_404_and_corrupt_sidecar_lists_unlocked(
            self, client, isolated_profiles):
        resp = client.put("/api/skills/lock", json={"name": "ghost", "locked": True})
        assert resp.status_code == 404



class TestProfileScopedHubActions:
    def test_hub_install_spawns_with_profile_flag(
        self, client, isolated_profiles, monkeypatch
    ):
        """Hub installs must go through a fresh ``hermes -p <profile>``
        subprocess — the in-process scope can't reach skills_hub's
        import-time SKILLS_DIR binding."""
        from hermes_cli import web_server

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

    def test_hub_install_scoped_to_default_still_carries_the_selector(
        self, client, isolated_profiles, monkeypatch
    ):
        """``default`` is a real named profile, not an alias for "the dashboard's own".
        A pooled ``hermes -p worker_alpha serve`` dashboard answering a hub action with
        ``profile=default`` must still emit ``-p default``: without it the child's env
        carries this process's home verbatim and the install lands on worker_alpha."""
        calls = []

        class _FakeProc:
            pid = 4242

        def _fake_spawn(subcommand, name):
            calls.append(list(subcommand))
            return _FakeProc()

        # Ambient home of a dashboard serving under the named profile.
        monkeypatch.setenv("HERMES_HOME", str(isolated_profiles["worker_alpha"]))
        monkeypatch.setattr(_web_server_gateway, "_spawn_hermes_action", _fake_spawn)
        resp = client.post(
            "/api/skills/hub/install",
            json={"identifier": "official/demo", "profile": "default"},
        )
        assert resp.status_code == 200
        assert calls == [
            ["-p", "default", "skills", "install", "official/demo", "--yes"]
        ]
        # The spawn-path env pin must land the child on the default home, not the
        # ambient one it would inherit from a selector-less argv.
        env = _web_server_gateway._profile_action_environment(calls[0])
        assert env["HERMES_HOME"] == str(isolated_profiles["default"])
        (isolated_profiles["default"] / "skills" / ".usage.json").write_text("{not json")
        resp = client.get("/api/skills")
        assert resp.status_code == 200
        assert [r["locked"] for r in resp.json()] == [False]

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
