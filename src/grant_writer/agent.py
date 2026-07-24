"""Agent assembly."""

from __future__ import annotations

import sqlite3

from deepagents import RubricMiddleware, create_deep_agent
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from grant_writer.backends import build_backend, build_permissions, seed_store_from_disk
from grant_writer.config import (
    DRAFTING_MODEL,
    GRADER_MODEL,
    MEMORY_FILE,
    SKILLS_DIR,
    Settings,
)
from grant_writer.prompts import ORCHESTRATOR_PROMPT
from grant_writer.subagents import build_subagents
from grant_writer.tools import build_search_tool, extract_pdf_text, measure_text


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
        model=DRAFTING_MODEL,
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
                model=GRADER_MODEL,
                max_iterations=settings.max_rubric_iterations,
            )
        ],
        checkpointer=checkpointer,
        store=store,
        name="grant-writer",
    )
