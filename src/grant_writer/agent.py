"""Agent assembly."""

from __future__ import annotations

import atexit
import functools
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Any

from deepagents import RubricMiddleware, create_deep_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
    TodoListMiddleware,
)
from langchain_core.messages import AIMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from grant_writer.backends import (
    build_backend,
    build_permissions,
    discovery_permissions,
    seed_store_from_disk,
)
from grant_writer.config import (
    DISCOVERY_EFFORT,
    DISCOVERY_MODEL,
    DRAFTING_EFFORT,
    DRAFTING_MODEL,
    GRADER_EFFORT,
    GRADER_MODEL,
    MEMORY_FILE,
    SKILLS_DIR,
    Settings,
    build_model,
)
from grant_writer.prompts import DISCOVERY_PROMPT, ORCHESTRATOR_PROMPT
from grant_writer.refusals import RefusalMiddleware
from grant_writer.runs import PostgresRunLedger, RunLedger, SqliteRunLedger
from grant_writer.subagents import build_discovery_subagents, build_subagents
from grant_writer.tools import (
    build_fetch_opportunity_tool,
    build_measure_tool,
    build_search_tool,
    search_grants_gov,
)


class PlainTodoListMiddleware(TodoListMiddleware):
    """`write_todos`, without langchain's prose added to the system prompt.

    Spelled `TodoListMiddleware(system_prompt="")` from the deepagents 0.7
    upgrade until October 2026, and that was never empty: the middleware
    appends `f"\\n\\n{system_prompt}"` as a text block of its own, so a blank
    prompt became a block holding nothing but whitespace. The API now refuses
    one outright -- `system: text content blocks must contain non-whitespace
    text` -- on the first model call of every draft and every scan, and no test
    saw it, because no test sends a real request.

    Overriding the two wraps keeps what the blank prompt was for (the tool's
    own schema description carries the usage guidance) and keeps
    `after_model`, which rejects parallel `write_todos` calls in one turn.
    """

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | AIMessage:
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | AIMessage:
        return await handler(request)


@functools.cache
def _connection_pool(
    url: str, max_size: int, timeout: float
) -> ConnectionPool[Connection[DictRow]]:
    """One pool per database per process, shared by every graph built in it.

    Cached because the frontends build more than one graph -- the UI caches
    one per `(profile, approve, search)` and per workflow -- and a pool apiece
    would multiply the connections a deployment holds by however many sidebar
    combinations its users happened to try. Keyed on the three values rather
    than on `Settings`, so graphs differing only in `approve_final` share it.

    The connection kwargs are the ones `PostgresSaver.from_conn_string` uses,
    and both savers need them: `autocommit` for `setup()`'s migrations,
    `dict_row` because they read rows by column name, `prepare_threshold=0`
    so a pooler in transaction mode does not trip over prepared statements.
    `check` re-tests a connection on checkout, so a database restart costs a
    reconnect rather than an error on the next run.

    Both savers borrow a connection per query and return it, under their own
    lock, so a long drafting run does not hold one for its duration: the size
    bounds concurrent *queries*, and `timeout` is what turns an exhausted pool
    or an unreachable server into `PoolTimeout` instead of a hung run.
    """
    # Specialised for the type checker only: it says rows are dicts, which is
    # what `row_factory` makes true at run time and what both savers declare.
    pool_class = ConnectionPool[Connection[DictRow]]
    pool = pool_class(
        url,
        connection_class=Connection[DictRow],
        min_size=1,
        max_size=max_size,
        timeout=timeout,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        check=pool_class.check_connection,
        name="grant-writer",
    )
    # The cache keeps the pool alive to interpreter teardown, where its worker
    # threads can no longer be joined: every CLI exit then stalled ~20s (four
    # threads x psycopg_pool's 5s join timeout) printing "couldn't stop
    # thread". Closing it at exit, while threads still run, ends cleanly.
    atexit.register(pool.close)
    return pool


def _pool_for(settings: Settings) -> ConnectionPool[Connection[DictRow]]:
    assert settings.database_url is not None
    return _connection_pool(
        settings.database_url, settings.db_pool_max_size, settings.db_pool_timeout
    )


def _build_checkpointer(settings: Settings) -> BaseCheckpointSaver:
    """A checkpointer is mandatory for interrupts and is what makes `thread_id`
    resume work rather than start over.

    With ``database_url`` set, use Postgres: the deployment case, where threads
    must outlive the process and be shared by every replica. Its tables come
    from ``grant-writer db-setup``, not from here -- `setup()` runs migrations,
    which is a deploy step and not something every graph build should race.
    With ``checkpoint_db`` set, use SQLite so conversation and todos survive
    across separate processes (what the CLI needs for `draft` then `chat`).
    Otherwise use an in-memory saver -- correct for tests and single-process use.
    """
    if settings.database_url is not None:
        from langgraph.checkpoint.postgres import PostgresSaver

        return PostgresSaver(_pool_for(settings))
    if settings.checkpoint_db is None:
        return InMemorySaver()
    settings.checkpoint_db.parent.mkdir(parents=True, exist_ok=True)
    from langgraph.checkpoint.sqlite import SqliteSaver

    # check_same_thread=False: LangGraph may touch the connection from worker
    # threads. The process owns the file for its lifetime.
    conn = sqlite3.connect(str(settings.checkpoint_db), check_same_thread=False)
    return SqliteSaver(conn)


def _build_store(settings: Settings) -> BaseStore:
    """The Store behind `/skills/` and `/memories/` in the server profile.

    One function for both graphs, because two copies is the shape invariant 2's
    history warns against: one learning about Postgres and the other not would
    give a scan an empty org profile and nothing raised. Postgres when
    ``database_url`` is set, on the checkpointer's pool; memory otherwise.

    Seeding is here too, and is safe to repeat against a Store that outlives
    the process: `seed_store_from_disk` overwrites skills and only fills a
    memory key that is missing (decision 10). The local profile reads both
    straight off disk and never consults the Store, so it is not seeded.
    """
    if settings.database_url is not None:
        from langgraph.store.postgres import PostgresStore

        store: BaseStore = PostgresStore(_pool_for(settings))
    else:
        store = InMemoryStore()
    # In the server profile, /skills/ and /memories/ are served from the Store,
    # so it must be filled from disk or the drafter gets no guides and no
    # org profile.
    if settings.backend_profile == "server":
        seed_store_from_disk(store, settings)
    return store


def build_run_ledger(settings: Settings) -> RunLedger:
    """The run table the limits read (invariant 26), beside the checkpoints.

    Postgres on the checkpointer's pool when ``database_url`` is set, so every
    replica counts the same runs; otherwise a SQLite file of its own, which
    still holds across the processes of one machine. Never in memory: a limit
    that resets with the process is not a limit.
    """
    if settings.database_url is not None:
        return PostgresRunLedger(_pool_for(settings))
    return SqliteRunLedger(settings.run_table_db)


def setup_database(settings: Settings) -> None:
    """Create or migrate the checkpoint, Store and run tables. Idempotent.

    What ``grant-writer db-setup`` runs, once per deploy, before any graph is
    built against the database. Both `setup()`s apply only migrations not yet
    recorded, and the run table is `CREATE ... IF NOT EXISTS`, so running it
    on every deploy is safe and is the intended use.
    """
    if settings.database_url is None:
        raise ValueError("DATABASE_URL is not set; there is no database to set up.")
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.store.postgres import PostgresStore

    pool = _pool_for(settings)
    PostgresSaver(pool).setup()
    PostgresStore(pool).setup()
    PostgresRunLedger(pool).setup()


def build_agent(settings: Settings | None = None):
    """Build the grant writer agent.

    The returned graph is ordinary LangGraph, so it can be streamed,
    checkpointed, or embedded in a larger graph.
    """
    settings = settings or Settings()

    # One backend for the graph and for `measure_text`, which reads a section
    # through it -- in the server profile a draft exists only in graph state.
    backend = build_backend(settings)
    tools = [build_measure_tool(backend)]
    if (search := build_search_tool(enabled=settings.enable_search)) is not None:
        tools.append(search)

    checkpointer = _build_checkpointer(settings)
    store = _build_store(settings)

    return create_deep_agent(
        model=build_model(DRAFTING_MODEL, effort=DRAFTING_EFFORT),
        tools=tools,
        system_prompt=ORCHESTRATOR_PROMPT,
        subagents=build_subagents(settings, backend),
        skills=[SKILLS_DIR],
        memory=[MEMORY_FILE],
        backend=backend,
        permissions=build_permissions(settings),
        middleware=[
            # Passed explicitly since deepagents 0.7, which stopped installing
            # it: `write_todos` is what step 3 of ORCHESTRATOR_PROMPT tells the
            # model to plan with, and what the UI's Plan panel renders. Losing
            # it raises nothing -- the orchestrator simply stops planning and
            # the panel stays blank. The plain variant drops langchain's
            # generic prose only; the tool's own schema description, which is
            # where the usage guidance actually lives, is untouched.
            PlainTodoListMiddleware(),
            # No-op unless a `rubric` is passed on invocation state, so it is
            # safe to always include. When the funder's review criteria are
            # supplied, this makes the agent grade its own work against them
            # and iterate.
            RubricMiddleware(
                model=build_model(GRADER_MODEL, effort=GRADER_EFFORT),
                max_iterations=settings.max_rubric_iterations,
            ),
            # Says so when the API declines a reply, which it does with HTTP
            # 200 and empty content (invariant 21). Not inherited by
            # subagents, so `_subagent` installs its own on each of them.
            RefusalMiddleware(agent="grant-writer"),
        ],
        checkpointer=checkpointer,
        store=store,
        name="grant-writer",
    )


def build_discovery_agent(settings: Settings | None = None):
    """Build the opportunity-discovery agent.

    A second graph rather than a mode of `build_agent`, and the deciding
    reason is a permission rather than a preference: `discovery_permissions`
    returns no interrupt rule at any setting, so a discovery thread *cannot*
    park on an approval. That is a structural property worth having -- it means
    the frontends need no approval panel, no `is_parked` check, and none of
    invariant 11's machinery for this graph -- and it only holds if discovery
    is its own graph with its own rules. Folding it into `build_agent` would
    produce one graph that sometimes can be parked and sometimes cannot, which
    is a far harder thing to keep true than two graphs that differ honestly.

    Note it is `discovery_permissions(settings)` and *not* `build_permissions(settings)`
    here. Sharing the latter looked right and was not: it allows
    `/applications/**` and, under `--approve`, interrupts on
    `/applications/*/final/**` -- and this orchestrator has `write_file` like
    any other, with `WORKSPACE_CONVENTIONS` describing that directory to it.
    The roster having no drafter constrains the subagents, not the orchestrator.

    The second reason is scope: dispatching between drafting and discovery
    inside `ORCHESTRATOR_PROMPT` would put the choice in prose, where a
    misread costs a wrong workflow, instead of in the roster, where it cannot
    be misread at all -- this graph has no `section-drafter` to reach.

    Everything below the orchestrator is shared as-is: the same `Settings`,
    the same backend, the same permission builder, the same checkpoint file.
    """
    settings = settings or Settings()

    tools = [search_grants_gov, build_fetch_opportunity_tool(settings.root)]
    # Web search covers what grants.gov does not index -- private foundations,
    # state agencies, non-US funders -- so `--no-search` narrows a scan to US
    # federal rather than disabling it.
    if (search := build_search_tool(enabled=settings.enable_search)) is not None:
        tools.append(search)

    checkpointer = _build_checkpointer(settings)
    store = _build_store(settings)

    # Annotated so ty does not infer a union from the elements. A lone
    # `[PlainTodoListMiddleware()]` took its type from the parameter; adding a
    # second class made it `list[A | B]`, which surfaced the upstream arity
    # mismatch CLAUDE.md describes here as well, doubling the ty baseline with
    # no new cause. `build_agent`'s list is left unannotated on purpose: it is
    # where that one upstream diagnostic is kept visible until langchain fixes it.
    discovery_middleware: list[AgentMiddleware[Any, Any, Any]] = [
        PlainTodoListMiddleware(),
        RefusalMiddleware(agent="opportunity-discovery"),
    ]
    return create_deep_agent(
        model=build_model(DISCOVERY_MODEL, effort=DISCOVERY_EFFORT),
        tools=tools,
        system_prompt=DISCOVERY_PROMPT,
        subagents=build_discovery_subagents(),
        # No `skills`: the six guides under /skills/ are for drafting sections
        # of a proposal, and nothing here drafts one. No RubricMiddleware
        # either -- it grades a draft against a funder's review criteria, which
        # is a question that only exists once there is a draft.
        memory=[MEMORY_FILE],
        # `write_todos` is the one piece of the harness this graph does want.
        # A scan is search, then triage, then one delegated scoring per
        # candidate, and the shortlist is only as complete as that list of
        # candidates -- a scan that quietly stops after four of eleven looks
        # exactly like a scan that found four. See build_agent for why it is
        # passed here at all and why the prompt is left out.
        middleware=discovery_middleware,
        backend=build_backend(settings),
        permissions=discovery_permissions(settings),
        checkpointer=checkpointer,
        store=store,
        name="opportunity-discovery",
    )
