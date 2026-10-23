"""Operations: logs, tracing, the approval record and the operator commands (E7).

Offline throughout. The page cases reuse `test_sign_in`'s harness, because the
records E7 adds are written where a signed-in turn runs.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import langsmith
import pytest
from langchain_core.messages import AIMessage
from langchain_core.tracers.langchain import LangChainTracer
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from grant_writer import cli, config, ops, runs
from grant_writer.config import Settings, tracing_mode, user_thread_id
from grant_writer.ops import (
    LOG_FIELDS,
    MAX_ERROR_CHARS,
    configure_logging,
    configure_tracing,
    log_event,
    parked_threads,
)
from grant_writer.runs import Approval, SqliteRunLedger, UsageMeter, approvals_for
from tests.test_sign_in import ALICE, APP, _Agent, _app, _button, _draft, _Ledger

SENTINEL = "zz-sentinel-proposal-text-7f3a"
FINAL = f"/applications/{APP}/final/p.md"
logger = logging.getLogger("grant_writer.test")


# --- Log lines ------------------------------------------------------------------------


def test_the_log_fields_are_the_vetted_set():
    """Pins invariant 27.

    Each name is a claim that its value holds no proposal content. A field
    added here has to say why in review; `log_event` refuses any other.
    """
    assert {
        "user_id",
        "thread_id",
        "run_id",
        "frontend",
        "phase",
        "kind",
        "outcome",
        "tool",
        "path",
        "decision",
        "count",
        "error_type",
        "error",
    } == LOG_FIELDS


@pytest.mark.parametrize("field", ["content", "message", "brief", "reason"])
def test_a_field_outside_the_set_is_refused(field):
    """Pins invariant 27."""
    # Through `Any`: a name outside the signature's types is the point.
    with pytest.raises(ValueError, match="content-free"):
        cast(Any, log_event)(logger, "x", **{field: SENTINEL})
    with pytest.raises(ValueError, match="content-free"):
        log_event(logger, "x", context={field: SENTINEL})


def test_a_line_is_one_json_object_with_the_exception_capped(caplog):
    with caplog.at_level(logging.INFO, logger="grant_writer"):
        log_event(
            logger,
            "run.failed",
            exc=RuntimeError("x" * 1000),
            context={"user_id": "u1", "frontend": "ui"},
            run_id="r1",
            phase=None,
        )
    (record,) = caplog.records
    line = json.loads(ops._JsonFormatter().format(record))
    assert line["event"] == "run.failed"
    assert (line["user_id"], line["run_id"], line["error_type"]) == (
        "u1",
        "r1",
        "RuntimeError",
    )
    assert len(line["error"]) == MAX_ERROR_CHARS
    assert "phase" not in line, "None fields are dropped, not sent as null"


def test_logging_is_configured_once_and_only_for_this_package():
    """Never the root logger, which would also collect what third-party
    libraries log -- text this package cannot vet."""
    package = logging.getLogger(ops.LOGGER_NAME)
    # An earlier page case may already have installed one; start from none.
    installed = [h for h in package.handlers if isinstance(h, ops._JsonHandler)]
    for handler in installed:
        package.removeHandler(handler)
    before = list(package.handlers)
    root_before = list(logging.getLogger().handlers)
    try:
        configure_logging("INFO")
        configure_logging("DEBUG")
        added = [h for h in package.handlers if h not in before]
        assert len(added) == 1 and isinstance(added[0], ops._JsonHandler)
        assert logging.getLogger().handlers == root_before
        assert package.propagate
    finally:
        for handler in package.handlers[:]:
            if handler not in before:
                package.removeHandler(handler)
        for handler in installed:
            package.addHandler(handler)


class _Failing(_Agent):
    """Streams the agent saying something, then fails mid-turn."""

    def stream(self, payload, *, config, **_kwargs):
        self.streamed.append((payload, config))
        yield {"model": {"messages": [AIMessage(content=SENTINEL)]}}
        raise RuntimeError("overloaded_error")


def test_a_failed_turn_is_logged_and_its_content_is_not(monkeypatch, caplog):
    """Pins invariant 27.

    The run handler's whole record of a failure, with a sentinel in the
    agent's own words: the log must say who, where and what failed, and the
    feed (not the log) must be where the words went.
    """
    agent, ledger = _Failing(), _Ledger()
    app = _app(monkeypatch, user=ALICE, agent=agent, ledger=ledger)
    with caplog.at_level(logging.INFO, logger="grant_writer"):
        _draft(app)

    assert not app.exception
    assert any(SENTINEL in e.detail for e in app.session_state["activity"])
    failed = [r for r in caplog.records if r.getMessage() == "run.failed"]
    assert len(failed) == 1
    fields = failed[0].fields
    assert fields["user_id"] == ALICE
    assert fields["thread_id"] == user_thread_id(ALICE, APP)
    assert fields["error_type"] == "RuntimeError"
    assert fields["run_id"] == "run-1"
    lines = [ops._JsonFormatter().format(r) for r in caplog.records]
    assert lines and not any(SENTINEL in line for line in lines)
    (_run_id, outcome, _meter) = ledger.finished[0]
    assert outcome == "failed"
    assert ledger.error_types == ["RuntimeError"]


def test_a_failed_turn_records_its_error_class_in_the_run_table(tmp_path):
    ledger = SqliteRunLedger(tmp_path / "runs.sqlite")
    run_id = ledger.begin(
        user_id="u", thread_id="t", kind="draft", limits=runs.Limits(10, 3)
    )
    meter = UsageMeter(search_cost_usd=0)
    ledger.finish(run_id, "failed", meter, error_type="RateLimitError")
    assert ledger.summary(since=0) == [("failed", "RateLimitError", 1)]


def test_a_signed_in_turn_shares_one_id_with_its_trace(monkeypatch):
    """E7's correlation id: the id given to `begin` is the trace's root run."""
    agent, ledger = _Agent(), _Ledger()
    app = _app(monkeypatch, user=ALICE, agent=agent, ledger=ledger)
    _draft(app)
    (_payload, run_config) = agent.streamed[0]
    assert run_config["run_id"].hex == ledger.begun[0]["run_id"]


def test_an_e4_run_table_gains_the_new_column(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` leaves an existing table alone, so without
    the migration every `finish` on an E4-era file would fail."""
    path = tmp_path / "runs.sqlite"
    columns = runs._COLUMNS.replace(",\n    error_type TEXT", "")
    with sqlite3.connect(path) as conn:
        conn.execute(f"CREATE TABLE grant_writer_runs ({columns})")
    ledger = SqliteRunLedger(path)
    run_id = ledger.begin(
        user_id="u", thread_id="t", kind="draft", limits=runs.Limits(10, 3)
    )
    ledger.finish(run_id, "completed", UsageMeter(search_cost_usd=0))
    SqliteRunLedger(path)  # and again: the migration is idempotent


# --- Tracing --------------------------------------------------------------------------


def test_hidden_tracing_reaches_the_tracers_client():
    """Pins invariant 27.

    Asserted on the client a `LangChainTracer` actually takes, and on what it
    does to a payload -- not on a flag of ours.
    """
    configure_tracing("hidden")
    client = LangChainTracer().client
    assert client._hide_run_inputs({"messages": SENTINEL}) == {}
    assert client._hide_run_outputs({"output": SENTINEL}) == {}
    assert client._hide_run_metadata({"rubric": SENTINEL}) == {}


def test_off_outranks_every_environment_spelling(monkeypatch):
    """Invariant 19's four switches, all on, and `off` still wins."""
    for name in (
        "LANGSMITH_TRACING_V2",
        "LANGCHAIN_TRACING_V2",
        "LANGSMITH_TRACING",
        "LANGCHAIN_TRACING",
    ):
        monkeypatch.setenv(name, "true")
    cast(Any, langsmith.utils.get_env_var).cache_clear()
    try:
        assert langsmith.utils.tracing_is_enabled() is True
        configure_tracing("off")
        assert langsmith.utils.tracing_is_enabled() is False
    finally:
        cast(Any, langsmith.utils.get_env_var).cache_clear()


def test_full_tracing_leaves_langsmith_to_the_environment():
    configure_tracing("full")
    assert langsmith.run_trees._CLIENT is None


def test_an_unknown_mode_is_refused():
    with pytest.raises(ValueError, match="tracing mode"):
        configure_tracing("partial")


def test_a_shared_process_hides_content_unless_told_otherwise(monkeypatch):
    """Decision 16: the default leans toward hiding."""
    assert tracing_mode(shared=True) == "hidden"
    assert tracing_mode(shared=False) == "full"
    monkeypatch.setattr(config, "TRACING", "off")
    assert tracing_mode(shared=False) == "off"
    monkeypatch.setattr(config, "TRACING", "full")
    assert tracing_mode(shared=True) == "full"


def test_a_signed_in_page_traces_with_content_hidden(monkeypatch):
    """Pins invariant 27."""
    app = _app(monkeypatch, user=ALICE)
    app.run()
    assert not app.exception
    assert langsmith.run_trees._CLIENT is not None
    assert langsmith.run_trees._CLIENT._hide_inputs is True


def test_a_single_user_page_leaves_tracing_alone(monkeypatch):
    app = _app(monkeypatch, user=None)
    app.run()
    assert not app.exception
    assert langsmith.run_trees._CLIENT is None


# --- The approval record --------------------------------------------------------------


def test_the_approval_record_holds_no_content(tmp_path):
    """Pins invariant 27.

    Who, when, which thread, run and file, and the decision. Not the file's
    text and not the reason a human typed, which goes to the agent only.
    """
    ledger = SqliteRunLedger(tmp_path / "runs.sqlite")
    with sqlite3.connect(ledger.path) as conn:
        columns = [
            row[1] for row in conn.execute("PRAGMA table_info(grant_writer_approvals)")
        ]
    assert columns == [
        "approval_id",
        "decided_at",
        "user_id",
        "thread_id",
        "run_id",
        "frontend",
        "tool",
        "path",
        "decision",
    ]


def test_nothing_in_the_package_updates_or_deletes_an_approval():
    """Pins invariant 27.

    Append-only is a property of the code that touches the table, so it is
    checked against every source file rather than one module's constants.
    """
    sources = Path(runs.__file__).parent.glob("*.py")
    pattern = re.compile(
        r"(UPDATE|DELETE\s+FROM|TRUNCATE|DROP\s+TABLE)\s+grant_writer_approvals",
        re.IGNORECASE,
    )
    assert not [p.name for p in sources if pattern.search(p.read_text())]


def test_each_decision_names_its_request_and_a_blind_one_names_none():
    requests = [
        {"name": "write_file", "args": {"file_path": FINAL, "content": SENTINEL}},
        {"name": "delete", "args": {"path": "/applications/x/sections/a.md"}},
    ]
    decisions = [
        {"type": "reject", "message": SENTINEL},
        {"type": "reject", "message": SENTINEL},
    ]
    assert approvals_for(decisions, requests) == [
        Approval(tool="write_file", path=FINAL, decision="reject"),
        Approval(
            tool="delete", path="/applications/x/sections/a.md", decision="reject"
        ),
    ]
    assert SENTINEL not in repr(approvals_for(decisions, requests))
    # Invariant 5's floor of one: a parked thread nothing could be read from.
    assert approvals_for([{"type": "approve"}], []) == [
        Approval(tool=None, path=None, decision="approve")
    ]


def test_recorded_approvals_land_as_rows(tmp_path):
    ledger = SqliteRunLedger(tmp_path / "runs.sqlite")
    ledger.record_approvals(
        [Approval("write_file", FINAL, "approve")],
        user_id=None,
        thread_id="t",
        run_id=None,
        frontend="cli",
        now=5.0,
    )
    with sqlite3.connect(ledger.path) as conn:
        rows = conn.execute(
            "SELECT decided_at, user_id, thread_id, frontend, tool, path, decision "
            "FROM grant_writer_approvals"
        ).fetchall()
    assert rows == [(5.0, None, "t", "cli", "write_file", FINAL, "approve")]


class _Parks(_Agent):
    """A turn that ends on an approval for one `final/` write."""

    def stream(self, payload, *, config, **_kwargs):
        self.streamed.append((payload, config))
        if not isinstance(payload, Command):
            self.parked = [FINAL]
            yield {"__interrupt__": ()}
        else:
            self.parked = []


def _parked_app(monkeypatch, ledger):
    agent = _Parks()
    app = _app(monkeypatch, user=ALICE, agent=agent, ledger=ledger)
    _draft(app)
    assert app.session_state["phase"] == "awaiting"
    return app, agent


def test_an_approval_is_recorded_before_it_is_sent(monkeypatch):
    """Pins invariant 27.

    One row per request, naming the run that parked, on the viewer's own
    thread -- and recorded by the time the resume streams.
    """
    ledger = _Ledger()
    app, agent = _parked_app(monkeypatch, ledger)
    _button(app, "Approve").click().run()

    assert not app.exception
    (approvals, where) = ledger.approvals[0]
    assert approvals == [Approval(tool="write_file", path=FINAL, decision="approve")]
    assert where == {
        "user_id": ALICE,
        "thread_id": user_thread_id(ALICE, APP),
        "run_id": "run-1",
        "frontend": "ui",
    }
    assert isinstance(agent.streamed[-1][0], Command)


def test_an_approval_that_cannot_be_recorded_is_not_sent(monkeypatch):
    """Pins invariant 27.

    The panel stays, so the same click works once the database answers. A
    rejection still goes through: it writes nothing, and refusing it too
    would leave a broken database holding a thread with no way out.
    """
    ledger = _Ledger()
    app, agent = _parked_app(monkeypatch, ledger)
    ledger.refuse_record = OSError("database is locked")
    _button(app, "Approve").click().run()

    assert not app.exception
    assert len(agent.streamed) == 1
    assert app.session_state["phase"] == "awaiting"
    assert "could not be recorded" in app.error[0].value

    app.text_input(key="reject_reason_0").set_value(SENTINEL)
    _button(app, "Reject").click().run()
    assert not app.exception
    assert isinstance(agent.streamed[-1][0], Command)


def test_a_reject_reason_is_sent_to_the_agent_and_not_recorded(monkeypatch):
    ledger = _Ledger()
    app, agent = _parked_app(monkeypatch, ledger)
    app.text_input(key="reject_reason_0").set_value(SENTINEL)
    _button(app, "Reject").click().run()

    (approvals, _where) = ledger.approvals[0]
    assert approvals == [Approval(tool="write_file", path=FINAL, decision="reject")]
    resume = agent.streamed[-1][0]
    assert resume.resume["decisions"][0]["message"] == SENTINEL


# --- Parked threads -------------------------------------------------------------------


@dataclass
class _State:
    n: int = 0


def _graph(checkpointer):
    def ask(state: _State) -> dict:
        interrupt("approve?")
        return {"n": state.n + 1}

    builder = StateGraph(_State)
    builder.add_node("ask", ask)
    builder.add_edge(START, "ask")
    builder.add_edge("ask", END)
    return builder.compile(checkpointer=checkpointer)


def _thread(thread_id):
    return {"configurable": {"thread_id": thread_id}}


def test_only_a_thread_still_waiting_is_parked():
    """A real interrupt through a real checkpointer: parked, resumed, and a
    thread that never asked."""
    saver = InMemorySaver()
    graph = _graph(saver)
    graph.invoke({"n": 0}, _thread("waiting"))
    graph.invoke({"n": 0}, _thread("answered"))
    graph.invoke(Command(resume="yes"), _thread("answered"))
    saver.put(
        {"configurable": {"thread_id": "idle", "checkpoint_ns": ""}},
        empty_checkpoint(),
        {"source": "input", "step": -1},
        {},
    )

    found = parked_threads(saver.list(None), older_than_s=0)
    assert [p.thread_id for p in found] == ["waiting"]
    assert parked_threads(saver.list(None), older_than_s=3 * 86400) == []


# --- Operator commands ----------------------------------------------------------------


def _operator_settings(monkeypatch, tmp_path, database_url=None):
    settings = replace(
        Settings(root=tmp_path),
        checkpoint_db=tmp_path / ".grant_writer" / "checkpoints.sqlite",
        database_url=database_url,
    )
    monkeypatch.setattr(cli, "persistent_settings", lambda **_k: settings)
    return settings


def _cli(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["grant-writer", *argv])
    return cli.main()


def test_healthcheck_is_ready_without_a_database(monkeypatch, tmp_path, capsys):
    _operator_settings(monkeypatch, tmp_path)
    assert _cli(monkeypatch, "healthcheck") == 0
    assert "ready" in capsys.readouterr().out


def test_healthcheck_fails_when_the_database_does_not_answer(
    monkeypatch, tmp_path, capsys
):
    _operator_settings(monkeypatch, tmp_path, database_url="postgresql://x/y")

    def unreachable(_settings):
        raise OSError("connection refused")

    monkeypatch.setattr(cli, "check_database", unreachable)
    assert _cli(monkeypatch, "healthcheck") == 1
    assert "not ready: OSError" in capsys.readouterr().err


def test_ops_check_is_quiet_when_nothing_needs_attention(monkeypatch, tmp_path, capsys):
    _operator_settings(monkeypatch, tmp_path)
    assert _cli(monkeypatch, "ops-check") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["alerts"] == [] and report["runs"] == 0


def test_ops_check_reports_rate_limits_and_a_long_parked_thread(
    monkeypatch, tmp_path, capsys
):
    settings = _operator_settings(monkeypatch, tmp_path)
    ledger = SqliteRunLedger(settings.run_table_db)
    run_id = ledger.begin(
        user_id="u", thread_id="t", kind="draft", limits=runs.Limits(10, 3)
    )
    ledger.finish(
        run_id, "failed", UsageMeter(search_cost_usd=0), error_type="RateLimitError"
    )
    _graph(cli.build_checkpointer(settings)).invoke({"n": 0}, _thread("user:u:nsf"))

    assert _cli(monkeypatch, "ops-check", "--parked-days", "0") == 1
    report = json.loads(capsys.readouterr().out)
    assert report["parked"] == ["user:u:nsf"]
    assert report["errors"] == {"RateLimitError": 1}
    assert any("RateLimitError" in a for a in report["alerts"])
    assert any("user:u:nsf" in a for a in report["alerts"])


def test_the_operator_cli_records_its_approvals(monkeypatch, tmp_path):
    """Pins invariant 27.

    Against the shared database the CLI is an operator answering for a
    team's thread, so it records like the UI -- and refuses an approval it
    cannot record.
    """
    settings = replace(Settings(root=tmp_path), database_url="postgresql://x/y")
    ledger = _Ledger()
    monkeypatch.setattr(cli, "build_run_ledger", lambda _s: ledger)
    agent = _Agent(parked=[FINAL])
    answers = iter(["approve"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    result = cli._resolve_interrupt(agent, _thread("t"), settings)
    assert result == {"decisions": [{"type": "approve"}]}
    (approvals, where) = ledger.approvals[0]
    assert approvals == [Approval(tool="write_file", path=FINAL, decision="approve")]
    assert where["frontend"] == "cli" and where["user_id"] is None

    ledger.refuse_record = OSError("database is locked")
    answers = iter(["approve"])
    assert cli._resolve_interrupt(agent, _thread("t"), settings) is None


def test_the_laptop_cli_writes_no_record(monkeypatch, tmp_path):
    def no_ledger(_settings):
        raise AssertionError("a single-user approval reached the record")

    monkeypatch.setattr(cli, "build_run_ledger", no_ledger)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "approve")
    agent = _Agent(parked=[FINAL])
    result = cli._resolve_interrupt(agent, _thread("t"), Settings(root=tmp_path))
    assert result == {"decisions": [{"type": "approve"}]}
