"""What the CLI and the Streamlit UI must agree on.

Two frontends now read the same stream and send the same brief. Both couplings
fail silently: a renamed key in a `deepagents` tool call turns into a blank
label rather than an error, and a kickoff instruction that drifts in one
frontend produces a differently-steered proposal from identical inputs. Neither
shows up as a crash, so they are pinned here.
"""

from __future__ import annotations

import shutil
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
    streamlit = pytest.importorskip("streamlit", reason="dev dependency; run `uv sync`")
    from streamlit.testing.v1 import AppTest

    # `get_agent` is `@st.cache_resource`, whose store is global to the process
    # rather than per-AppTest. A case that patches `build_agent` would otherwise
    # be handed whichever fake an earlier case cached under the same
    # (profile, approve, search) key -- passing or failing on test order.
    streamlit.cache_resource.clear()

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
        # A UI-only kind, spelled as a literal because it is defined in
        # streamlit_app.py and that file cannot be imported without running it.
        # It is not in `activity.py` because nothing in the agent's stream
        # produces it -- see the PROMPT comment there.
        Event("prompt", detail="Tighten the need section."),
    ]
    app.run()

    assert not app.exception
    rendered = " ".join(block.value for block in app.markdown)
    assert "section-drafter" in rendered
    assert "/applications/x/sections/need.md" in rendered
    assert "Draft complete." in rendered
    assert "Tighten the need section." in rendered


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


def test_the_run_button_is_disabled_while_a_turn_is_in_flight(monkeypatch):
    """`busy` is read near the top of the script, before the submit handler
    sets the phase, so the pass that starts a run would draw the form enabled
    and then spend minutes streaming with a live button on screen. The handler
    reruns instead of falling through, so the streaming pass is a later one
    that reads phase=RUNNING and draws the form disabled.

    Asserting on the tree left after the turn *finishes* pins the wrong thing:
    by then the phase is terminal and the button should be live again. The
    agent here halts the pass from inside the run block instead, so the tree
    AppTest captures is the one drawn mid-stream.
    """
    streamlit = pytest.importorskip("streamlit", reason="dev dependency")

    class _HaltingAgent:
        """Stops the script inside the run block, freezing the in-flight tree.

        `st.stop` raises a BaseException subclass, so the run block's
        `except Exception` does not catch it and the phase stays RUNNING --
        the same property the STOPPED guard at the top of the app relies on.
        """

        def stream(self, *_args, **_kwargs):
            streamlit.stop()

    monkeypatch.setattr(
        "grant_writer.agent.build_agent", lambda *_a, **_k: _HaltingAgent()
    )
    app = _app_test(monkeypatch)
    app.run()
    app.text_input(key="app_id_input").set_value("zz-pytest-inflight")
    app.button[0].click().run()

    assert not app.exception
    assert app.session_state["phase"] == "running"  # genuinely mid-turn
    assert app.button[0].disabled


def test_the_run_button_comes_back_once_the_turn_ends(monkeypatch):
    """The other half of the rule above, and the one the first attempt at it
    got backwards. The streaming pass draws the form disabled and nothing
    afterwards would redraw it, so the run block reruns once the phase is
    terminal. Without that the app finishes with a greyed-out submit button
    under its own "Run finished" banner and no way to start another run short
    of touching an unrelated widget.
    """

    class _FinishedAgent:
        """Streams nothing, so the turn ends immediately and offline."""

        def stream(self, *_args, **_kwargs):
            return iter(())

    monkeypatch.setattr(
        "grant_writer.agent.build_agent", lambda *_a, **_k: _FinishedAgent()
    )
    app = _app_test(monkeypatch)
    app.run()
    app.text_input(key="app_id_input").set_value("zz-pytest-finished")
    app.button[0].click().run()

    assert not app.exception
    assert app.session_state["phase"] == "done"
    assert not app.button[0].disabled


def test_a_follow_up_sends_what_the_cli_chat_loop_sends(monkeypatch):
    """The UI could only ever start a fresh draft, so refining one meant
    dropping to `grant-writer chat`. A follow-up has to send exactly what
    `cli._chat` sends -- a plain turn on the existing thread -- and not another
    `draft_request`, which re-briefs the agent to run the whole process from the
    top. Both produce output, which is why this is pinned rather than noticed.
    """
    seen: list[tuple[object, dict]] = []

    class _RecordingAgent:
        """Streams nothing, but keeps what it was asked to stream."""

        def stream(self, payload, config, **_kwargs):
            seen.append((payload, config))
            return iter(())

    monkeypatch.setattr(
        "grant_writer.agent.build_agent", lambda *_a, **_k: _RecordingAgent()
    )
    app = _app_test(monkeypatch)
    app.run()
    app.text_input(key="app_id_input").set_value("zz-pytest-followup").run()
    app.button[0].click().run()
    assert app.session_state["phase"] == "done"
    seen.clear()  # drop the opening brief; the follow-up is what is under test

    app.chat_input(key="followup_input").set_value("Tighten the need section.").run()

    assert not app.exception
    assert len(seen) == 1, "a follow-up must run exactly one turn"
    payload, config = seen[0]
    # Byte-for-byte the shape of `cli._chat`'s `_run(agent, {...}, config)`.
    assert payload == {
        "messages": [{"role": "user", "content": "Tighten the need section."}]
    }
    # Same thread id as the run it continues, which is the whole point: the
    # checkpoint carries the plan, todos, and history across to this turn.
    assert config["configurable"]["thread_id"] == "zz-pytest-followup"
    assert app.session_state["active_app_id"] == "zz-pytest-followup"


def test_a_follow_up_cannot_be_sent_while_an_approval_is_pending(monkeypatch):
    """AWAITING is not idleness. The graph is parked on an interrupt, and a
    plain message resumes nothing -- it starts a new turn, abandoning the
    pending submission-bound write rather than approving or rejecting it. The
    approval panel is the only way out of this state.
    """
    agent = SimpleNamespace(
        get_state=lambda _config: SimpleNamespace(
            tasks=[
                SimpleNamespace(
                    interrupts=[
                        SimpleNamespace(
                            value={
                                "action_requests": [
                                    {
                                        "name": "write_file",
                                        "args": {
                                            "file_path": "/applications/x/final/a.md",
                                            "content": "Final text.",
                                        },
                                    }
                                ]
                            }
                        )
                    ]
                )
            ]
        )
    )
    monkeypatch.setattr("grant_writer.agent.build_agent", lambda *_a, **_k: agent)

    app = _app_test(monkeypatch)
    app.run()
    app.session_state["active_app_id"] = "zz-pytest-parked"
    app.session_state["phase"] = "awaiting"
    app.run()

    assert not app.exception
    assert app.chat_input(key="followup_input").disabled
    # The way forward is still on screen -- this gates the input, not the turn.
    assert any(button.label == "Approve" for button in app.button)


def test_the_follow_up_input_is_disabled_before_any_run(monkeypatch):
    """There is no thread to continue yet. `monkeypatch` forces the credential
    check to pass, so a disabled input here means "no active run" rather than
    the missing-key state every control shares.
    """
    app = _app_test(monkeypatch)
    app.run()

    assert not app.exception
    assert not app.button[0].disabled  # the app is otherwise runnable
    assert app.chat_input(key="followup_input").disabled


def test_the_approval_panel_renders_every_pending_write(monkeypatch):
    """The panel is an `st.fragment`, and fragment misuse -- a bad rerun scope,
    or writing into a container it was never called in -- raises at run time,
    not import time. Nothing else in the suite reaches AWAITING, so without
    this a refactor here ships a dead approval prompt standing in front of a
    submission-bound file.
    """
    requests = [
        {
            "name": "write_file",
            "args": {
                "file_path": "/applications/x/final/narrative.md",
                # The content branch: the UI shows the drafted text at the
                # prompt, which is the thing the CLI cannot do.
                "content": "# Narrative\n\nDrafted text.",
            },
        },
        # No `content`, so this one falls to the st.json branch.
        {
            "name": "write_file",
            "args": {"file_path": "/applications/x/final/budget.md"},
        },
    ]
    agent = SimpleNamespace(
        get_state=lambda _config: SimpleNamespace(
            tasks=[
                SimpleNamespace(
                    interrupts=[SimpleNamespace(value={"action_requests": requests})]
                )
            ]
        )
    )
    monkeypatch.setattr("grant_writer.agent.build_agent", lambda *_a, **_k: agent)

    app = _app_test(monkeypatch)
    app.run()
    app.session_state["phase"] = "awaiting"
    app.session_state["active_app_id"] = "zz-pytest-approval"
    app.run()

    assert not app.exception
    assert "Approval required" in {sub.value for sub in app.subheader}
    # Both writes are offered, and the count is per request, not per interrupt.
    assert any("2 write(s)" in caption.value for caption in app.caption)
    assert {"Approve", "Reject"} <= {button.label for button in app.button}
    # Shown as source, not rendered: what is approved has to be the bytes that
    # get written. A rendered view hides exactly the things worth checking on a
    # submission -- a citation whose link text and URL disagree, most of all.
    assert "# Narrative" in " ".join(block.value for block in app.code)
    assert "Drafted text." not in " ".join(block.value for block in app.markdown)


def test_an_unreadable_interrupt_does_not_offer_a_plain_approve(monkeypatch):
    """`pending_action_requests` returns [] for a renamed `deepagents` payload
    key or a non-dict interrupt value -- it skips both without raising -- and
    the phase is AWAITING regardless, because the graph really is parked. The
    panel used to draw "1 write(s) ... read each one before approving" above no
    expanders at all, and Approve then resumed a submission-bound write nobody
    had seen. Exactly the silent-blanking failure activity.py was centralised
    to prevent.
    """
    agent = SimpleNamespace(
        get_state=lambda _config: SimpleNamespace(
            # An interrupt whose value carries no readable action requests.
            tasks=[SimpleNamespace(interrupts=[SimpleNamespace(value={})])]
        )
    )
    monkeypatch.setattr("grant_writer.agent.build_agent", lambda *_a, **_k: agent)

    app = _app_test(monkeypatch)
    app.run()
    app.session_state["phase"] = "awaiting"
    app.session_state["active_app_id"] = "zz-pytest-blind"
    app.run()

    assert not app.exception
    approve = next(button for button in app.button if button.label == "Approve")
    assert approve.disabled
    assert any("no pending write could be read" in e.value for e in app.error)
    # The reassuring count is gone; nothing claims a file is on offer.
    assert not any("write(s) to" in caption.value for caption in app.caption)
    # Rejecting stays available throughout -- it releases the graph without
    # writing, so it is the safe way out of this state.
    assert not next(b for b in app.button if b.label == "Reject").disabled

    # Approving is reachable, but only as a second deliberate act.
    app.checkbox[0].set_value(True).run()
    assert not next(b for b in app.button if b.label == "Approve").disabled


def test_the_sidebar_is_frozen_while_a_turn_is_in_flight(monkeypatch):
    """Gating only the submit button moves which control throws a run away.
    Every sidebar widget interaction aborts the streaming pass at the next
    element call, dropping minutes of model calls into STOPPED.

    The keys matter as much as the `disabled`: without them a widget's identity
    comes from its parameters, `disabled` included, so a run would change their
    identity and hand back defaults -- silently swapping the backend profile
    for the very pass that builds the graph.
    """
    streamlit = pytest.importorskip("streamlit", reason="dev dependency")

    class _HaltingAgent:
        def stream(self, *_args, **_kwargs):
            streamlit.stop()

    monkeypatch.setattr(
        "grant_writer.agent.build_agent", lambda *_a, **_k: _HaltingAgent()
    )
    app = _app_test(monkeypatch)
    app.run()
    # Move them off their defaults, so a reset would be visible as well.
    app.selectbox(key="profile_select").set_value("server").run()
    app.toggle(key="search_toggle").set_value(False).run()
    app.text_input(key="app_id_input").set_value("zz-pytest-sidebar")
    app.button[0].click().run()

    assert not app.exception
    assert app.session_state["phase"] == "running"
    assert app.selectbox(key="profile_select").disabled
    assert app.toggle(key="approve_toggle").disabled
    assert app.toggle(key="search_toggle").disabled
    assert app.number_input(key="recursion_limit_input").disabled
    # Still holding what was chosen, not the defaults.
    assert app.selectbox(key="profile_select").value == "server"
    assert app.toggle(key="search_toggle").value is False


def test_the_approval_preview_can_still_be_read_as_prose(monkeypatch):
    """Source is the default, but a grant narrative is prose and monospace is a
    poor way to read two pages of it. The toggle exists so the safe default
    does not cost readability -- and so the rendered view is a deliberate act
    rather than what the reviewer is handed."""
    requests = [
        {
            "name": "write_file",
            "args": {
                "file_path": "/applications/x/final/narrative.md",
                "content": "# Narrative\n\nDrafted text.",
            },
        }
    ]
    agent = SimpleNamespace(
        get_state=lambda _config: SimpleNamespace(
            tasks=[
                SimpleNamespace(
                    interrupts=[SimpleNamespace(value={"action_requests": requests})]
                )
            ]
        )
    )
    monkeypatch.setattr("grant_writer.agent.build_agent", lambda *_a, **_k: agent)

    app = _app_test(monkeypatch)
    app.run()
    app.session_state["phase"] = "awaiting"
    app.session_state["active_app_id"] = "zz-pytest-view"
    app.run()

    control = app.segmented_control[0]
    assert control.value == "Source"
    control.set_value("Rendered").run()

    assert not app.exception
    assert "Drafted text." in " ".join(block.value for block in app.markdown)


def test_the_rejection_reason_does_not_leak_into_the_next_interrupt(monkeypatch):
    """A turn interrupts once per file under `final/`, so the panel is redrawn
    for the next file straight after a resume. With a fixed widget key the box
    kept its text across that: rejecting narrative.md with "budget is wrong"
    left that sitting in the box for budget.md, and a second Reject sent the
    previous file's complaint, so the agent fixed the wrong thing. The keys
    carry `approval_round`, which `resume_with` bumps.
    """
    sent = []

    class _Agent:
        def get_state(self, _config):
            requests = [{"name": "write_file", "args": {"file_path": "/f/next.md"}}]
            return SimpleNamespace(
                tasks=[
                    SimpleNamespace(
                        interrupts=[
                            SimpleNamespace(value={"action_requests": requests})
                        ]
                    )
                ]
            )

        def stream(self, payload, **_kwargs):
            sent.append(payload)
            # Interrupts again immediately, on the turn's next final/ write.
            return iter([{"__interrupt__": ()}])

    monkeypatch.setattr("grant_writer.agent.build_agent", lambda *_a, **_k: _Agent())

    app = _app_test(monkeypatch)
    app.run()
    app.session_state["phase"] = "awaiting"
    app.session_state["active_app_id"] = "zz-pytest-reason"
    app.run()

    first = f"reject_reason_{app.session_state['approval_round']}"
    app.text_input(key=first).set_value("budget is wrong")
    next(button for button in app.button if button.label == "Reject").click().run()

    assert not app.exception
    assert sent[0].resume == {
        "decisions": [{"type": "reject", "message": "budget is wrong"}]
    }
    # Parked on the next file's approval, with a box that says nothing about it.
    assert app.session_state["phase"] == "awaiting"
    second = f"reject_reason_{app.session_state['approval_round']}"
    assert second != first
    assert app.text_input(key=second).value == ""


def test_approving_resumes_the_graph_from_inside_the_fragment(monkeypatch):
    """Approving has to actually resume the graph, with the decision list
    `approval_decisions` builds. Rendering the panel proves nothing about that.

    What this does NOT cover, despite touching the same code: the scope of the
    rerun in `resume_with`. In a browser a button inside `@st.fragment`
    produces a fragment rerun, where `scope="fragment"` is legal and would
    leave the graph parked on its interrupt behind a dead Approve button.
    AppTest runs the click as a full-script rerun instead, so mutating the
    scope here fails with `StreamlitAPIException: scope="fragment" can only be
    specified ... during fragment reruns` rather than with the silent stall.
    The comment on `resume_with` is what guards that; this test cannot.
    """
    resumed = []

    class _Agent:
        def get_state(self, _config):
            requests = [{"name": "write_file", "args": {"file_path": "/f/a.md"}}]
            return SimpleNamespace(
                tasks=[
                    SimpleNamespace(
                        interrupts=[
                            SimpleNamespace(value={"action_requests": requests})
                        ]
                    )
                ]
            )

        def stream(self, payload, **_kwargs):
            resumed.append(payload)
            return iter(())

    monkeypatch.setattr("grant_writer.agent.build_agent", lambda *_a, **_k: _Agent())

    app = _app_test(monkeypatch)
    app.run()
    app.session_state["phase"] = "awaiting"
    app.session_state["active_app_id"] = "zz-pytest-resume"
    app.run()
    next(button for button in app.button if button.label == "Approve").click().run()

    assert not app.exception
    # The graph was actually resumed, with the decision list `approval_decisions`
    # builds -- one per action request, which is what resume validates against.
    assert len(resumed) == 1
    assert resumed[0].resume == {"decisions": [{"type": "approve"}]}
    assert app.session_state["phase"] == "done"


def test_the_application_id_input_is_not_inside_the_form():
    """The precondition every file-browser case below silently depends on.

    `st.form` batches its widgets: their values reach the server only when
    Submit is pressed. The id is also what the browser reads back to pick an
    application, so inside the form it could only ever show one already
    submitted this session -- reading an earlier application meant starting a
    fresh billed run on it.

    AppTest cannot catch that by behaviour. `ElementTree.get_widget_states`
    walks every widget and serialises it regardless of `form_id`, so
    `set_value(...).run()` publishes a form widget immediately and the cases
    below pass either way. They did, against a browser where the pane stayed
    empty. So pin the structural fact the harness does model -- `form_id` is on
    the proto -- rather than the behaviour it fakes.
    """
    app = _app_test()
    app.run()

    assert not app.exception
    assert app.text_input(key="app_id_input").form_id == ""
    # The other run inputs belong in the form; nothing reads them back.
    assert [t.label for t in app.text_input if t.form_id] == ["Funder"]


def test_typing_an_application_id_browses_it_without_starting_a_run(
    application_with_drafts,
):
    """The read path must not cost a turn.

    This one cannot catch the input moving back into the form -- verified: it
    passes against that code, because `get_widget_states` publishes a form
    widget anyway. The check above is the one that fails there, and it is the
    only one that can. What this pins instead is the `browse_id` expression:
    drop the typed-id clause and leave the `active_app_id` fallback, and the
    browser goes back to showing only what a submit put there. That regression
    the harness does model, and this fails on it.
    """
    app = _app_test()
    app.run()
    app.text_input(key="app_id_input").set_value(application_with_drafts).run()

    assert not app.exception
    assert {m.label: m.value for m in app.metric}["Files"] == "4"
    # No run was started: the browser reads disk, it does not touch the graph.
    assert app.session_state["phase"] == "idle"
    assert app.session_state["active_app_id"] == ""


def test_the_browse_picker_fills_the_id_box_and_clears_itself(
    application_with_drafts,
):
    """The picker is an input method for the id box, not a rival source of
    truth -- see `_pick_application`. Two controls deciding which application is
    on screen disagree the moment a run starts on one the picker is not showing,
    and whichever the code reads first is then wrong half the time.

    The self-clear looks redundant and is not: left set, picking `alpha`, typing
    `beta`, then picking `alpha` again fires no change event -- same value -- so
    the box would sit on `beta` while the picker read `alpha`.
    """
    app = _app_test()
    app.run()
    app.selectbox(key="browse_pick").set_value(application_with_drafts).run()

    assert not app.exception
    assert app.text_input(key="app_id_input").value == application_with_drafts
    assert {m.label: m.value for m in app.metric}["Files"] == "4"
    assert app.session_state["browse_pick"] is None
    # Reading is not running.
    assert app.session_state["phase"] == "idle"


@pytest.fixture
def application_with_a_pdf():
    """A real application directory holding a PDF, removed afterwards.

    It has to live under the project's own `applications/` -- that is the only
    tree `config.application_dir` resolves an id into, and the app builds its
    Settings from PROJECT_ROOT rather than taking one. The directory is
    gitignored, and the id is prefixed so a failed teardown is recognisable.
    """
    app_id = "zz-pytest-pdf"
    app_dir = PROJECT_ROOT / "applications" / app_id
    app_dir.mkdir(parents=True, exist_ok=True)
    # Never parsed -- the viewer fails long before it reads the bytes.
    (app_dir / "solicitation.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    try:
        yield app_id
    finally:
        shutil.rmtree(app_dir, ignore_errors=True)


def test_a_pdf_in_the_application_directory_renders(application_with_a_pdf):
    """`st.pdf` lives in a separate `streamlit-pdf` package, which the dev group
    pulls in via the `streamlit[pdf]` extra. Plain `streamlit` is a working
    install that passes every other case here, so nothing else notices: the
    failure is a raised StreamlitAPIException where the document should be, and
    only on the file browser's PDF branch. The app saves the uploaded
    solicitation into this directory itself, so every run started in the UI has
    one sitting in the listing.
    """
    app = _app_test()
    app.run()
    app.text_input(key="app_id_input").set_value(application_with_a_pdf)
    app.run()

    assert not app.exception
    assert app.radio(key="file_pick").value == "solicitation.pdf"


@pytest.fixture
def application_with_drafts():
    """A real application directory spanning the browser's rendering branches.

    Same constraint as `application_with_a_pdf`: it has to live under the
    project's own `applications/`, since that is the only tree
    `config.application_dir` resolves an id into.
    """
    app_id = "zz-pytest-drafts"
    app_dir = PROJECT_ROOT / "applications" / app_id
    contents = {
        "requirements.md": "# Requirements\n\n[NEEDS INPUT: the deadline]\n",
        "research/notes.yaml": "# funder priorities\nawards:\n  - median: 400000\n",
        "sections/need.md": "# Need\n\nWorking draft.\n",
        # The link text and the URL deliberately disagree: rendered, the URL is
        # simply not on screen.
        "final/need.md": "# Need\n\nSee [the 2025 report](https://example.org/r.pdf).\n",
    }
    for relative, text in contents.items():
        target = app_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    try:
        yield app_id
    finally:
        shutil.rmtree(app_dir, ignore_errors=True)


def test_the_file_browser_renders_from_inside_its_fragment(application_with_drafts):
    """The browser is an `st.fragment` so that picking a file does not rerun the
    whole app: that click replayed the activity feed through st.markdown,
    re-walked the application directory, and re-read every draft to recount
    gaps and re-read the verdict, all to swap one pane.

    What this pins is that the fragment is wired up at all. Fragment misuse --
    writing into a container that received no write during the full app run --
    raises at run time, not import time. It cannot pin the saving itself:
    AppTest replays every interaction as a full-script rerun, so a fragment
    rerun and an app rerun are indistinguishable from here. The counts below
    stand in for the arguments the fragment is handed rather than computes.
    """
    app = _app_test()
    app.run()
    app.text_input(key="app_id_input").set_value(application_with_drafts)
    app.run()

    assert not app.exception
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Files"] == "4"
    # Only requirements.md carries a marker, and `review/` is excluded by design.
    assert metrics["Needs input"] == "1"


def test_a_submission_bound_draft_is_shown_as_source(application_with_drafts):
    """The approval panel defaults `final/` to source because st.markdown shows
    a citation's link text and not its URL, and those are the files that go to
    a funder. That argument does not expire when the approval is clicked --
    the browser is where the same file gets re-read, and it offered no way to
    see the bytes. Working drafts are read for their content, so they render.
    """
    app = _app_test()
    app.run()
    app.text_input(key="app_id_input").set_value(application_with_drafts)
    app.run()

    app.radio(key="file_pick").set_value("sections/need.md").run()
    assert not app.exception
    assert "Working draft." in " ".join(block.value for block in app.markdown)

    app.radio(key="file_pick").set_value("final/need.md").run()
    assert not app.exception
    # On screen as source, which is the whole point: rendered, this line shows
    # "the 2025 report" and the URL that would actually be submitted is hidden.
    assert "https://example.org/r.pdf" in " ".join(block.value for block in app.code)


def test_a_non_markdown_file_is_not_run_through_the_markdown_renderer(
    application_with_drafts,
):
    """Five of the six suffixes the browser called text were mangled by
    st.markdown: a YAML comment became an <h1>, the indented block became a
    code fence, and a CSV collapsed into one paragraph. A viewer that quietly
    shows something other than the file is worse than one that refuses to.
    """
    app = _app_test()
    app.run()
    app.text_input(key="app_id_input").set_value(application_with_drafts)
    app.run()
    app.radio(key="file_pick").set_value("research/notes.yaml").run()

    assert not app.exception
    assert "# funder priorities" in " ".join(block.value for block in app.code)
    assert "funder priorities" not in " ".join(block.value for block in app.markdown)


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
