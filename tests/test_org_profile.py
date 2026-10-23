"""The organization profile: created from a template, never tracked, never reset.

`memories/org/AGENTS.md` holds the organisation's own data once a real run has
written to it, so it is gitignored and created from `templates/org-profile.md`.
Every case here is offline and builds its own root under `tmp_path`, except the
ones that ask git about this checkout.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from deepagents.middleware.filesystem import _check_fs_permission
from langgraph.store.memory import InMemoryStore

from grant_writer.backends import (
    _SEED_ROUTES,
    build_permissions,
    compliance_permissions,
    discovery_permissions,
    drafting_subagent_permissions,
    scouting_permissions,
    seed_store_from_disk,
)
from grant_writer.config import CONTENT_DIRS, PROJECT_ROOT, Settings
from grant_writer.tools import (
    _DISCOVERY_WRITE_DIRS,
    _DRAFTING_WRITE_DIRS,
    _resolve_output_path,
)

TEMPLATE = "# Organization Profile\n\n- **Legal name:** UNKNOWN\n"
PROFILE_KEY = "/org/AGENTS.md"


def _settings(root: Path, *, template: bool = True) -> Settings:
    settings = replace(Settings(), root=root)
    if template:
        settings.memory_template_path.parent.mkdir(parents=True)
        settings.memory_template_path.write_text(TEMPLATE, encoding="utf-8")
    return settings


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    if shutil.which("git") is None or not (PROJECT_ROOT / ".git").exists():
        pytest.skip("needs this checkout's git metadata")
    return subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


# ---- the local profile: copied once, never overwritten ---------------------


def test_a_fresh_root_gets_its_profile_from_the_template(tmp_path):
    settings = _settings(tmp_path)
    assert not settings.memory_path.exists()

    settings.ensure_dirs()

    assert settings.memory_path.read_text(encoding="utf-8") == TEMPLATE


def test_an_existing_profile_is_never_reset_to_the_template(tmp_path):
    """The profile is the organisation's accumulated record. Resetting it on
    the next build would erase every fact a run had confirmed, silently."""
    settings = _settings(tmp_path)
    settings.memory_path.parent.mkdir(parents=True)
    settings.memory_path.write_text("- **Legal name:** Real Org\n", encoding="utf-8")

    settings.ensure_dirs()
    settings.ensure_dirs()

    assert "Real Org" in settings.memory_path.read_text(encoding="utf-8")


def test_a_root_with_no_template_builds_without_one(tmp_path):
    """An install outside a checkout may have no template; that is a missing
    profile for the agent to report, not a crash before the first turn."""
    settings = _settings(tmp_path, template=False)
    settings.ensure_dirs()
    assert not settings.memory_path.exists()


# ---- the server profile: seeding falls back, and never overwrites ----------


def test_seeding_falls_back_to_the_template_when_no_profile_exists(tmp_path):
    settings = _settings(tmp_path)
    store = InMemoryStore()
    _, memories = _SEED_ROUTES["memories"]

    assert seed_store_from_disk(store, settings) == 1

    item = store.get(memories, PROFILE_KEY)
    assert item is not None
    assert item.value["content"] == TEMPLATE


def test_seeding_prefers_the_real_profile_to_the_template(tmp_path):
    settings = _settings(tmp_path)
    settings.memory_path.parent.mkdir(parents=True)
    settings.memory_path.write_text("Real Org\n", encoding="utf-8")
    store = InMemoryStore()
    _, memories = _SEED_ROUTES["memories"]

    seed_store_from_disk(store, settings)

    item = store.get(memories, PROFILE_KEY)
    assert item is not None
    assert item.value["content"] == "Real Org\n"


def test_seeding_never_overwrites_a_memory_the_store_already_holds(tmp_path):
    """Decision 10: Store edits win, and disk only fills what is missing.
    Invisible against today's `InMemoryStore`, which starts empty; against a
    store that outlives the process, the opposite rule reverts every edit to
    the profile on every restart."""
    settings = _settings(tmp_path)
    store = InMemoryStore()
    _, memories = _SEED_ROUTES["memories"]
    seed_store_from_disk(store, settings)
    store.put(memories, PROFILE_KEY, {"content": "edited in the Store"})

    assert seed_store_from_disk(store, settings) == 0

    item = store.get(memories, PROFILE_KEY)
    assert item is not None
    assert item.value["content"] == "edited in the Store"


def test_seeding_still_lets_disk_win_for_skills(tmp_path):
    """The other half of decision 10's split: skills ship with the code and no
    agent may write them, so a restart after a deploy must pick up the new
    text rather than keep the old."""
    settings = _settings(tmp_path)
    skill = tmp_path / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("old", encoding="utf-8")
    store = InMemoryStore()
    _, skills = _SEED_ROUTES["skills"]
    seed_store_from_disk(store, settings)

    skill.write_text("new", encoding="utf-8")
    seed_store_from_disk(store, settings)

    item = store.get(skills, "/demo/SKILL.md")
    assert item is not None
    assert item.value["content"] == "new"


# ---- what keeps the leak closed ----------------------------------------------


def test_the_profile_is_untracked_and_the_template_is_what_ships():
    """Pins invariant 22.

    Asked of git rather than of `.gitignore`'s text: an ignore rule does
    nothing for a file git already tracks, which is the whole reason the
    template was moved rather than the profile merely ignored.
    """
    profile = "memories/org/AGENTS.md"
    template = "templates/org-profile.md"

    assert _git("ls-files", "--error-unmatch", profile).returncode != 0
    assert _git("ls-files", "--error-unmatch", template).returncode == 0
    assert _git("check-ignore", "-q", profile).returncode == 0
    assert Settings().memory_template_path == PROJECT_ROOT / template


@pytest.mark.parametrize(
    "rules",
    [
        build_permissions(replace(Settings(), approve_final=True)),
        drafting_subagent_permissions(replace(Settings(), approve_final=True)),
        discovery_permissions(),
        scouting_permissions(),
        compliance_permissions(),
    ],
    ids=["drafting", "drafting-subagent", "discovery", "scout", "compliance"],
)
def test_no_agent_may_write_the_tracked_template(rules):
    """Pins invariant 22.

    Held by where the file is, not by a rule naming it: outside every content
    directory, the catch-all deny already covers it. Under `memories/` the
    drafting and discovery orchestrators would both be allowed to write it.
    `delete` is checked as a `write` upstream (invariant 18), so this covers it.
    """
    settings = Settings()
    virtual = "/" + settings.memory_template_path.relative_to(settings.root).as_posix()

    assert settings.memory_template_path.parent.name not in CONTENT_DIRS
    assert _check_fs_permission(rules, "write", virtual) == "deny"


@pytest.mark.parametrize("allowed", [_DRAFTING_WRITE_DIRS, _DISCOVERY_WRITE_DIRS])
def test_the_real_disk_writers_refuse_the_tracked_template(allowed, tmp_path):
    """Pins invariant 22.

    The second enforcement point (invariant 2): `extract_pdf_text` writes to
    real disk and never consults the permission rules above.
    """
    with pytest.raises(ValueError):
        _resolve_output_path("/templates/org-profile.md", allowed, tmp_path)
