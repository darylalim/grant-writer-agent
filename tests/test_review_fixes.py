"""Regression tests for the code-review findings.

Each test pins a specific defect the review found, so a future change that
reintroduces it fails here rather than silently in production.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_anthropic.chat_models import (
    _FALLBACK_MAX_OUTPUT_TOKENS,
    _MODEL_PROFILES,
)
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
    COMPLIANCE_MODEL,
    DRAFTING_MODEL,
    GRADER_MODEL,
    RESEARCH_MODEL,
    _resolve_root,
)
from grant_writer.tools import build_search_tool

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
    from langgraph.checkpoint.sqlite import SqliteSaver

    db = tmp_path / "ck.sqlite"
    cfg = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
    checkpoint = {
        "v": 1,
        "id": "c1",
        "ts": "2026-01-01T00:00:00+00:00",
        "channel_values": {"x": 1},
        "channel_versions": {},
        "versions_seen": {},
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
    from deepagents.backends import CompositeBackend, StoreBackend

    from grant_writer.config import Settings

    factory = build_backend(Settings(backend_profile="server"))
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


# ---- ⑦ configured models are known to the installed profile registry --------


@pytest.mark.parametrize(
    "spec",
    [DRAFTING_MODEL, RESEARCH_MODEL, COMPLIANCE_MODEL, GRADER_MODEL],
)
def test_model_profile_is_known(spec):
    """A model id absent from the profile registry silently gets a 4096
    `max_tokens` instead of the model's real ceiling.

    Nothing raises: the id is valid and the API accepts it, so the only symptom
    is narratives that stop mid-sentence. Opus 5 also thinks by default and
    thinking shares the `max_tokens` budget, which makes the short cap bite
    sooner. Pinning the lookup catches a model bump that outruns the installed
    `langchain-anthropic`.
    """
    _, _, model_name = spec.partition(":")
    assert model_name in _MODEL_PROFILES, (
        f"{model_name!r} is unknown to langchain-anthropic; max_tokens would "
        f"fall back to {_FALLBACK_MAX_OUTPUT_TOKENS}. Upgrade the dependency."
    )
    max_tokens = ChatAnthropic(model=model_name).max_tokens
    assert max_tokens is not None
    assert max_tokens > _FALLBACK_MAX_OUTPUT_TOKENS
