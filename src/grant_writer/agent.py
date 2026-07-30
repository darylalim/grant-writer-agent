"""Agent assembly."""

from __future__ import annotations

import sqlite3

from deepagents import RubricMiddleware, create_deep_agent
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from grant_writer.backends import (
    build_backend,
    build_permissions,
    discovery_permissions,
    seed_store_from_disk,
)
from grant_writer.config import (
    DISCOVERY_MODEL,
    DRAFTING_MODEL,
    GRADER_MODEL,
    MEMORY_FILE,
    SKILLS_DIR,
    Settings,
    build_model,
)
from grant_writer.prompts import DISCOVERY_PROMPT, ORCHESTRATOR_PROMPT
from grant_writer.subagents import build_discovery_subagents, build_subagents
from grant_writer.tools import (
    build_search_tool,
    extract_pdf_text,
    fetch_grants_gov_opportunity,
    measure_text,
    search_grants_gov,
)


def _build_checkpointer(settings: Settings) -> BaseCheckpointSaver:
    """A checkpointer is mandatory for interrupts and is what makes `thread_id`
    resume work rather than start over.

    With ``checkpoint_db`` set, use SQLite so conversation and todos survive
    across separate processes (what the CLI needs for `draft` then `chat`).
    Otherwise use an in-memory saver -- correct for tests and single-process use.
    """
    if settings.checkpoint_db is None:
        return InMemorySaver()
    settings.checkpoint_db.parent.mkdir(parents=True, exist_ok=True)
    from langgraph.checkpoint.sqlite import SqliteSaver

    # check_same_thread=False: LangGraph may touch the connection from worker
    # threads. The process owns the file for its lifetime.
    conn = sqlite3.connect(str(settings.checkpoint_db), check_same_thread=False)
    return SqliteSaver(conn)


def build_agent(settings: Settings | None = None):
    """Build the grant writer agent.

    The returned graph is ordinary LangGraph, so it can be streamed,
    checkpointed, or embedded in a larger graph.
    """
    settings = settings or Settings()

    tools = [extract_pdf_text, measure_text]
    if (search := build_search_tool(enabled=settings.enable_search)) is not None:
        tools.append(search)

    checkpointer = _build_checkpointer(settings)
    store = InMemoryStore()
    # In the server profile, /skills/ and /memories/ are served from the Store,
    # so it must be filled from disk or the drafter gets no guides and no
    # org profile. The local profile reads these straight off disk instead.
    if settings.backend_profile == "server":
        seed_store_from_disk(store, settings)

    return create_deep_agent(
        model=build_model(DRAFTING_MODEL),
        tools=tools,
        system_prompt=ORCHESTRATOR_PROMPT,
        subagents=build_subagents(settings),
        skills=[SKILLS_DIR],
        memory=[MEMORY_FILE],
        backend=build_backend(settings),
        permissions=build_permissions(settings),
        # No-op unless a `rubric` is passed on invocation state, so it is safe
        # to always include. When the funder's review criteria are supplied,
        # this makes the agent grade its own work against them and iterate.
        middleware=[
            RubricMiddleware(
                model=build_model(GRADER_MODEL),
                max_iterations=settings.max_rubric_iterations,
            )
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

    Note it is `discovery_permissions()` and *not* `build_permissions(settings)`
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

    tools = [search_grants_gov, fetch_grants_gov_opportunity]
    # Web search covers what grants.gov does not index -- private foundations,
    # state agencies, non-US funders -- so `--no-search` narrows a scan to US
    # federal rather than disabling it.
    if (search := build_search_tool(enabled=settings.enable_search)) is not None:
        tools.append(search)

    checkpointer = _build_checkpointer(settings)
    store = InMemoryStore()
    if settings.backend_profile == "server":
        seed_store_from_disk(store, settings)

    return create_deep_agent(
        model=build_model(DISCOVERY_MODEL),
        tools=tools,
        system_prompt=DISCOVERY_PROMPT,
        subagents=build_discovery_subagents(),
        # No `skills`: the six guides under /skills/ are for drafting sections
        # of a proposal, and nothing here drafts one. No RubricMiddleware
        # either -- it grades a draft against a funder's review criteria, which
        # is a question that only exists once there is a draft.
        memory=[MEMORY_FILE],
        backend=build_backend(settings),
        permissions=discovery_permissions(),
        checkpointer=checkpointer,
        store=store,
        name="opportunity-discovery",
    )
