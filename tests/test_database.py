"""Postgres wiring (E1), checked offline.

Nothing here opens a connection. The pool's `open()` is a no-op, and
neither `PostgresSaver` nor `PostgresStore` touches the network on
construction, so what is pinned is the shape: which saver a `Settings` gets,
that both graphs share one pool, and what that pool connects with. Whether the
SQL works against a real server is upstream's to test; this suite is offline
by contract.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.store.memory import InMemoryStore
from langgraph.store.postgres import PostgresStore
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from grant_writer import agent, cli, config
from grant_writer.config import Settings
from grant_writer.runs import PostgresRunLedger

URL = "postgresql://nobody@127.0.0.1:1/never"


@pytest.fixture
def unopened_pool(monkeypatch):
    """Build pools exactly as `agent` does, except never open one."""
    agent._connection_pool.cache_clear()

    # Patched on the class rather than by swapping `agent.ConnectionPool`,
    # which `agent` subscripts for the type checker and a stand-in would have
    # to be generic to survive. The constructor's own `open()` becomes a no-op.
    monkeypatch.setattr(ConnectionPool, "open", lambda self, *a, **k: None)
    yield
    agent._connection_pool.cache_clear()


def test_settings_built_directly_never_reach_a_database(monkeypatch):
    """Only `persistent_settings` reads DATABASE_URL, so a test or a library
    caller cannot be pointed at Postgres by the shell it runs in."""
    monkeypatch.setattr(config, "DATABASE_URL", URL)
    assert Settings().database_url is None
    assert config.persistent_settings().database_url == URL


def test_conftest_leaves_the_suite_without_a_database():
    assert config.DATABASE_URL is None
    assert config.persistent_settings().database_url is None


def test_the_url_wins_over_the_sqlite_file(tmp_path, unopened_pool) -> None:
    sqlite = replace(Settings(), checkpoint_db=tmp_path / "c.sqlite")
    both = replace(sqlite, database_url=URL)
    assert isinstance(agent._build_checkpointer(both), PostgresSaver)
    assert not (tmp_path / "c.sqlite").exists()
    assert isinstance(agent._build_checkpointer(sqlite), SqliteSaver)
    assert isinstance(agent._build_checkpointer(Settings()), InMemorySaver)


def test_both_graphs_share_one_pool_for_checkpoints_and_store(unopened_pool) -> None:
    settings = replace(Settings(), database_url=URL)
    drafting = agent.build_agent(settings)
    # A graph that differs only in approve_final must not open a second pool.
    discovery = agent.build_discovery_agent(replace(settings, approve_final=True))
    for graph in (drafting, discovery):
        assert isinstance(graph.checkpointer, PostgresSaver)
        assert isinstance(graph.store, PostgresStore)
    pools = {id(g.checkpointer.conn) for g in (drafting, discovery)} | {
        id(g.store.conn) for g in (drafting, discovery)
    }
    assert len(pools) == 1


def test_without_a_url_the_store_stays_in_memory():
    for graph in (agent.build_agent(), agent.build_discovery_agent()):
        assert isinstance(graph.store, InMemoryStore)


def test_the_pool_connects_the_way_both_savers_require(unopened_pool) -> None:
    """`PostgresSaver.from_conn_string`'s kwargs, which a pool must restate.

    Without `autocommit`, `setup()`'s migrations run inside a transaction
    that `CREATE INDEX CONCURRENTLY` refuses; without `dict_row`, every read
    indexes a tuple by column name. Neither fails until the first real query.
    """
    settings = replace(
        Settings(), database_url=URL, db_pool_max_size=7, db_pool_timeout=4.5
    )
    pool = agent._pool_for(settings)
    assert pool.kwargs == {
        "autocommit": True,
        "prepare_threshold": 0,
        "row_factory": dict_row,
    }
    assert pool.max_size == 7
    assert pool.timeout == 4.5
    assert pool._check is ConnectionPool.check_connection


def test_the_pool_is_closed_at_exit(monkeypatch, unopened_pool) -> None:
    """Left to interpreter teardown, its threads cannot be joined: found live,
    as a ~20s stall and four "couldn't stop thread" lines on every CLI exit."""
    registered = []
    monkeypatch.setattr(agent.atexit, "register", registered.append)
    pool = agent._pool_for(replace(Settings(), database_url=URL))
    assert registered == [pool.close]


def test_a_restart_against_a_persistent_store_keeps_profile_edits(
    monkeypatch, unopened_pool
):
    """Decision 10 through `_build_store`, the path a deployed restart takes.

    `PostgresStore` is swapped for one in-memory instance that outlives each
    build, which is the property Postgres adds: every graph build seeds the
    same rows again. A skill is refreshed from disk; the profile edit stays.
    """
    persistent = InMemoryStore()
    monkeypatch.setattr(
        "langgraph.store.postgres.PostgresStore", lambda conn: persistent
    )
    settings = replace(Settings(), database_url=URL, backend_profile="server")

    agent._build_store(settings)
    profile = persistent.get(("filesystem", "memories"), "/org/AGENTS.md")
    skills = persistent.search(("filesystem", "skills"))
    assert profile is not None and skills

    edited = {**profile.value, "content": "edited in the Store"}
    persistent.put(("filesystem", "memories"), "/org/AGENTS.md", edited)
    stale = {**skills[0].value, "content": "stale"}
    persistent.put(("filesystem", "skills"), skills[0].key, stale)

    agent._build_store(settings)
    profile = persistent.get(("filesystem", "memories"), "/org/AGENTS.md")
    assert profile is not None
    assert profile.value["content"] == "edited in the Store"
    skill = persistent.get(("filesystem", "skills"), skills[0].key)
    assert skill is not None
    assert skill.value["content"] != "stale"


def _run_cli(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr("sys.argv", ["grant-writer", *argv])
    return cli.main()


def test_db_setup_refuses_without_a_url(monkeypatch, capsys):
    assert _run_cli(monkeypatch, "db-setup") == 1
    assert "DATABASE_URL is not set" in capsys.readouterr().err


def test_db_setup_migrates_every_table_on_one_pool(monkeypatch, unopened_pool) -> None:
    """The savers and the run table (invariant 26), all on the one pool."""
    monkeypatch.setattr(config, "DATABASE_URL", URL)
    seen = []
    monkeypatch.setattr(PostgresSaver, "setup", lambda self: seen.append(self.conn))
    monkeypatch.setattr(PostgresStore, "setup", lambda self: seen.append(self.conn))
    monkeypatch.setattr(PostgresRunLedger, "setup", lambda self: seen.append(self.pool))
    assert _run_cli(monkeypatch, "db-setup") == 0
    assert len(seen) == 3 and seen[0] is seen[1] is seen[2]
