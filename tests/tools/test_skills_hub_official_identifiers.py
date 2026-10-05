"""An explicit repo identifier must not be captured by a catalog skill that shares its last segment.

``OptionalSkillSource`` runs first in the install router. It locates skills by name for
``official/...`` identifiers and bare names, but ``owner/impeccable`` names a different repo:
matching it to the catalog's ``impeccable`` silently installed the wrong skill.
"""

from unittest.mock import MagicMock

import pytest

from hermes_cli.skills_hub import _resolve_source_meta_and_bundle
from tools.skills_hub_models import SkillBundle
from tools.skills_hub_official import OptionalSkillSource


def _source(tmp_path, local=("creative/impeccable",), remote=()):
    root = tmp_path / "optional-skills"
    root.mkdir()
    for rel in local:
        (root / rel).mkdir(parents=True)
        name = rel.rsplit("/", 1)[-1]
        (root / rel / "SKILL.md").write_text(f"---\nname: {name}\ndescription: catalog {name}\n---\n", encoding="utf-8")
    src = OptionalSkillSource()
    src._optional_dir = root
    src._remote_dirs = dict.fromkeys([*local, *remote], True)
    src._github = MagicMock()
    src._github._get_repo_tree.return_value = None
    return src


@pytest.mark.parametrize("identifier", [
    "someone/impeccable", "github/someone/impeccable", "https://github.com/someone/impeccable",
    "someone/skills/impeccable",
])
def test_repo_identifier_is_not_matched_by_last_segment(tmp_path, identifier):
    src = _source(tmp_path)

    assert src.fetch(identifier) is None
    assert src.inspect(identifier) is None


def test_repo_identifier_is_not_matched_against_the_live_catalog(tmp_path):
    src = _source(tmp_path, local=(), remote=("creative/impeccable",))

    assert src.fetch("someone/impeccable") is None
    assert src.inspect("someone/impeccable") is None
    src._github._get_repo_tree.assert_not_called()


@pytest.mark.parametrize("identifier", ["official/impeccable", "official/creative/impeccable", "impeccable",
                                        "creative/impeccable"])
def test_catalog_identifiers_still_resolve(tmp_path, identifier):
    src = _source(tmp_path)

    bundle = src.fetch(identifier)

    assert bundle is not None and bundle.identifier == "official/creative/impeccable"
    assert src.inspect(identifier).identifier == "official/creative/impeccable"


def test_router_sends_a_repo_identifier_to_the_repo(tmp_path):
    repo_bundle = SkillBundle(name="impeccable", files={"SKILL.md": "---\nname: impeccable\n---\n"},
                              source="github", identifier="someone/impeccable", trust_level="community")
    github = MagicMock()
    github.inspect.return_value = None
    github.fetch.side_effect = lambda ident: repo_bundle if ident == "someone/impeccable" else None

    _meta, bundle, src = _resolve_source_meta_and_bundle("someone/impeccable", [_source(tmp_path), github])

    assert bundle is repo_bundle and src is github
