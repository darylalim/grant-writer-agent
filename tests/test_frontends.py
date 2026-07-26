"""What the CLI and the Streamlit UI must agree on.

Two frontends now read the same stream and send the same brief. Both couplings
fail silently: a renamed key in a `deepagents` tool call turns into a blank
label rather than an error, and a kickoff instruction that drifts in one
frontend produces a differently-steered proposal from identical inputs. Neither
shows up as a crash, so they are pinned here.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from grant_writer.activity import (
    DELEGATE,
    MESSAGE,
    PLAN,
    TOOL,
    approval_decisions,
    iter_activity,
    pending_action_requests,
)
from grant_writer.cli import _print_activity
from grant_writer.config import Settings, persistent_settings
from grant_writer.prompts import draft_request

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _message(tool_calls=None, text=""):
    """A stand-in for a langchain message, which exposes `.text` as a method."""
    return SimpleNamespace(tool_calls=tool_calls or [], text=lambda: text)


def _chunk(node="model", **kwargs):
    return {node: {"messages": [_message(**kwargs)]}}


# ---- ① the stream parser ----------------------------------------------------


def test_delegation_target_is_read_from_subagent_type():
    call = {"name": "task", "args": {"subagent_type": "section-drafter"}}
    (event,) = iter_activity(_chunk(tool_calls=[call]))
    assert event.kind == DELEGATE
    assert event.label == "section-drafter"


def test_delegation_falls_back_to_legacy_agent_key():
    """A blank label is the failure mode this guards: it renders, just empty."""
    call = {"name": "task", "args": {"agent": "funder-researcher"}}
    (event,) = iter_activity(_chunk(tool_calls=[call]))
    assert event.label == "funder-researcher"


def test_plan_events_carry_todos_and_status_marks():
    call = {
        "name": "write_todos",
        "args": {
            "todos": [
                {"status": "completed", "content": "Extract requirements"},
                {"status": "in_progress", "content": "Draft statement of need"},
                {"status": "pending", "content": "Budget justification"},
            ]
        },
    }
    (event,) = iter_activity(_chunk(tool_calls=[call]))
    assert event.kind == PLAN
    assert [todo.mark for todo in event.todos] == ["x", ">", " "]
    assert event.todos[0].content == "Extract requirements"


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (
            {"file_path": "/applications/x/sections/need.md"},
            "/applications/x/sections/need.md",
        ),
        ({"path": "/skills/"}, "/skills/"),
        ({"query": "NSF AISL priorities"}, "NSF AISL priorities"),
        ({}, ""),
    ],
)
def test_tool_subject_is_read_from_whichever_key_is_present(args, expected):
    (event,) = iter_activity(_chunk(tool_calls=[{"name": "read_file", "args": args}]))
    assert event.kind == TOOL
    assert event.detail == expected


def test_prose_is_only_surfaced_from_the_orchestrator_node():
    assert list(iter_activity(_chunk(node="tools", text="tool output"))) == []
    (event,) = iter_activity(_chunk(node="model", text="Draft complete."))
    assert event.kind == MESSAGE
    assert event.detail == "Draft complete."


def test_a_message_carrying_tool_calls_is_not_also_rendered_as_prose():
    """Otherwise every delegation would print twice."""
    call = {"name": "task", "args": {"subagent_type": "compliance-checker"}}
    events = list(iter_activity(_chunk(tool_calls=[call], text="Delegating now.")))
    assert [event.kind for event in events] == [DELEGATE]


def test_interrupt_chunks_yield_no_events():
    """They change control flow, not the display; callers detect them apart."""
    assert list(iter_activity({"__interrupt__": ()})) == []


def test_cli_output_is_unchanged_by_the_shared_parser(capsys):
    """The terminal trace is a user-facing surface; the refactor must not move it."""
    _print_activity(
        _chunk(tool_calls=[{"name": "task", "args": {"subagent_type": "x-agent"}}])
    )
    _print_activity(
        _chunk(
            tool_calls=[
                {
                    "name": "write_todos",
                    "args": {"todos": [{"status": "completed", "content": "Plan"}]},
                }
            ]
        )
    )
    _print_activity(
        _chunk(tool_calls=[{"name": "write_file", "args": {"file_path": "/a/b.md"}}])
    )
    assert capsys.readouterr().out == (
        "  -> delegate to x-agent\n"
        "  -> plan (1 steps)\n"
        "     [x] Plan\n"
        "  -> write_file /a/b.md\n"
    )


# ---- ② the shared brief -----------------------------------------------------


def test_draft_request_names_the_application_directory():
    assert "/applications/nsf-26/" in draft_request("nsf-26")


def test_draft_request_omits_sections_it_was_given_nothing_for():
    minimal = draft_request("nsf-26")
    assert "solicitation is the PDF" not in minimal
    assert "The funder is" not in minimal
    assert "Additional context" not in minimal
    # The process instruction is unconditional -- it is what drives the run.
    assert "requirements checklist" in minimal


def test_draft_request_routes_the_pdf_through_extract_pdf_text():
    """Never `write_file`: relayed PDF text gets silently abridged in transit."""
    request = draft_request("nsf-26", rfp_path="/tmp/rfp.pdf", funder="NSF")
    assert "extract_pdf_text" in request
    assert "/tmp/rfp.pdf" in request
    assert "/applications/nsf-26/rfp.md" in request
    assert "The funder is NSF." in request


# ---- ③ frontends persist, libraries do not ----------------------------------


def test_persistent_settings_checkpoints_to_disk():
    """Without this, `--approve` interrupts are a no-op and nothing resumes."""
    settings = persistent_settings()
    assert settings.checkpoint_db == settings.default_checkpoint_db
    assert settings.checkpoint_db is not None


def test_plain_settings_stay_in_memory():
    assert Settings().checkpoint_db is None


def test_persistent_settings_passes_flags_through():
    settings = persistent_settings(
        backend_profile="server", approve_final=True, enable_search=False
    )
    assert settings.backend_profile == "server"
    assert settings.approve_final is True
    assert settings.enable_search is False


# ---- ④ approval decisions are counted per action request --------------------


def test_pending_action_requests_flattens_across_interrupts():
    """Resume needs one decision per request; counting interrupts under-counts."""
    interrupt = SimpleNamespace(
        value={"action_requests": [{"name": "write_file"}, {"name": "write_file"}]}
    )
    agent = SimpleNamespace(
        get_state=lambda _config: SimpleNamespace(
            tasks=[SimpleNamespace(interrupts=[interrupt])]
        )
    )
    assert len(pending_action_requests(agent, {})) == 2


@pytest.mark.parametrize("n", [0, 1, 2, 5])
def test_approval_decisions_returns_one_decision_per_request(n):
    """Both frontends build the resume payload here. A count that disagrees
    with the interrupted calls is rejected on resume, and zero requests still
    needs one decision -- the floor the CLI has always applied."""
    requests = [{"name": "write_file"} for _ in range(n)]
    approved = approval_decisions(requests, approve=True)
    assert len(approved) == max(n, 1)
    assert all(decision == {"type": "approve"} for decision in approved)


def test_approval_decisions_carries_the_rejection_reason():
    rejected = approval_decisions([{}, {}], approve=False, message="  fix the budget ")
    assert [d["type"] for d in rejected] == ["reject", "reject"]
    assert all(d["message"] == "fix the budget" for d in rejected)


def test_rejection_without_a_reason_still_says_something():
    (decision,) = approval_decisions([], approve=False, message="   ")
    assert decision == {"type": "reject", "message": "Rejected."}


def test_approval_decisions_are_independent_objects():
    """`[{...}] * n` would alias one dict; mutating one would mutate all."""
    decisions = approval_decisions([{}, {}], approve=True)
    decisions[0]["type"] = "reject"
    assert decisions[1]["type"] == "approve"


# ---- ⑤ the UI renders at all ------------------------------------------------


def _app_test(monkeypatch=None):
    """The real Streamlit script, ready to run headlessly.

    streamlit is a dev dependency, so `uv sync` installs it and these run
    everywhere the rest of the suite does. The guard only covers `--no-dev`,
    where pytest itself would be missing too.

    Pass `monkeypatch` to force the credential check to pass. Without it the
    app's behaviour depends on whether the developer has a dotenv -- conftest
    blanks TAVILY_API_KEY, so on a clean checkout every case would render the
    disabled-button state and never exercise the app in its normal one.
    """
    pytest.importorskip("streamlit", reason="dev dependency; run `uv sync`")
    from streamlit.testing.v1 import AppTest

    if monkeypatch is not None:
        monkeypatch.setattr(
            "grant_writer.config.require_api_keys", lambda **_kwargs: []
        )
    return AppTest.from_file(str(PROJECT_ROOT / "streamlit_app.py"), default_timeout=60)


def test_streamlit_app_renders_without_error():
    """A Streamlit app fails at run time, not import time, so nothing else in
    this suite would catch a bad layout or a renamed element parameter."""
    app = _app_test()
    app.run()

    assert not app.exception
    assert [title.value for title in app.title] == ["Grant writer"]
    assert {"Activity", "Application files"} <= {s.value for s in app.subheader}


def test_every_activity_event_kind_renders():
    """The renderer has a branch per kind; an unhandled one draws nothing."""
    from grant_writer.activity import Event, Todo

    app = _app_test()
    app.run()
    app.session_state["activity"] = [
        Event(PLAN, todos=(Todo("completed", "Extract requirements"),)),
        Event(DELEGATE, label="section-drafter"),
        Event(TOOL, label="write_file", detail="/applications/x/sections/need.md"),
        Event(TOOL, label="a_tool_with_no_icon_mapping"),
        Event(MESSAGE, detail="Draft complete."),
    ]
    app.run()

    assert not app.exception
    rendered = " ".join(block.value for block in app.markdown)
    assert "section-drafter" in rendered
    assert "/applications/x/sections/need.md" in rendered
    assert "Draft complete." in rendered


def test_a_run_cannot_start_without_an_application_id(monkeypatch):
    """It doubles as the thread id; a blank one would checkpoint to nowhere."""
    app = _app_test(monkeypatch)
    app.run()
    assert not app.button[0].disabled  # the app is in its normal, runnable state
    app.button[0].click().run()

    assert not app.exception
    assert app.session_state["phase"] == "failed"
    assert app.session_state["payload"] is None
    assert any("application id is required" in e.value for e in app.error)


@pytest.mark.parametrize("app_id", ["../evil", "/tmp/evil", "nsf/../.."])
def test_the_ui_refuses_an_application_id_that_escapes_the_tree(monkeypatch, app_id):
    """The submit handler writes the uploaded PDF to this path itself, outside
    FilesystemPermission, so the boundary has to hold here too."""
    app = _app_test(monkeypatch)
    app.run()
    app.text_input(key="app_id_input").set_value(app_id)
    app.button[0].click().run()

    assert not app.exception
    assert app.session_state["phase"] == "failed"
    assert app.session_state["payload"] is None
    assert any("invalid application id" in e.value for e in app.error)


def test_a_stopped_run_does_not_wedge_the_app(monkeypatch):
    """Streamlit aborts a script pass with a BaseException the run block's
    `except Exception` cannot see, leaving phase=RUNNING and no payload. Before
    the reset, the submit button stayed disabled with no way back."""
    app = _app_test(monkeypatch)
    app.run()
    app.session_state["phase"] = "running"
    app.session_state["payload"] = None
    app.session_state["active_app_id"] = "wedged-run"
    app.run()

    assert not app.exception
    assert app.session_state["phase"] == "stopped"
    assert not app.button[0].disabled
    assert any("checkpointed" in warning.value for warning in app.warning)


def test_missing_api_keys_disable_the_run_button(monkeypatch):
    """Better to grey out the button than to fail three tool calls into a run."""
    monkeypatch.setattr(
        "grant_writer.config.require_api_keys",
        lambda **_kwargs: ["ANTHROPIC_API_KEY"],
    )
    app = _app_test()
    app.run()

    assert not app.exception
    assert any("ANTHROPIC_API_KEY" in error.value for error in app.error)
    assert app.button[0].disabled
