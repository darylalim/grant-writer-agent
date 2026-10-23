"""The run table, its cost estimate, and the limits it enforces (invariant 26).

Offline throughout. The SQLite ledger runs the same SQL text the Postgres one
does -- only the placeholder and the lock statement differ, and those two are
pinned against a recording connection at the end -- so these cases exercise
the queries a deployment runs.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from grant_writer import agent as agent_module
from grant_writer import subagents as subagents_module
from grant_writer.agent import build_agent, build_run_ledger
from grant_writer.config import Settings
from grant_writer.runs import (
    FALLBACK_PRICE,
    PRICES,
    STALE_AFTER_S,
    LimitReached,
    Limits,
    PostgresRunLedger,
    SqliteRunLedger,
    UsageMeter,
    price_for,
)

NOON = datetime(2026, 10, 23, 12, tzinfo=UTC).timestamp()
LIMITS = Limits(daily_budget_usd=10.0, max_concurrent_runs=3)


def _meter() -> UsageMeter:
    return UsageMeter(search_cost_usd=0.01)


def _end(meter: UsageMeter, model: str | None, **usage) -> None:
    message = AIMessage("", usage_metadata=usage, response_metadata={})
    if model is not None:
        message.response_metadata["model_name"] = model
    meter.on_llm_end(SimpleNamespace(generations=[[ChatGeneration(message=message)]]))


# --- Prices and the meter --------------------------------------------------------


def test_a_dated_snapshot_prices_as_its_model_and_an_unknown_id_as_the_dearest():
    """Pins invariant 26.

    Unknown is charged high, not free: a `GRANT_WRITER_*_MODEL` override to an
    id nobody priced must make the cap bite sooner, never switch it off.
    """
    assert price_for("claude-sonnet-5-5-20261001") is PRICES["claude-sonnet-5-5"]
    assert price_for("claude-opus-9") is FALLBACK_PRICE
    assert price_for(None) is FALLBACK_PRICE
    assert FALLBACK_PRICE.output == max(p.output for p in PRICES.values())


def test_cached_input_is_priced_as_cache_and_not_counted_twice():
    """langchain-anthropic's `input_tokens` already includes cache reads and
    writes; charging it whole at the input rate, *plus* the cache lines, would
    overstate every cached call."""
    meter = _meter()
    _end(
        meter,
        "claude-opus-5-5",
        input_tokens=1_000_000 + 2_000_000 + 400_000,
        output_tokens=100_000,
        total_tokens=0,
        input_token_details={"cache_read": 2_000_000, "cache_creation": 400_000},
    )
    opus = PRICES["claude-opus-5-5"]
    expected = (
        1 * opus.input + 2 * opus.cache_read + 0.4 * opus.cache_write_5m
    ) + 0.1 * opus.output
    assert meter.cost_usd == pytest.approx(expected)
    assert (meter.model_calls, meter.cache_read_tokens) == (1, 2_000_000)
    assert meter.cache_write_tokens == 400_000


def test_a_one_hour_cache_write_is_priced_at_its_own_rate():
    meter = _meter()
    _end(
        meter,
        "claude-sonnet-5-5",
        input_tokens=1_000_000,
        output_tokens=0,
        total_tokens=0,
        input_token_details={
            "cache_creation": 0,
            "ephemeral_1h_input_tokens": 1_000_000,
        },
    )
    assert meter.cost_usd == pytest.approx(PRICES["claude-sonnet-5-5"].cache_write_1h)


def test_only_billed_searches_are_counted():
    meter = _meter()
    meter.on_tool_start({"name": "tavily_search"}, "")
    meter.on_tool_start({"name": "search_grants_gov"}, "")
    meter.on_tool_start(None, "")
    assert (meter.searches, meter.cost_usd) == (1, pytest.approx(0.01))


def test_a_subagents_usage_reaches_the_turns_meter(monkeypatch):
    """Pins invariant 26.

    Through the real drafting graph. A subagent runs inside the `task` tool and
    its messages never reach the orchestrator's stream, so a meter fed from
    the stream would miss every delegated section -- most of a draft's cost.
    One model stands in for every role, so the script runs across the boundary.
    """
    script = iter(
        [
            AIMessage(
                "",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "subagent_type": "section-drafter",
                            "description": "Draft the need statement.",
                        },
                        "id": "t1",
                    }
                ],
                usage_metadata={
                    "input_tokens": 10,
                    "output_tokens": 1,
                    "total_tokens": 11,
                },
                response_metadata={"model_name": "claude-opus-5-5"},
            ),
            AIMessage(
                "Drafted.",
                usage_metadata={
                    "input_tokens": 1_000,
                    "output_tokens": 500,
                    "total_tokens": 1_500,
                },
                response_metadata={"model_name": "claude-opus-5-5"},
            ),
            AIMessage(
                "Done.",
                usage_metadata={
                    "input_tokens": 20,
                    "output_tokens": 2,
                    "total_tokens": 22,
                },
                response_metadata={"model_name": "claude-opus-5-5"},
            ),
        ]
    )

    class _Shared(GenericFakeChatModel):
        def bind_tools(self, tools, **_kwargs):
            del tools
            return self

        def _generate(self, *_args, **_kwargs):
            return ChatResult(generations=[ChatGeneration(message=next(script))])

    def fake(*_args, **_kwargs):
        return _Shared(messages=iter(()))

    monkeypatch.setattr(agent_module, "build_model", fake)
    monkeypatch.setattr(subagents_module, "build_model", fake)
    graph = build_agent(Settings(backend_profile="server", enable_search=False))
    meter = _meter()

    graph.invoke(
        {"messages": [{"role": "user", "content": "go"}]},
        config={"configurable": {"thread_id": "meter"}, "callbacks": [meter]},
    )

    assert meter.model_calls == 3
    assert (meter.input_tokens, meter.output_tokens) == (1_030, 503)


# --- The ledger -------------------------------------------------------------------


def _ledger(tmp_path) -> SqliteRunLedger:
    return SqliteRunLedger(tmp_path / "runs.sqlite")


def _begin(ledger, user="alice", now=NOON, limits=LIMITS):
    return ledger.begin(
        user_id=user, thread_id=f"user:{user}:app", kind="draft", limits=limits, now=now
    )


def _spend(ledger, run_id, usd, now=NOON + 60):
    meter = _meter()
    meter.cost_usd = usd
    ledger.finish(run_id, "completed", meter, now=now)


def test_the_concurrent_limit_refuses_the_next_run_until_one_finishes(tmp_path):
    """Pins invariant 26."""
    ledger = _ledger(tmp_path)
    first, *_ = [_begin(ledger, user=f"u{i}") for i in range(3)]
    with pytest.raises(LimitReached, match="3 runs are in progress"):
        _begin(ledger, user="u9")
    _spend(ledger, first, 0.0)
    _begin(ledger, user="u9")


def test_a_run_that_never_finished_stops_holding_its_slot(tmp_path):
    """A process killed mid-turn never calls `finish`; its slot must not be
    held for ever, or three crashes would lock the deployment out."""
    ledger = _ledger(tmp_path)
    for i in range(3):
        _begin(ledger, user=f"u{i}", now=NOON - STALE_AFTER_S - 1)
    _begin(ledger, user="u9")


def test_the_daily_budget_is_per_user_and_resets_at_utc_midnight(tmp_path):
    """Pins invariant 26."""
    ledger = _ledger(tmp_path)
    _spend(ledger, _begin(ledger), 10.0)
    with pytest.raises(LimitReached, match=r"\$10\.00 used"):
        _begin(ledger)
    _begin(ledger, user="bob")
    tomorrow = datetime(2026, 10, 24, 0, 0, 1, tzinfo=UTC).timestamp()
    _begin(ledger, now=tomorrow)


def test_a_refused_turn_leaves_no_row(tmp_path):
    ledger = _ledger(tmp_path)
    _spend(ledger, _begin(ledger), 50.0)
    with pytest.raises(LimitReached):
        _begin(ledger)
    with sqlite3.connect(ledger.path) as conn:
        (count,) = conn.execute("SELECT COUNT(*) FROM grant_writer_runs").fetchone()
    assert count == 1


def test_the_limit_holds_under_concurrent_sessions(tmp_path):
    """Pins invariant 26.

    Twelve sessions racing for three slots. A count and an insert in separate
    transactions would let several see two in flight and all start a third.
    """
    ledger = _ledger(tmp_path)
    started, refused = [], []
    barrier = threading.Barrier(12)

    def race(i):
        barrier.wait()
        try:
            started.append(_begin(ledger, user=f"u{i}"))
        except LimitReached:
            refused.append(i)

    threads = [threading.Thread(target=race, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert (len(started), len(refused)) == (3, 9)


def test_the_run_table_holds_no_content(tmp_path):
    """Pins invariant 26.

    The row is the one record that outlives every tracing decision, so it may
    say who, which thread, when, how it ended and what it spent -- and nothing
    a proposal says. A column added later has to be argued for here.
    """
    ledger = _ledger(tmp_path)
    with sqlite3.connect(ledger.path) as conn:
        columns = [
            row[1] for row in conn.execute("PRAGMA table_info(grant_writer_runs)")
        ]
    assert columns == [
        "run_id",
        "user_id",
        "thread_id",
        "kind",
        "started_at",
        "ended_at",
        "outcome",
        "model_calls",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "searches",
        "cost_usd",
    ]


def test_a_finished_run_records_what_it_spent(tmp_path):
    ledger = _ledger(tmp_path)
    run_id = _begin(ledger)
    meter = _meter()
    _end(meter, "claude-sonnet-5-5", input_tokens=10, output_tokens=5, total_tokens=15)
    meter.on_tool_start({"name": "tavily_search"}, "")
    ledger.finish(run_id, "parked", meter, now=NOON + 5)
    with sqlite3.connect(ledger.path) as conn:
        row = conn.execute(
            "SELECT outcome, ended_at, model_calls, searches, cost_usd "
            "FROM grant_writer_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    assert row[:4] == ("parked", NOON + 5, 1, 1)
    assert row[4] == pytest.approx(meter.cost_usd)


def test_without_a_database_the_ledger_is_a_file_beside_the_checkpoints(tmp_path):
    """Never in memory: a limit that resets with the process is not a limit."""
    ledger = build_run_ledger(Settings(root=tmp_path))
    assert isinstance(ledger, SqliteRunLedger)
    assert ledger.path == tmp_path / ".grant_writer" / "runs.sqlite"


def test_the_postgres_ledger_locks_then_runs_the_same_queries():
    """The two lines the SQLite cases cannot reach: the advisory lock is taken
    first inside the transaction, and `?` becomes psycopg's `%s`."""
    sent: list[tuple[str, tuple]] = []

    class _Cursor:
        def __init__(self, sql):
            self.description = None if sql.startswith(("INSERT", "SELECT pg_")) else 1
            self.sql = sql

        def fetchall(self):
            return [{"n": 0}]

    class _Conn:
        def execute(self, sql, params=()):
            sent.append((sql, params))
            return _Cursor(sql)

        def transaction(self):
            return _Nothing()

    class _Nothing:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    class _Pool:
        def connection(self):
            class _Borrow(_Nothing):
                def __enter__(self):
                    return _Conn()

            return _Borrow()

    PostgresRunLedger(_Pool()).begin(
        user_id="alice", thread_id="t", kind="draft", limits=LIMITS, now=NOON
    )
    assert sent[0][0] == "SELECT pg_advisory_xact_lock(%s)"
    assert all("?" not in sql for sql, _ in sent)
    assert [sql.split()[0] for sql, _ in sent] == [
        "SELECT",
        "SELECT",
        "SELECT",
        "INSERT",
    ]
