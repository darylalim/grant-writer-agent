"""Wiring tests. No model API calls, so these run offline and in CI.

They cover the things that fail *silently* in a Deep Agents setup: a subagent
that quietly lost its skills, a permission rule ordered so the catch-all deny
shadows the allow, or an interrupt configured without the checkpointer that
makes it work.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from deepagents.middleware.filesystem import _check_fs_permission, supports_execution

from grant_writer.agent import build_agent
from grant_writer.backends import (
    build_backend,
    build_permissions,
    compliance_permissions,
)
from grant_writer.config import PROJECT_ROOT, Settings
from grant_writer.subagents import build_subagents
from grant_writer.tools import (
    _parse_page_spec,
    _resolve_output_path,
    extract_pdf_text,
    measure_text,
)


@pytest.mark.parametrize("profile", ["local", "server"])
def test_graph_builds(profile):
    assert build_agent(Settings(backend_profile=profile)) is not None


@pytest.mark.parametrize(
    "tool_name",
    [
        "task",
        "write_todos",
        "read_file",
        "write_file",
        "edit_file",
        "ls",
        "glob",
        "grep",
        "extract_pdf_text",
        "measure_text",
    ],
)
def test_expected_tools_registered(tool_name):
    graph = build_agent(Settings())
    registry = graph.nodes["tools"].bound.tools_by_name
    assert tool_name in registry


def test_subagent_roster():
    names = [s["name"] for s in build_subagents()]
    assert names == ["funder-researcher", "section-drafter", "compliance-checker"]


def test_drafting_subagents_receive_skills_explicitly():
    """Custom subagents do NOT inherit skills from the parent.

    Without this, the drafter still answers -- just generically, with none of
    the funder-specific guidance. That is the failure this test exists for.
    """
    for sub in build_subagents():
        if sub["name"] in {"section-drafter", "compliance-checker"}:
            assert sub.get("skills") == ["/skills/"], sub["name"]


@pytest.mark.parametrize(
    ("operation", "path", "expected"),
    [
        ("write", "/applications/nsf-26/sections/need.md", "allow"),
        ("write", "/memories/org/AGENTS.md", "allow"),
        ("write", "/skills/statement-of-need/SKILL.md", "deny"),
        ("write", "/src/grant_writer/agent.py", "deny"),
        ("write", "/pyproject.toml", "deny"),
        ("read", "/skills/statement-of-need/SKILL.md", "allow"),
        ("read", "/src/grant_writer/agent.py", "allow"),
    ],
)
def test_main_agent_permissions(operation, path, expected):
    rules = build_permissions(Settings(approve_final=False))
    assert _check_fs_permission(rules, operation, path) == expected


def test_approve_final_interrupts_only_final_writes():
    rules = build_permissions(Settings(approve_final=True))
    assert (
        _check_fs_permission(rules, "write", "/applications/x/final/proposal.md")
        == "interrupt"
    )
    # Scratch work must stay friction-free, or approvals get rubber-stamped.
    assert (
        _check_fs_permission(rules, "write", "/applications/x/sections/need.md")
        == "allow"
    )


def test_approve_final_wires_human_in_the_loop():
    plain = set(build_agent(Settings(approve_final=False)).get_graph().nodes)
    gated = set(build_agent(Settings(approve_final=True)).get_graph().nodes)
    assert any("HumanInTheLoop" in node for node in gated - plain)


def test_reviewer_cannot_edit_what_it_reviews():
    rules = compliance_permissions()
    assert (
        _check_fs_permission(rules, "write", "/applications/x/review/report.md")
        == "allow"
    )
    assert (
        _check_fs_permission(rules, "write", "/applications/x/sections/need.md")
        == "deny"
    )
    assert (
        _check_fs_permission(rules, "read", "/applications/x/sections/need.md")
        == "allow"
    )


def test_execute_tool_is_not_a_permission_bypass():
    """`execute` is registered but inert on FilesystemBackend.

    It is offered to the model, so if this ever flips to True the write
    permissions above stop being a real boundary -- the agent could shell out.
    """
    backend = build_backend(Settings(backend_profile="local"))
    assert not callable(backend), "local profile should build a concrete backend"
    assert supports_execution(backend) is False


def test_measure_text_counts_accurately():
    out = measure_text.invoke({"text": "one two three four five"})
    assert "words=5" in out
    assert "est_pages_single_spaced" in out


def test_extract_pdf_text_reports_missing_file():
    assert extract_pdf_text.invoke({"pdf_path": "/nope/missing.pdf"}).startswith(
        "Error: no such file"
    )


@pytest.mark.parametrize(
    "out_path",
    ["/applications/x/rfp.md", "applications/x/rfp.md", "/memories/org/AGENTS.md"],
)
def test_output_paths_inside_content_dirs_are_allowed(out_path):
    assert _resolve_output_path(out_path).is_relative_to(PROJECT_ROOT)


@pytest.mark.parametrize(
    "out_path",
    [
        "/src/grant_writer/agent.py",
        "/skills/statement-of-need/SKILL.md",
        "/pyproject.toml",
        "/applications/../src/agent.py",
        "/etc/passwd",
    ],
)
def test_output_paths_outside_content_dirs_are_refused(out_path):
    """`extract_pdf_text` writes to real disk, bypassing FilesystemPermission.

    It therefore has to enforce the same boundary itself, or it becomes a way
    around every write rule tested above.
    """
    with pytest.raises(ValueError, match="refusing to write|escapes the project root"):
        _resolve_output_path(out_path)


@pytest.mark.parametrize(
    ("spec", "total", "expected"),
    [("1-3,7", 10, [0, 1, 2, 6]), ("", 3, [0, 1, 2]), ("9-12", 10, [8, 9])],
)
def test_page_spec_parsing(spec, total, expected):
    assert _parse_page_spec(spec, total) == expected


def test_skills_have_valid_frontmatter():
    """Skills without frontmatter are ignored, silently."""
    skills = sorted(Path(PROJECT_ROOT, "skills").glob("*/SKILL.md"))
    assert len(skills) >= 5
    for skill in skills:
        lines = skill.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "---", skill
        head = lines[:6]
        assert any(line.startswith("name:") for line in head), skill
        assert any(line.startswith("description:") for line in head), skill
