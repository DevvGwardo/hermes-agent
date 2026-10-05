"""Hub installs keep the executable bit a skill's scripts carry at the source.

``SkillBundle.files`` is a path -> bytes map, so the mode is lost unless the source records it in
``SkillBundle.executable`` and ``quarantine_bundle`` restores it.
"""

import os
import stat
from unittest.mock import MagicMock

import pytest

from tools.skills_guard import ScanResult
from tools.skills_hub_github import GitHubSource
from tools.skills_hub_install import install_from_quarantine, quarantine_bundle
from tools.skills_hub_models import SkillBundle
from tools.skills_hub_official import OptionalSkillSource

SKILL_MD = "---\nname: demo-skill\ndescription: demo\n---\nRun `scripts/run context`, see scripts/helper.py.\n"


@pytest.fixture
def hub_dirs(tmp_path, monkeypatch):
    import tools.skills_hub as hub
    hub_dir = tmp_path / "skills" / ".hub"
    for attr, path in {
        "SKILLS_DIR": tmp_path / "skills", "HUB_DIR": hub_dir, "LOCK_FILE": hub_dir / "lock.json",
        "QUARANTINE_DIR": hub_dir / "quarantine", "AUDIT_LOG": hub_dir / "audit.log",
        "TAPS_FILE": hub_dir / "taps.json", "INDEX_CACHE_DIR": hub_dir / "index-cache",
    }.items():
        monkeypatch.setattr(hub, attr, path)
    return tmp_path / "skills"


def _bundle(executable=frozenset({"scripts/run"})) -> SkillBundle:
    return SkillBundle(
        name="demo-skill", source="github", identifier="owner/repo/demo-skill", trust_level="community",
        files={"SKILL.md": SKILL_MD, "scripts/run": b"#!/bin/sh\necho ok\n", "scripts/helper.py": b"print(1)\n"},
        executable=set(executable),
    )


def _is_exec(path) -> bool:
    return bool(path.stat().st_mode & 0o111)


def test_github_fetch_records_mode_100755_blobs():
    src = GitHubSource(auth=MagicMock())
    modes = {"SKILL.md": "100644", "scripts/run": "100755", "scripts/helper.py": "100644"}
    entries = [{"path": f"demo-skill/{p}", "type": "blob", "mode": m, "sha": f"sha-{p}", "size": 3}
               for p, m in modes.items()]
    entries.append({"path": "other-skill/scripts/run", "type": "blob", "mode": "100755", "sha": "x", "size": 3})
    api = {"/repos/owner/repo": {"default_branch": "main"},
           "/repos/owner/repo/git/trees/main": {"sha": "a" * 40, "tree": entries}}
    src._github_json = lambda url, **kw: api[url.split("api.github.com", 1)[1]]
    src._fetch_file_content = lambda repo, path, **kw: SKILL_MD
    src._fetch_file_bytes = lambda repo, path, **kw: b"#!/bin/sh\n"

    bundle = src.fetch("owner/repo/demo-skill")

    assert bundle is not None
    assert bundle.executable == {"scripts/run"}


@pytest.mark.platforms("posix")
def test_quarantine_restores_executable_bit_only_where_recorded(hub_dirs):
    q_path = quarantine_bundle(_bundle())

    assert _is_exec(q_path / "scripts" / "run")
    assert not _is_exec(q_path / "scripts" / "helper.py")
    assert not _is_exec(q_path / "SKILL.md")


@pytest.mark.platforms("posix")
def test_restored_bit_follows_the_umask(hub_dirs):
    old = os.umask(0o077)
    try:
        q_path = quarantine_bundle(_bundle())
    finally:
        os.umask(old)

    assert stat.S_IMODE((q_path / "scripts" / "run").stat().st_mode) == 0o700


def test_bundle_without_executable_paths_writes_plain_files(hub_dirs):
    q_path = quarantine_bundle(_bundle(executable=()))

    assert (q_path / "scripts" / "run").read_bytes() == b"#!/bin/sh\necho ok\n"
    if os.name != "nt":
        assert not _is_exec(q_path / "scripts" / "run")


@pytest.mark.platforms("posix")
def test_installed_skill_keeps_the_executable_bit(hub_dirs):
    bundle = _bundle()
    q_path = quarantine_bundle(bundle)
    scan = ScanResult(skill_name="demo-skill", source="github", trust_level="community", verdict="safe")

    installed = install_from_quarantine(q_path, "demo-skill", "creative", bundle, scan)

    assert installed == hub_dirs / "creative" / "demo-skill"
    assert _is_exec(installed / "scripts" / "run")
    assert not _is_exec(installed / "scripts" / "helper.py")


@pytest.mark.platforms("posix")
def test_optional_skill_from_local_checkout_records_executable_files(tmp_path):
    skill_dir = tmp_path / "optional-skills" / "creative" / "demo-skill"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    (skill_dir / "scripts" / "run").write_bytes(b"#!/bin/sh\n")
    (skill_dir / "scripts" / "run").chmod(0o755)
    (skill_dir / "scripts" / "helper.py").write_bytes(b"print(1)\n")
    src = OptionalSkillSource()
    src._optional_dir = tmp_path / "optional-skills"

    bundle = src.fetch("official/creative/demo-skill")

    assert bundle is not None
    assert bundle.executable == {os.path.join("scripts", "run")}


def test_optional_skill_from_live_repo_records_mode_100755_blobs(tmp_path):
    prefix = "optional-skills/creative/demo-skill/"
    modes = {"SKILL.md": "100644", "install.sh": "100755", "scripts/helper.py": "100644"}
    github = MagicMock()
    github._get_repo_tree.return_value = (
        "main", [{"type": "blob", "path": prefix + p, "mode": m} for p, m in modes.items()])
    github._fetch_file_bytes.side_effect = lambda repo, path: SKILL_MD.encode() if path.endswith(".md") else b"x"
    (tmp_path / "optional-skills").mkdir()
    src = OptionalSkillSource()
    src._optional_dir = tmp_path / "optional-skills"
    src._remote_dirs = {"creative/demo-skill": True}
    src._github = github

    bundle = src.fetch("official/creative/demo-skill")

    assert bundle is not None
    assert bundle.executable == {"install.sh"}


def test_upstream_catalog_redirect_keeps_executable_paths():
    github = MagicMock()
    github.fetch.return_value = _bundle()
    src = OptionalSkillSource()
    src._github = github

    bundle = src._fetch_from_upstream({"repo": "owner/repo", "path": "demo-skill"}, "creative/demo-skill")

    assert bundle is not None and bundle.source == "official"
    assert bundle.executable == {"scripts/run"}
