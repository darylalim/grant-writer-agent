"""Regression tests for the code-review findings.

Each test pins a specific defect the review found, so a future change that
reintroduces it fails here rather than silently in production.
"""

from __future__ import annotations

import os
import sqlite3
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from grant_writer.agent import _build_checkpointer, build_agent
from grant_writer.backends import (
    _SEED_ROUTES,
    build_backend,
    seed_store_from_disk,
)
from grant_writer.cli import _build_parser, _resolve_interrupt
from grant_writer.config import (
    Settings,
    _resolve_root,
    application_dir,
    application_ids,
)
from grant_writer.tools import build_search_tool
from grant_writer.workspace import (
    NO_VERDICT,
    application_files,
    compliance_verdict,
    count_gaps,
    modified_at,
    read_bytes,
    read_text,
)

# ---- ① --no-search actually disables search --------------------------------


def test_search_tool_disabled_even_with_key(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "dummy-key")
    assert build_search_tool(enabled=False) is None
    assert build_search_tool(enabled=True) is not None  # sanity: key is seen


def test_disabled_search_removes_tool_from_graph(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "dummy-key")
    from grant_writer.config import Settings

    def tool_names(settings):
        graph = build_agent(settings)
        return set(graph.nodes["tools"].bound.tools_by_name)

    assert "tavily_search" in tool_names(Settings(enable_search=True))
    assert "tavily_search" not in tool_names(Settings(enable_search=False))


# ---- ② batched-approval crash ----------------------------------------------


def _fake_agent_with_interrupts(n_requests: int):
    """A stand-in agent whose state has one interrupt carrying n action requests."""
    action_requests = [
        {"name": "write_file", "args": {"file_path": f"/applications/x/final/{i}.md"}}
        for i in range(n_requests)
    ]
    interrupt = SimpleNamespace(value={"action_requests": action_requests})
    task = SimpleNamespace(interrupts=[interrupt])
    state = SimpleNamespace(tasks=[task])
    return SimpleNamespace(get_state=lambda _config: state)


@pytest.mark.parametrize("n", [1, 2, 3])
def test_resolve_interrupt_returns_one_decision_per_request(monkeypatch, n):
    """HITL requires len(decisions) == number of interrupted tool calls."""
    monkeypatch.setattr("builtins.input", lambda *_: "approve")
    agent = _fake_agent_with_interrupts(n)
    result = _resolve_interrupt(agent, {})
    assert result is not None
    assert len(result["decisions"]) == n
    assert all(d["type"] == "approve" for d in result["decisions"])


def test_resolve_interrupt_reject_matches_count(monkeypatch):
    answers = iter(["reject", "run tests first"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    agent = _fake_agent_with_interrupts(2)
    result = _resolve_interrupt(agent, {})
    assert result is not None
    assert [d["type"] for d in result["decisions"]] == ["reject", "reject"]
    assert all(d["message"] == "run tests first" for d in result["decisions"])


# ---- ③ SQLite checkpointer -------------------------------------------------


def test_checkpointer_defaults_to_in_memory():
    from grant_writer.config import Settings

    assert isinstance(_build_checkpointer(Settings()), InMemorySaver)


def test_checkpointer_uses_sqlite_when_db_set(tmp_path):
    from langgraph.checkpoint.sqlite import SqliteSaver

    from grant_writer.config import Settings

    db = tmp_path / "sub" / "checkpoints.sqlite"
    cp = _build_checkpointer(Settings(checkpoint_db=db))
    assert isinstance(cp, SqliteSaver)
    assert db.exists()  # parent dir created and file opened


def test_sqlite_checkpoint_persists_across_savers(tmp_path):
    """A second saver on the same file sees what the first wrote -- the property
    that makes cross-process `draft` -> `chat` resume actually work."""
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import Checkpoint
    from langgraph.checkpoint.sqlite import SqliteSaver

    db = tmp_path / "ck.sqlite"
    cfg: RunnableConfig = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
    checkpoint: Checkpoint = {
        "v": 1,
        "id": "c1",
        "ts": "2026-01-01T00:00:00+00:00",
        "channel_values": {"x": 1},
        "channel_versions": {},
        "versions_seen": {},
        "updated_channels": None,
    }
    with SqliteSaver.from_conn_string(str(db)) as saver:
        saver.put(cfg, checkpoint, {}, {})

    conn = sqlite3.connect(str(db))
    reopened = SqliteSaver(conn)
    assert reopened.get(cfg) is not None
    conn.close()


# ---- ④ flags accepted after the subcommand ---------------------------------


def test_flags_accepted_after_subcommand():
    parser = _build_parser()
    args = parser.parse_args(
        ["draft", "--app-id", "x", "--approve", "--no-search", "--profile", "server"]
    )
    assert args.approve is True
    assert args.no_search is True
    assert args.profile == "server"
    assert args.app_id == "x"


def test_chat_also_accepts_common_flags_after_subcommand():
    args = _build_parser().parse_args(["chat", "--app-id", "y", "--approve"])
    assert args.approve is True
    assert args.app_id == "y"


# ---- ⑤ server profile seeds skills + memory --------------------------------


def test_seed_store_uses_prefix_stripped_keys_per_namespace():
    """CompositeBackend strips the route prefix before delegating, so keys must
    be stored prefix-stripped and in the route's own namespace -- otherwise
    routed reads 404 and `ls /skills/` leaks memory files (the live bug)."""
    from grant_writer.config import Settings

    store = InMemoryStore()
    n = seed_store_from_disk(store, Settings())
    assert n >= 7  # six skills + the memory file

    _, skills_ns = _SEED_ROUTES["skills"]
    _, memories_ns = _SEED_ROUTES["memories"]

    # Skills live under their own namespace, keyed WITHOUT the /skills/ prefix.
    skill = store.get(skills_ns, "/rfp-decomposition/SKILL.md")
    assert skill is not None
    assert "rfp-decomposition" in skill.value["content"]
    # And NOT under the doubled/prefixed key that the old seeding produced.
    assert store.get(skills_ns, "/skills/rfp-decomposition/SKILL.md") is None

    memory = store.get(memories_ns, "/org/AGENTS.md")
    assert memory is not None
    # Memory must not leak into the skills namespace.
    assert store.get(skills_ns, "/org/AGENTS.md") is None


def test_seed_store_skips_non_utf8_files(tmp_path):
    """A stray binary file (e.g. macOS .DS_Store) under skills/ must not crash
    server-profile startup -- it is skipped, not decoded."""
    from dataclasses import replace

    from grant_writer.config import Settings

    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\nbody", encoding="utf-8")
    # Invalid UTF-8, as a real .DS_Store would be.
    (tmp_path / "skills" / ".DS_Store").write_bytes(b"\x00\x01\xff\xfe not utf-8")

    store = InMemoryStore()
    settings = replace(Settings(), root=tmp_path)
    n = seed_store_from_disk(store, settings)  # must not raise

    _, skills_ns = _SEED_ROUTES["skills"]
    assert store.get(skills_ns, "/demo/SKILL.md") is not None
    assert n == 1  # the binary file was skipped, the real skill seeded


def test_server_backend_routes_skills_and_memory_to_store():
    from deepagents.backends import BackendProtocol, CompositeBackend, StoreBackend

    from grant_writer.config import Settings

    factory = build_backend(Settings(backend_profile="server"))
    # build_backend returns `BackendProtocol | Callable[..., BackendProtocol]`.
    # The server profile is specifically the callable half -- local returns a
    # ready-made FilesystemBackend instead -- and that split is part of what
    # this test pins, so assert it rather than assuming it.
    assert not isinstance(factory, BackendProtocol)
    backend = factory(SimpleNamespace())
    assert isinstance(backend, CompositeBackend)
    for prefix in ("/skills/", "/memories/"):
        assert isinstance(backend.routes[prefix], StoreBackend)


# ---- ⑥ install-safe project root -------------------------------------------


def test_resolve_root_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GRANT_WRITER_ROOT", str(tmp_path))
    assert _resolve_root() == tmp_path.resolve()


def test_resolve_root_finds_repo_without_env(monkeypatch):
    monkeypatch.delenv("GRANT_WRITER_ROOT", raising=False)
    root = _resolve_root()
    assert (root / "skills").is_dir()


# ---- ⑦ a user-supplied application id cannot escape applications/ ----------


@pytest.mark.parametrize(
    "app_id",
    [
        "/Users/me/.ssh",  # absolute: pathlib discards the base entirely
        "../../../tmp/evil",
        "..",
        ".",
        "nsf/../../etc",
        "nsf\\..\\..",
        "",
        "   ",
    ],
)
def test_application_dir_rejects_ids_that_leave_the_tree(app_id, tmp_path):
    """The UI joins this onto a real path and writes an upload there.

    `Path("/repo/applications") / "/etc/x"` is `/etc/x` -- the left operand is
    discarded -- so an unvalidated id is an arbitrary-write, and on the read
    side an id of ".." lists the repo root and offers `.env` for download.
    """
    with pytest.raises(ValueError, match=r"application id|outside applications"):
        application_dir(Settings(root=tmp_path), app_id)


@pytest.mark.parametrize("app_id", ["nsf-aisl-2026", "NSF_26.v2", "a", "2026"])
def test_application_dir_accepts_plain_ids_and_stays_inside(app_id, tmp_path):
    settings = Settings(root=tmp_path)
    resolved = application_dir(settings, app_id)
    assert resolved.is_relative_to(settings.applications_path.resolve())
    assert resolved.name == app_id


def test_application_dir_strips_surrounding_whitespace(tmp_path):
    settings = Settings(root=tmp_path)
    assert application_dir(settings, "  nsf-26  ").name == "nsf-26"


# ---- ⑧ the compliance verdict shown is the current one ---------------------


def test_verdict_prefers_the_newest_review_not_the_first_sorted(tmp_path):
    """`review/` gains a report per pass; a stale NOT-READY must not outlive
    the fixes that cleared it."""
    review = tmp_path / "review"
    review.mkdir()
    old = review / "compliance-01.md"
    new = review / "compliance-02.md"
    old.write_text("Verdict: NOT-READY", encoding="utf-8")
    new.write_text("Verdict: SUBMIT-READY", encoding="utf-8")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))

    # Alphabetically `old` sorts first, so first-match-wins would return it.
    assert compliance_verdict(application_files(tmp_path)) == "SUBMIT-READY"


def test_verdict_takes_the_last_token_not_a_passing_mention(tmp_path):
    """COMPLIANCE_PROMPT puts the verdict at the end; the prose above it can
    legitimately name the other one."""
    review = tmp_path / "review"
    review.mkdir()
    (review / "compliance.md").write_text(
        "The draft is no longer NOT-READY.\n\nVerdict: SUBMIT-READY\n",
        encoding="utf-8",
    )
    assert compliance_verdict(application_files(tmp_path)) == "SUBMIT-READY"


def test_verdict_is_absent_when_no_review_states_one(tmp_path):
    review = tmp_path / "review"
    review.mkdir()
    (review / "notes.md").write_text("Still working through it.", encoding="utf-8")
    assert compliance_verdict(application_files(tmp_path)) == NO_VERDICT


def test_gaps_exclude_the_review_that_collects_them(tmp_path):
    """The reviewer's job is to gather every marker, so counting its report
    would double each gap it found."""
    (tmp_path / "sections").mkdir()
    (tmp_path / "review").mkdir()
    (tmp_path / "sections" / "need.md").write_text(
        "[NEEDS INPUT: baseline data?] and [NEEDS INPUT: indirect rate?]",
        encoding="utf-8",
    )
    (tmp_path / "review" / "gaps.md").write_text(
        "[NEEDS INPUT: baseline data?]\n[NEEDS INPUT: indirect rate?]",
        encoding="utf-8",
    )
    assert count_gaps(application_files(tmp_path)) == 2


def test_workspace_readers_tolerate_a_vanished_file(tmp_path):
    """The listing is a snapshot and the agent keeps writing."""
    missing = tmp_path / "gone.md"
    assert read_text(missing) == ""
    assert read_bytes(missing) is None
    assert modified_at(missing) == 0.0


def test_application_ids_offers_only_what_the_boundary_will_accept(tmp_path):
    """The picker's options get joined onto a path by `application_dir`, so an
    entry that boundary would refuse must never be offered in the first place.
    `application_ids` runs each candidate through that function rather than
    through a copy of its rules, which is what keeps the two from drifting into
    a dropdown whose entries raise when they are selected.
    """
    settings = Settings(root=tmp_path)
    apps = tmp_path / "applications"
    apps.mkdir()
    (apps / "nsf-aisl-2026").mkdir()
    (apps / "zz_ok.2").mkdir()
    (apps / "has space").mkdir()  # not a legal id
    (apps / "loose-file.md").write_text("x", encoding="utf-8")  # not a directory

    listed = application_ids(settings)
    assert listed == ["nsf-aisl-2026", "zz_ok.2"]
    # Everything offered survives the boundary, which is the point.
    for app_id in listed:
        assert application_dir(settings, app_id).parent == apps.resolve()


def test_application_ids_is_empty_before_the_directory_exists(tmp_path):
    """First run, nothing drafted yet. Returning [] rather than raising is what
    lets the frontend simply not draw the picker."""
    assert application_ids(Settings(root=tmp_path)) == []


def test_application_ids_does_not_offer_a_symlink_out_of_the_tree(tmp_path):
    """The case a name filter cannot see.

    `applications/legacy -> /elsewhere` passes any test applied to the string
    `legacy`, and `is_dir()` follows the link and agrees. It is only when
    `application_dir` *resolves* it that the escape shows up -- and by then the
    id is in the box and the user gets a refusal where their files should be.
    Matching `_SAFE_APP_ID` on both sides was never the same thing as agreeing
    with the boundary; calling the boundary is.
    """
    settings = Settings(root=tmp_path)
    apps = tmp_path / "applications"
    apps.mkdir()
    (apps / "real").mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (apps / "legacy").symlink_to(outside, target_is_directory=True)
    # Inside the tree is still fine: the boundary's objection is the escape.
    (apps / "alias").symlink_to(apps / "real", target_is_directory=True)

    assert application_ids(settings) == ["alias", "real"]
    with pytest.raises(ValueError, match="outside applications/"):
        application_dir(settings, "legacy")


def test_application_ids_hides_dot_directories(tmp_path):
    """`_SAFE_APP_ID` admits a leading dot -- only `.` and `..` are special-cased
    -- so the regex alone puts `.git` and `.ipynb_checkpoints` in the picker
    beside real applications. Harmless to the boundary, which resolves them
    inside the tree quite happily; the objection is that this is a listing of
    the user's applications and editor droppings are not among them.
    """
    settings = Settings(root=tmp_path)
    apps = tmp_path / "applications"
    apps.mkdir()
    (apps / "nsf-aisl-2026").mkdir()
    (apps / ".ipynb_checkpoints").mkdir()
    (apps / ".git").mkdir()

    assert application_ids(settings) == ["nsf-aisl-2026"]
    # Not a security claim: the boundary accepts these, it just should not be
    # asked to. Pinned so the reason for the extra filter stays legible.
    assert application_dir(settings, ".git").name == ".git"


def test_application_ids_does_not_offer_a_name_that_opens_another_directory(tmp_path):
    """Calling the boundary settles whether an id is *accepted*, not whether it
    still denotes the directory that was listed. `application_dir` strips before
    it validates, so `"nsf-26 "` -- a trailing space, which a hand-made
    directory picks up easily -- is accepted and resolves to
    `applications/nsf-26`. Listed verbatim it puts an entry in the picker that
    reads one application and opens another, while the padded directory stays
    unreachable through any frontend, since no accepted id resolves to it.
    """
    settings = Settings(root=tmp_path)
    apps = tmp_path / "applications"
    apps.mkdir()
    (apps / "nsf-26").mkdir()
    (apps / "nsf-26 ").mkdir()

    assert application_ids(settings) == ["nsf-26"]
    # The boundary does accept it -- that is exactly why the listing has to ask
    # the further question rather than trusting acceptance alone.
    assert application_dir(settings, "nsf-26 ") == (apps / "nsf-26").resolve()


def test_application_ids_survives_an_unreadable_applications_directory(tmp_path):
    """This is called from the Streamlit script body, outside any try, so an
    `OSError` here takes down the whole page -- no title, no sidebar, no way to
    resume a checkpointed run -- rather than just the picker. `is_dir()` returns
    True for a directory with no read permission, so the precheck it replaced
    would not have caught this either.
    """
    settings = Settings(root=tmp_path)
    apps = tmp_path / "applications"
    apps.mkdir()
    (apps / "nsf-aisl-2026").mkdir()
    apps.chmod(0o000)
    try:
        if os.access(apps, os.R_OK):  # running as root; the chmod means nothing
            pytest.skip("cannot make a directory unreadable as this user")
        assert application_ids(settings) == []
    finally:
        apps.chmod(0o755)
