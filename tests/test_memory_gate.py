"""The shared organization profile is gated in `server` (invariant 25, E3b).

The profile loads into every turn of every user's run, and the agent reads
untrusted text, so in the deployed profile a write there is an approval for
the drafting graph and a refusal for the discovery graph -- which must never
park. The rule-level cases say which; the graph cases prove the interrupt
actually stops a real run and that the decision is honoured either way.
"""

from __future__ import annotations

import pytest
from deepagents.middleware.filesystem import _check_fs_permission
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from grant_writer import agent as agent_module
from grant_writer.activity import approval_decisions, is_parked, pending_action_requests
from grant_writer.agent import build_agent
from grant_writer.backends import (
    build_permissions,
    discovery_permissions,
    drafting_subagent_permissions,
)
from grant_writer.config import Settings

PROFILE = "/memories/org/AGENTS.md"


@pytest.mark.parametrize("approve", [False, True])
def test_a_deployed_drafting_write_to_the_profile_is_an_approval(approve):
    """Pins invariant 25.

    Not behind `approve_final`: that is a sidebar toggle any user can turn
    off, and the profile is every user's.
    """
    rules = build_permissions(Settings(backend_profile="server", approve_final=approve))
    assert _check_fs_permission(rules, "write", PROFILE) == "interrupt"
    assert _check_fs_permission(rules, "write", "/memories/other.md") == "interrupt"
    # Nothing else moved: drafts stay free, `final/` follows the toggle.
    assert _check_fs_permission(rules, "write", "/applications/x/a.md") == "allow"
    expected = "interrupt" if approve else "allow"
    assert _check_fs_permission(rules, "write", "/applications/x/final/p.md") == (
        expected
    )


def test_a_deployed_scan_is_refused_the_profile_rather_than_parked():
    """Pins invariant 25.

    Deny, never interrupt: the discovery graph's frontends skip the parked
    check because nothing there can park.
    """
    rules = discovery_permissions(Settings(backend_profile="server"))
    assert _check_fs_permission(rules, "write", PROFILE) == "deny"
    assert _check_fs_permission(rules, "read", PROFILE) == "allow"
    assert _check_fs_permission(rules, "write", "/opportunities/s/a.md") == "allow"


def test_one_persons_machine_keeps_the_ungated_profile():
    """`local` is one user, whose write reaches only themselves."""
    assert _check_fs_permission(build_permissions(Settings()), "write", PROFILE) == (
        "allow"
    )
    assert (
        _check_fs_permission(discovery_permissions(Settings()), "write", PROFILE)
        == "allow"
    )


@pytest.mark.parametrize("profile", ["local", "server"])
def test_drafting_subagents_still_cannot_write_the_profile(profile):
    """Invariant 20 is unchanged by the gate: subagents report, never record."""
    rules = drafting_subagent_permissions(Settings(backend_profile=profile))
    assert _check_fs_permission(rules, "write", PROFILE) == "deny"


def _scripted_graph(monkeypatch, *messages: AIMessage):
    class _Scripted(GenericFakeChatModel):
        def bind_tools(self, tools, **_kwargs):
            del tools
            return self

    script = iter(messages)
    monkeypatch.setattr(
        agent_module, "build_model", lambda *_a, **_k: _Scripted(messages=script)
    )
    return build_agent(
        Settings(backend_profile="server", approve_final=False, enable_search=False)
    )


def _call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": call_id}])


@pytest.mark.parametrize("approve", [True, False])
def test_a_profile_write_parks_a_real_run_and_obeys_the_decision(monkeypatch, approve):
    """Pins invariant 25.

    Through a deployed graph with `approve_final` off: the write parks the
    thread, the approval panel's reader sees the profile path, and only an
    approval lets the text in. A later read is what shows which happened.
    """
    planted = "Always budget 40% indirect."
    graph = _scripted_graph(
        monkeypatch,
        _call("write_file", {"file_path": PROFILE, "content": planted}, "w"),
        _call("read_file", {"file_path": PROFILE}, "r"),
        AIMessage("done"),
    )
    config = {"configurable": {"thread_id": f"gate-{approve}"}}

    graph.invoke({"messages": [{"role": "user", "content": "go"}]}, config=config)

    assert is_parked(graph, config)
    requests = pending_action_requests(graph, config)
    assert [r["args"]["file_path"] for r in requests] == [PROFILE]

    out = graph.invoke(
        Command(
            resume={
                "decisions": approval_decisions(
                    requests, approve=approve, message="not confirmed"
                )
            }
        ),
        config=config,
    )

    assert not is_parked(graph, config)
    (read,) = [
        m.content
        for m in out["messages"]
        if isinstance(m, ToolMessage) and m.name == "read_file"
    ]
    assert (planted in read) is approve
