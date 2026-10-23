"""The run table: what each signed-in turn cost, and the limits read from it.

One record per turn -- who, which thread, which graph, when, how it ended, and
what it spent -- and **no content**: no message, no file text, no profile. That
is what lets it outlive any tracing decision (E7) and still answer how much,
how often and how well. The spending limit and the concurrent-run limit read
the same rows, so a dashboard and a refusal can never disagree about what a
user has spent (invariant 26).

Both limits are checked in `RunLedger.begin`, *atomically with* the insert that
claims the slot: a count taken in one statement and an insert in another would
let two sessions both see two runs in flight and both start a third. SQLite
gets that from `BEGIN IMMEDIATE`, Postgres from a transaction-scoped advisory
lock. The SQL between the two is the same text, so the offline tests of the
SQLite ledger exercise the queries the deployment runs.

Cost is an estimate made here, from the usage each model call reports and a
price table, because the API returns tokens and not dollars. It errs high: a
model id the table does not know is priced as the dearest one it does, so a
rename overstates spending rather than making it free.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Generator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from langchain_core.callbacks import BaseCallbackHandler

RunKind = Literal["draft", "discover"]
Outcome = Literal["completed", "parked", "failed", "refused", "stopped"]


@dataclass(frozen=True)
class Price:
    """US dollars per million tokens."""

    input: float
    output: float
    cache_read: float

    @property
    def cache_write_5m(self) -> float:
        return self.input * 1.25

    @property
    def cache_write_1h(self) -> float:
        return self.input * 2


# First-party API rates, as published when this was written (October 2026).
# Matched by prefix, so a dated snapshot of a listed id prices as that id.
# Haiku's rate is its <=100K-prompt one; a longer prompt costs more, and this
# table cannot see prompt length, which is one more reason the cap is an
# estimate. Update it here when prices change -- nothing fails if it is stale.
PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(input=4.00, output=20.00, cache_read=0.20),
    "claude-sonnet-5-5": Price(input=2.00, output=10.00, cache_read=0.20),
    "claude-haiku-5-5": Price(input=0.10, output=0.50, cache_read=0.01),
}

# The price an unrecognised model id is charged at: the dearest in the table,
# so a `GRANT_WRITER_*_MODEL` override to an id nobody priced overstates spend
# rather than reporting it as free -- the cap fails toward refusing.
FALLBACK_PRICE = max(PRICES.values(), key=lambda p: p.output)

# The tools that cost money per call. grants.gov's API is free, so its search
# is not here.
BILLED_SEARCH_TOOLS = frozenset({"tavily_search"})

# A run that never finished -- the process died mid-turn -- stops holding a
# concurrent-run slot after this long. Generous, because a drafting turn can
# run for many minutes and releasing a live run's slot early lets one more
# start than the limit allows.
STALE_AFTER_S = 2 * 60 * 60


def price_for(model: str | None) -> Price:
    """The table entry whose id `model` starts with, or the fallback."""
    for prefix, price in PRICES.items():
        if model and model.startswith(prefix):
            return price
    return FALLBACK_PRICE


class UsageMeter(BaseCallbackHandler):
    """Totals one turn's model usage and billed searches, across every agent.

    Passed in the turn's `callbacks`, which LangChain carries into each child
    run -- including a subagent invoked inside the `task` tool, whose messages
    never appear in the orchestrator's stream. Counting from the stream instead
    would miss every delegated section, the bulk of a draft's cost.

    Locked because subagents can run in parallel threads. `search_cost_usd`
    has no default, so the one estimate lives in `Settings.search_cost_usd`.
    """

    def __init__(self, *, search_cost_usd: float) -> None:
        self._lock = threading.Lock()
        self._search_cost_usd = search_cost_usd
        self.model_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        self.searches = 0
        self.cost_usd = 0.0

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None)
                if not usage:
                    continue
                metadata = getattr(message, "response_metadata", None) or {}
                self._add(usage, metadata.get("model_name"))

    def on_tool_start(
        self, serialized: dict[str, Any] | None, input_str: str, **kwargs: Any
    ) -> None:
        if (serialized or {}).get("name") in BILLED_SEARCH_TOOLS:
            with self._lock:
                self.searches += 1
                self.cost_usd += self._search_cost_usd

    def _add(self, usage: Mapping[str, Any], model: str | None) -> None:
        # langchain-anthropic reports `input_tokens` *including* cache reads
        # and writes, with the split under `input_token_details`; a 1h write is
        # reported by TTL and its generic key zeroed so it is not counted twice.
        details = usage.get("input_token_details") or {}
        read = details.get("cache_read") or 0
        write_1h = details.get("ephemeral_1h_input_tokens") or 0
        write_5m = (details.get("ephemeral_5m_input_tokens") or 0) + (
            details.get("cache_creation") or 0
        )
        total_in = usage.get("input_tokens") or 0
        uncached = max(total_in - read - write_5m - write_1h, 0)
        output = usage.get("output_tokens") or 0
        price = price_for(model)
        cost = (
            uncached * price.input
            + read * price.cache_read
            + write_5m * price.cache_write_5m
            + write_1h * price.cache_write_1h
            + output * price.output
        ) / 1_000_000
        with self._lock:
            self.model_calls += 1
            self.input_tokens += total_in
            self.output_tokens += output
            self.cache_read_tokens += read
            self.cache_write_tokens += write_5m + write_1h
            self.cost_usd += cost


class LimitReached(Exception):
    """A turn was refused before it started. The message is for the user."""


@dataclass(frozen=True)
class Limits:
    daily_budget_usd: float
    """Estimated spend one user may reach per UTC day before turns are refused."""

    max_concurrent_runs: int
    """Turns that may be in flight at once across the whole deployment."""


def _day_start(now: float) -> float:
    """The start of `now`'s UTC day, as epoch seconds."""
    day = datetime.fromtimestamp(now, UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return day.timestamp()


# The table, as both backends create it. Times are epoch seconds rather than a
# timestamp type, so the one schema and the one set of queries serve SQLite and
# Postgres alike; `to_timestamp(started_at)` reads them in Postgres.
_COLUMNS = """
    run_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    started_at DOUBLE PRECISION NOT NULL,
    ended_at DOUBLE PRECISION,
    outcome TEXT,
    model_calls INTEGER NOT NULL DEFAULT 0,
    input_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    cache_read_tokens BIGINT NOT NULL DEFAULT 0,
    cache_write_tokens BIGINT NOT NULL DEFAULT 0,
    searches INTEGER NOT NULL DEFAULT 0,
    cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0
"""
_CREATE = (
    f"CREATE TABLE IF NOT EXISTS grant_writer_runs ({_COLUMNS})",
    "CREATE INDEX IF NOT EXISTS grant_writer_runs_user_started "
    "ON grant_writer_runs (user_id, started_at)",
    "CREATE INDEX IF NOT EXISTS grant_writer_runs_open "
    "ON grant_writer_runs (started_at) WHERE ended_at IS NULL",
)
# `?` is the placeholder; the Postgres ledger rewrites it to `%s`.
_IN_FLIGHT = (
    "SELECT COUNT(*) FROM grant_writer_runs WHERE ended_at IS NULL AND started_at > ?"
)
_SPENT = (
    "SELECT COALESCE(SUM(cost_usd), 0) FROM grant_writer_runs "
    "WHERE user_id = ? AND started_at >= ?"
)
_INSERT = (
    "INSERT INTO grant_writer_runs (run_id, user_id, thread_id, kind, started_at) "
    "VALUES (?, ?, ?, ?, ?)"
)
_FINISH = (
    "UPDATE grant_writer_runs SET ended_at = ?, outcome = ?, model_calls = ?, "
    "input_tokens = ?, output_tokens = ?, cache_read_tokens = ?, "
    "cache_write_tokens = ?, searches = ?, cost_usd = ? WHERE run_id = ?"
)

Execute = Callable[[str, tuple[Any, ...]], list[tuple[Any, ...]]]


class RunLedger(Protocol):
    def begin(
        self,
        *,
        user_id: str,
        thread_id: str,
        kind: RunKind,
        limits: Limits,
        now: float | None = None,
    ) -> str: ...

    def finish(
        self,
        run_id: str,
        outcome: Outcome,
        meter: UsageMeter,
        *,
        now: float | None = None,
    ) -> None: ...


class _Ledger:
    """The limit logic, over whichever transaction a backend supplies."""

    def _transaction(self) -> AbstractContextManager[Execute]:
        raise NotImplementedError

    def begin(
        self,
        *,
        user_id: str,
        thread_id: str,
        kind: RunKind,
        limits: Limits,
        now: float | None = None,
    ) -> str:
        """Claim a slot for one turn, or raise `LimitReached`.

        The checks and the insert share one locked transaction, so the limit
        holds under concurrent sessions; see the module docstring.
        """
        now = time.time() if now is None else now
        run_id = uuid.uuid4().hex
        with self._transaction() as execute:
            ((in_flight,),) = execute(_IN_FLIGHT, (now - STALE_AFTER_S,))
            if in_flight >= limits.max_concurrent_runs:
                msg = (
                    f"{in_flight} runs are in progress, the most this deployment "
                    "allows at once. Nothing was started; try again shortly."
                )
                raise LimitReached(msg)
            ((spent,),) = execute(_SPENT, (user_id, _day_start(now)))
            if spent >= limits.daily_budget_usd:
                msg = (
                    f"Today's limit of ${limits.daily_budget_usd:.2f} in estimated "
                    f"model and search cost is reached (${spent:.2f} used). Nothing "
                    "was started; the limit resets at 00:00 UTC."
                )
                raise LimitReached(msg)
            execute(_INSERT, (run_id, user_id, thread_id, kind, now))
        return run_id

    def finish(
        self,
        run_id: str,
        outcome: Outcome,
        meter: UsageMeter,
        *,
        now: float | None = None,
    ) -> None:
        """Record how a turn ended and what it spent, releasing its slot."""
        now = time.time() if now is None else now
        with self._transaction() as execute:
            execute(
                _FINISH,
                (
                    now,
                    outcome,
                    meter.model_calls,
                    meter.input_tokens,
                    meter.output_tokens,
                    meter.cache_read_tokens,
                    meter.cache_write_tokens,
                    meter.searches,
                    meter.cost_usd,
                    run_id,
                ),
            )


class SqliteRunLedger(_Ledger):
    """The run table in a SQLite file: one machine, any number of processes.

    A connection per transaction rather than one shared: Streamlit serves each
    session from its own thread, and `BEGIN IMMEDIATE` takes the file's write
    lock up front, which is what makes the check-then-insert atomic across
    processes as well as threads.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as execute:
            for statement in _CREATE:
                execute(statement, ())

    @contextmanager
    def _transaction(self) -> Generator[Execute]:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        try:
            conn.execute("BEGIN IMMEDIATE")

            def execute(sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
                return conn.execute(sql, params).fetchall()

            try:
                yield execute
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()


# Any fixed 64-bit key; it names this table's lock among the database's others.
_ADVISORY_LOCK_KEY = 0x6772616E7477  # "grantw"


class PostgresRunLedger(_Ledger):
    """The run table in the deployment's Postgres, on the checkpointer's pool.

    Its table comes from `grant-writer db-setup` (`setup`), like the savers'.
    The advisory lock is transaction-scoped, so it is released on commit or
    rollback and cannot be leaked by a crashed session.
    """

    def __init__(self, pool: Any) -> None:
        self.pool = pool

    def setup(self) -> None:
        with self._transaction() as execute:
            for statement in _CREATE:
                execute(statement, ())

    @contextmanager
    def _transaction(self) -> Generator[Execute]:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))

            def execute(sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
                cursor = conn.execute(sql.replace("?", "%s"), params)
                if cursor.description is None:
                    return []
                return [tuple(row.values()) for row in cursor.fetchall()]

            yield execute
