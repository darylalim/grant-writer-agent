"""Postgres, for real (E6). Opt-in: skipped unless TEST_DATABASE_URL is set.

`test_database.py` pins the wiring offline: which saver a `Settings` gets,
one pool, and the kwargs that pool connects with. It cannot see anything that
constructs fine and then fails on the first query, which is how every Postgres
mistake here would arrive: a migration that does not apply, a lock that is not
taken, a row read back in the wrong shape. So each case below goes through a
real query on a real server. CI runs them in a `postgres` job of their own;
locally, point TEST_DATABASE_URL at a server you can create databases on.

These cases claim no invariant. Each one already has an offline test that
runs everywhere; this file shows the same thing holding on the backend a
deployment uses.
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.store.postgres import PostgresStore
from langgraph.types import Command
from psycopg import sql

from grant_writer import agent, cli, config, subagents
from grant_writer.activity import approval_decisions, is_parked, pending_action_requests
from grant_writer.config import Settings, user_thread_id
from grant_writer.runs import Approval, LimitReached, Limits, UsageMeter
from tests.load_postgres import close_pools, run_load

pytestmark = pytest.mark.postgres

PROFILE = "/memories/org/AGENTS.md"
MEMORIES = ("filesystem", "memories")
SKILLS = ("filesystem", "skills")


@pytest.fixture
def pg(postgres_url, monkeypatch) -> Settings:
    """Deployed settings on the scratch database, emptied for this case.

    Emptied rather than recreated: a database per case would make this file
    slow enough that nobody runs it. The migration tables are kept, so the
    schema stays the one `db-setup` built.
    """
    settings = replace(
        Settings(),
        database_url=postgres_url,
        backend_profile="server",
        enable_search=False,
    )
    with agent._pool_for(settings).connection() as conn:
        tables = [
            sql.Identifier(row["tablename"])
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename NOT LIKE '%migrations'"
            )
        ]
        conn.execute(sql.SQL("TRUNCATE {}").format(sql.SQL(", ").join(tables)))
    monkeypatch.setattr(config, "DATABASE_URL", postgres_url)
    return settings


def _cli(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr("sys.argv", ["grant-writer", *argv])
    return cli.main()


def _tables(settings: Settings) -> dict[str, set[str]]:
    with agent._pool_for(settings).connection() as conn:
        rows = conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public'"
        ).fetchall()
    tables: dict[str, set[str]] = {}
    for row in rows:
        tables.setdefault(row["table_name"], set()).add(row["column_name"])
    return tables


# --- Deploy steps --------------------------------------------------------------


def test_db_setup_is_idempotent_and_migrates_an_e4_run_table(pg, monkeypatch):
    """What a deploy runs every time, against a table from before E7.

    E4's run table had no `error_type`, and `CREATE TABLE IF NOT EXISTS`
    leaves such a table alone, so without the added-column migration every
    `finish` would fail on the first deploy after upgrading.
    """
    with agent._pool_for(pg).connection() as conn:
        conn.execute("ALTER TABLE grant_writer_runs DROP COLUMN error_type")
    assert "error_type" not in _tables(pg)["grant_writer_runs"]

    assert _cli(monkeypatch, "db-setup") == 0
    assert _cli(monkeypatch, "db-setup") == 0

    tables = _tables(pg)
    assert "error_type" in tables["grant_writer_runs"]
    expected = {
        "checkpoints",
        "checkpoint_writes",
        "checkpoint_blobs",
        "store",
        "grant_writer_runs",
        "grant_writer_approvals",
    }
    assert expected <= tables.keys()


def test_healthcheck_is_ready_against_a_real_server(pg, monkeypatch, capsys):
    del pg
    assert _cli(monkeypatch, "healthcheck") == 0
    assert "ready" in capsys.readouterr().out


# --- Threads and the Store ------------------------------------------------------


def _scripted(monkeypatch, *messages: AIMessage) -> None:
    class _Scripted(GenericFakeChatModel):
        def bind_tools(self, tools, **_kwargs):
            del tools
            return self

    script = iter(messages)

    def build(*_args, **_kwargs):
        return _Scripted(messages=script)

    monkeypatch.setattr(agent, "build_model", build)
    monkeypatch.setattr(subagents, "build_model", build)


def _call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": call_id}])


def test_a_parked_write_survives_a_restart_and_resumes_on_it(pg, monkeypatch, capsys):
    """Invariants 11 and 25 across a process boundary, on the real savers.

    The thread parks on a profile write; every pool is closed, as a restart
    or a second replica would see it; the graph built afresh still reads the
    interrupt, `ops-check` reports it, and the approval it then receives lands
    the write in the Postgres Store. The in-memory saver cannot show any of
    this: its interrupt never left the process that made it.
    """
    planted = "Recorded after approval."
    thread = {"configurable": {"thread_id": user_thread_id("alice", "nsf")}}
    _scripted(
        monkeypatch,
        _call("write_file", {"file_path": PROFILE, "content": planted}, "w"),
    )
    agent.build_agent(pg).invoke(
        {"messages": [{"role": "user", "content": "record it"}]}, config=thread
    )

    close_pools(pg)
    _scripted(monkeypatch, AIMessage("Recorded."))
    graph = agent.build_agent(pg)
    assert is_parked(graph, thread)
    requests = pending_action_requests(graph, thread)
    assert [r["args"]["file_path"] for r in requests] == [PROFILE]

    assert _cli(monkeypatch, "ops-check", "--parked-days", "0") == 1
    report = json.loads(capsys.readouterr().out)
    assert any(thread["configurable"]["thread_id"] in a for a in report["alerts"])

    decisions = approval_decisions(requests, approve=True, message="")
    graph.invoke(Command(resume={"decisions": decisions}), config=thread)
    assert not is_parked(graph, thread)

    close_pools(pg)
    stored = PostgresStore(agent._pool_for(pg)).get(MEMORIES, "/org/AGENTS.md")
    assert stored is not None
    assert planted in str(stored.value["content"])


def test_two_users_naming_an_application_alike_get_two_threads(pg, monkeypatch):
    """Invariant 23's thread half, as rows in the deployment's database."""
    _scripted(monkeypatch, AIMessage("For Alice."), AIMessage("For Bob."))
    graph = agent.build_agent(pg)
    for user in ("alice", "bob"):
        graph.invoke(
            {"messages": [{"role": "user", "content": f"I am {user}"}]},
            config={"configurable": {"thread_id": user_thread_id(user, "nsf")}},
        )

    close_pools(pg)
    graph = agent.build_agent(pg)
    for user, reply in (("alice", "For Alice."), ("bob", "For Bob.")):
        config = {"configurable": {"thread_id": user_thread_id(user, "nsf")}}
        texts = [m.content for m in graph.get_state(config).values["messages"]]
        assert texts == [f"I am {user}", reply]


def test_profile_edits_survive_a_restart_and_skills_do_not(pg):
    """Decision 10 and invariant 6 on the real `PostgresStore`.

    `test_database.py` shows the same with an in-memory stand-in for the
    Store. This is the restart a deployment actually performs: seed, edit,
    close every connection, seed again.
    """
    agent._build_store(pg)
    store = PostgresStore(agent._pool_for(pg))
    profile = store.get(MEMORIES, "/org/AGENTS.md")
    skills = store.search(SKILLS, limit=100)
    assert profile is not None and skills
    assert not any(item.key.startswith("/org/") for item in skills)

    store.put(MEMORIES, "/org/AGENTS.md", {**profile.value, "content": "edited"})
    store.put(SKILLS, skills[0].key, {**skills[0].value, "content": "stale"})

    close_pools(pg)
    agent._build_store(pg)
    store = PostgresStore(agent._pool_for(pg))
    profile = store.get(MEMORIES, "/org/AGENTS.md")
    skill = store.get(SKILLS, skills[0].key)
    assert profile is not None and profile.value["content"] == "edited"
    assert skill is not None and skill.value["content"] != "stale"


# --- The run table -------------------------------------------------------------


def test_the_limit_holds_under_a_race_on_the_advisory_lock(pg):
    """Invariant 26's `pg_advisory_xact_lock`, which SQLite never exercises.

    Twelve sessions, three slots, more racers than the pool has connections,
    so some also queue for one. Without the lock, the count and the insert
    run in separate snapshots and more than three start.
    """
    ledger = agent.build_run_ledger(pg)
    limits = Limits(daily_budget_usd=100.0, max_concurrent_runs=3)
    started, refused = [], []
    barrier = threading.Barrier(12)

    def race(i):
        barrier.wait()
        try:
            started.append(
                ledger.begin(
                    user_id=f"u{i}", thread_id=f"t{i}", kind="draft", limits=limits
                )
            )
        except LimitReached:
            refused.append(i)

    threads = [threading.Thread(target=race, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert (len(started), len(refused)) == (3, 9)


def test_spend_failures_and_approvals_round_trip_through_postgres(
    pg, monkeypatch, capsys
):
    """`finish`, `summary`, `record_approvals` and `ops-check`, on real rows.

    Every query here is written once with `?` and rewritten to `%s` for
    Postgres, so a query only SQLite accepts would pass the offline suite
    and fail its first deployed call.
    """
    ledger = agent.build_run_ledger(pg)
    limits = Limits(daily_budget_usd=1.0, max_concurrent_runs=5)
    meter = UsageMeter(search_cost_usd=0.0)
    meter.cost_usd = 1.2

    run_id = ledger.begin(user_id="alice", thread_id="t", kind="draft", limits=limits)
    ledger.finish(run_id, "failed", meter, error_type="PoolTimeout")
    with pytest.raises(LimitReached, match=r"\$1\.00"):
        ledger.begin(user_id="alice", thread_id="t", kind="draft", limits=limits)
    ledger.begin(user_id="bob", thread_id="t2", kind="discover", limits=limits)

    ledger.record_approvals(
        [
            Approval(tool="write_file", path=PROFILE, decision="approve"),
            Approval(tool=None, path=None, decision="reject"),
        ],
        user_id="alice",
        thread_id="t",
        run_id=run_id,
        frontend="ui",
    )
    with agent._pool_for(pg).connection() as conn:
        decisions = [
            (row["tool"], row["decision"])
            for row in conn.execute(
                "SELECT tool, decision FROM grant_writer_approvals ORDER BY decision"
            )
        ]
    assert decisions == [("write_file", "approve"), (None, "reject")]

    assert sorted(ledger.summary(since=0), key=str) == sorted(
        [("failed", "PoolTimeout", 1), (None, None, 1)], key=str
    )
    assert _cli(monkeypatch, "ops-check") == 1
    assert "PoolTimeout: 1 run(s)" in json.loads(capsys.readouterr().out)["alerts"]


# --- The load test itself ------------------------------------------------------


def test_the_load_test_runs_clean_at_a_small_size(pg):
    """`tests/load_postgres.py` at two sessions, so the script cannot rot.

    Not a capacity check -- that is the script's own run, by hand, at the
    sizes E5 needs. This only proves it still drives real turns: a turn that
    wrote nothing would report fine latencies over no work at all.
    """
    result = run_load(pg.database_url or "", sessions=2, turns=2, pool_size=4)
    assert result.errors == 0, result.error_types
    assert result.checkpoint_writes > 0
    assert result.checkpoints_scanned > 0
    with agent._pool_for(pg).connection() as conn:
        (row,) = conn.execute(
            "SELECT COUNT(*) AS n, MIN(outcome) AS lo, MAX(outcome) AS hi, "
            "SUM(cost_usd) AS cost FROM grant_writer_runs"
        ).fetchall()
    assert (row["n"], row["lo"], row["hi"]) == (4, "completed", "completed")
    assert row["cost"] > 0
