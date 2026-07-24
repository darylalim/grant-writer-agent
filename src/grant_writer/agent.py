"""Agent assembly."""

from __future__ import annotations

from deepagents import RubricMiddleware, create_deep_agent
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from grant_writer.backends import build_backend, build_permissions
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


def build_agent(settings: Settings | None = None):
    """Build the grant writer agent.

    The returned graph is ordinary LangGraph, so it can be streamed,
    checkpointed, or embedded in a larger graph.
    """
    settings = settings or Settings()

    tools = [extract_pdf_text, measure_text]
    if (search := build_search_tool()) is not None:
        tools.append(search)

    # A checkpointer is mandatory for interrupts, and is what makes `thread_id`
    # resume work rather than start over. The Store only does real work in the
    # `server` profile, where /memories/ is routed to it.
    checkpointer = InMemorySaver()
    store = InMemoryStore()

    return create_deep_agent(
        model=DRAFTING_MODEL,
        tools=tools,
        system_prompt=ORCHESTRATOR_PROMPT,
        subagents=build_subagents(),
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
