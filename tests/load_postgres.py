"""Load test for a deployment's database, with a stub model (E6). Costs nothing.

    TEST_DATABASE_URL=postgresql://... uv run python -m tests.load_postgres
    uv run python -m tests.load_postgres --url ... --sessions 5 10 25 --turns 3

What a shared deployment strains is not the model, which is billed and
rate-limited elsewhere, but the one process's pool and the checkpoint writes
every step makes. Those don't depend on what the model says, so a model that
answers instantly measures them with no cost -- and more harshly than real
use, since nothing waits on a network reply between writes.

Each size drives N simultaneous sessions through **one** drafting graph, as
the UI's `st.cache_resource` does, each on its own per-user thread, and each
turn claims and releases a run-table slot the way the run block does. A turn
writes a section into state, reads it back and replies, so a checkpoint
carries real file content. Reported per size: checkpoint write latency, pool
waits, peak-RSS growth, errors, and how long `ops-check`'s full checkpoint
scan (`list(None)`, E7's open item) takes at that history.

Not collected by pytest (no `test_` prefix), and never part of the default
suite. `tests/test_postgres.py` runs it once at a tiny size so it cannot rot.
It needs CREATEDB on the URL: it works in a scratch database of its own and
drops it afterwards, so it never touches the tables a deployment uses.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import statistics
import sys
import threading
import time
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from grant_writer import agent, subagents
from grant_writer.config import Settings, user_thread_id
from grant_writer.ops import parked_threads
from grant_writer.runs import Limits, UsageMeter

# About what a drafted section weighs, so a checkpoint carries a realistic blob.
_SECTION = ("The project will serve rural clinics across three counties. " * 140)[:8000]


@contextmanager
def scratch_database(url: str) -> Generator[str]:
    """A fresh database on `url`'s server, migrated by `db-setup`'s own code.

    Yields its conninfo and drops it afterwards, with `FORCE` so a pool still
    holding a connection cannot keep it alive. The migration is the real one,
    so a case built on this also proves `setup_database` runs on a clean server.
    """
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    name = f"grant_writer_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    scratch = make_conninfo(url, dbname=name)
    try:
        agent.setup_database(replace(Settings(), database_url=scratch))
        yield scratch
    finally:
        close_pools(replace(Settings(), database_url=scratch))
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


def close_pools(settings: Settings) -> None:
    """Close the cached pool `settings` would use, then forget every pool.

    What a process restart does to the pool: the next graph built connects
    afresh. The cache lookup returns the pool already open for these values
    (or opens one, which is closed straight away).
    """
    agent._pool_for(settings).close()
    agent._connection_pool.cache_clear()


class StubModel(GenericFakeChatModel):
    """Answers from the conversation alone, so one instance serves every thread.

    A human turn gets a `write_file`, its result a `read_file`, and that
    result a closing reply carrying usage, which the run's meter prices. No
    shared script to advance, so concurrent sessions cannot take each other's
    replies -- the failure a scripted iterator would have here.
    """

    def bind_tools(self, tools: Any, **_kwargs: Any) -> StubModel:
        del tools
        return self

    def _generate(self, messages: list[Any], *_args: Any, **_kwargs: Any):
        last = messages[-1]
        turn = sum(isinstance(m, HumanMessage) for m in messages)
        path = f"/applications/load/sections/turn-{turn}.md"
        if isinstance(last, ToolMessage) and last.name == "write_file":
            reply = _call("read_file", {"file_path": path})
        elif isinstance(last, ToolMessage):
            reply = AIMessage(
                "Drafted.",
                usage_metadata={
                    "input_tokens": 2_000,
                    "output_tokens": 400,
                    "total_tokens": 2_400,
                },
                response_metadata={"model_name": "claude-sonnet-5-5"},
            )
        else:
            reply = _call("write_file", {"file_path": path, "content": _SECTION})
        return ChatResult(generations=[ChatGeneration(message=reply)])


def _call(name: str, args: dict[str, Any]) -> AIMessage:
    call = {"name": name, "args": args, "id": uuid.uuid4().hex}
    return AIMessage("", tool_calls=[call])


@contextmanager
def stub_models() -> Generator[None]:
    """Every role built as `StubModel` while the graph is assembled."""

    def build(spec: str, *, effort: str) -> BaseChatModel:
        del spec, effort
        return StubModel(messages=iter(()))

    modules = (agent, subagents)
    saved = [module.build_model for module in modules]
    for module in modules:
        setattr(module, "build_model", build)  # noqa: B010 - ty refuses a plain assignment
    try:
        yield
    finally:
        for module, original in zip(modules, saved, strict=True):
            setattr(module, "build_model", original)  # noqa: B010


@dataclass(frozen=True)
class LoadResult:
    sessions: int
    turns: int
    pool_size: int
    errors: int
    error_types: list[str]
    wall_s: float
    turn_p50_ms: float
    turn_p95_ms: float
    checkpoint_writes: int
    checkpoint_p50_ms: float
    checkpoint_p95_ms: float
    checkpoint_max_ms: float
    pool_requests: int
    pool_waited: int
    pool_wait_mean_ms: float
    peak_rss_growth_mb: float
    checkpoints_scanned: int
    ops_scan_ms: float


def _percentile(values: list[float], q: int) -> float:
    if len(values) < 2:
        return values[0] if values else 0.0
    return statistics.quantiles(values, n=100, method="inclusive")[q - 1]


def _peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Bytes on macOS, kilobytes on Linux.
    return peak / 2**20 if sys.platform == "darwin" else peak / 2**10


def _timed(fn: Callable[..., Any], into: list[float], lock: threading.Lock):
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            with lock:
                into.append((time.perf_counter() - start) * 1000)

    return wrapper


def run_load(url: str, *, sessions: int, turns: int, pool_size: int) -> LoadResult:
    """Drive `sessions` concurrent sessions of `turns` turns each against `url`."""
    settings = replace(
        Settings(),
        backend_profile="server",
        enable_search=False,
        database_url=url,
        db_pool_max_size=pool_size,
        max_concurrent_runs=sessions,
    )
    # Every size on a fresh pool, so its stats and its RSS are its own.
    agent._connection_pool.cache_clear()
    with stub_models():
        graph = agent.build_agent(settings)
    ledger = agent.build_run_ledger(settings)
    limits = Limits(daily_budget_usd=1e9, max_concurrent_runs=sessions)
    pool = agent._pool_for(settings)

    lock = threading.Lock()
    writes: list[float] = []
    turn_ms: list[float] = []
    errors: list[str] = []
    saver = graph.checkpointer
    saver.put = _timed(saver.put, writes, lock)
    saver.put_writes = _timed(saver.put_writes, writes, lock)

    barrier = threading.Barrier(sessions)

    def session(index: int) -> None:
        user = f"load{index}"
        thread_id = user_thread_id(user, f"app-{uuid.uuid4().hex[:8]}")
        config = {"configurable": {"thread_id": thread_id}}
        barrier.wait()
        for turn in range(turns):
            start = time.perf_counter()
            meter = UsageMeter(search_cost_usd=settings.search_cost_usd)
            try:
                run_id = ledger.begin(
                    user_id=user, thread_id=thread_id, kind="draft", limits=limits
                )
                outcome, error_type = "failed", None
                try:
                    graph.invoke(
                        {"messages": [{"role": "user", "content": f"turn {turn}"}]},
                        config={**config, "callbacks": [meter]},
                    )
                    outcome = "completed"
                except Exception as exc:
                    error_type = type(exc).__name__
                    raise
                finally:
                    ledger.finish(run_id, outcome, meter, error_type=error_type)
            except Exception as exc:  # noqa: BLE001 - counted, the load goes on
                with lock:
                    errors.append(type(exc).__name__)
                continue
            with lock:
                turn_ms.append((time.perf_counter() - start) * 1000)

    rss_before = _peak_rss_mb()
    pool.pop_stats()
    started = time.perf_counter()
    workers = [threading.Thread(target=session, args=(i,)) for i in range(sessions)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    wall_s = time.perf_counter() - started
    stats = pool.pop_stats()

    scan_start = time.perf_counter()
    listed = list(saver.list(None))
    parked_threads(listed, older_than_s=0)
    ops_scan_ms = (time.perf_counter() - scan_start) * 1000

    close_pools(settings)
    waited = stats.get("requests_queued", 0)
    return LoadResult(
        sessions=sessions,
        turns=turns,
        pool_size=pool_size,
        errors=len(errors),
        error_types=sorted(set(errors)),
        wall_s=round(wall_s, 2),
        turn_p50_ms=round(_percentile(turn_ms, 50), 1),
        turn_p95_ms=round(_percentile(turn_ms, 95), 1),
        checkpoint_writes=len(writes),
        checkpoint_p50_ms=round(_percentile(writes, 50), 2),
        checkpoint_p95_ms=round(_percentile(writes, 95), 2),
        checkpoint_max_ms=round(max(writes, default=0.0), 2),
        pool_requests=stats.get("requests_num", 0),
        pool_waited=waited,
        pool_wait_mean_ms=round(stats.get("requests_wait_ms", 0) / waited, 2)
        if waited
        else 0.0,
        peak_rss_growth_mb=round(_peak_rss_mb() - rss_before, 1),
        checkpoints_scanned=len(listed),
        ops_scan_ms=round(ops_scan_ms, 1),
    )


_COLUMNS = (
    ("sessions", "sessions"),
    ("errors", "errors"),
    ("wall_s", "wall s"),
    ("turn_p95_ms", "turn p95 ms"),
    ("checkpoint_p50_ms", "ckpt p50 ms"),
    ("checkpoint_p95_ms", "ckpt p95 ms"),
    ("checkpoint_max_ms", "ckpt max ms"),
    ("pool_waited", "pool waits"),
    ("pool_wait_mean_ms", "wait mean ms"),
    ("peak_rss_growth_mb", "RSS +MB"),
    ("checkpoints_scanned", "ckpts"),
    ("ops_scan_ms", "ops scan ms"),
)


def _table(results: list[LoadResult]) -> str:
    rows = [[label for _key, label in _COLUMNS]]
    rows += [[str(getattr(r, key)) for key, _label in _COLUMNS] for r in results]
    widths = [max(len(row[i]) for row in rows) for i in range(len(_COLUMNS))]
    return "\n".join(
        "  ".join(cell.rjust(width) for cell, width in zip(row, widths, strict=True))
        for row in rows
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--url",
        default=os.environ.get("TEST_DATABASE_URL"),
        help="server to make a scratch database on (default: TEST_DATABASE_URL)",
    )
    parser.add_argument("--sessions", type=int, nargs="+", default=[5, 10, 25])
    parser.add_argument("--turns", type=int, default=3)
    parser.add_argument(
        "--pool-size",
        type=int,
        default=Settings().db_pool_max_size,
        help="the pool's max size (default: the deployment's)",
    )
    parser.add_argument("--json", action="store_true", help="print JSON lines")
    args = parser.parse_args(argv)
    if not args.url:
        parser.error("set TEST_DATABASE_URL or pass --url")

    # The checkpoint scan reads every size's history, so sizes run in one
    # database and grow it: the last row is the scan at the largest history.
    results = []
    with scratch_database(args.url) as url:
        for n in args.sessions:
            result = run_load(
                url, sessions=n, turns=args.turns, pool_size=args.pool_size
            )
            results.append(result)
            if args.json:
                print(json.dumps(asdict(result)), flush=True)
    if not args.json:
        print(f"turns per session: {args.turns}; pool max size: {args.pool_size}")
        print(_table(results))
    return 1 if any(r.errors for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
