"""Reading the agent's stream, for any frontend.

``agent.stream(..., stream_mode="updates")`` yields raw LangGraph node updates.
Every frontend needs the same reading of them -- which subagent was delegated
to, what the plan is, which file was written -- and only differs in how it draws
the result. So the parsing lives here once and the renderers stay dumb.

Keeping one parser matters because the shapes below are positional and
undocumented: the delegation target is ``args["subagent_type"]`` on a tool call
named ``task``, and a ``deepagents`` upgrade that renames either should break
one function rather than silently drawing an empty label in two frontends.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

# Event kinds. The orchestrator's own prose (MESSAGE) is worth surfacing
# distinctly from its tool calls -- it is where it reports what it could not do.
DELEGATE = "delegate"
PLAN = "plan"
TOOL = "tool"
MESSAGE = "message"

# Tool-call argument names that identify *what* a call is acting on, in the
# order we prefer them. Different tools spell it differently and none of them
# is guaranteed present.
_SUBJECT_KEYS = ("file_path", "path", "query")


@dataclass(frozen=True)
class Todo:
    """One entry from a ``write_todos`` call."""

    status: str
    content: str

    @property
    def mark(self) -> str:
        """Single-character status marker, as the CLI has always printed it."""
        return {"completed": "x", "in_progress": ">"}.get(self.status, " ")


@dataclass(frozen=True)
class Event:
    """One thing the agent did, ready to render.

    ``label`` is the subagent or tool name, ``detail`` the file path, query, or
    message text. Frozen and built from plain strings so a frontend can keep a
    list of these in session state across reruns.
    """

    kind: str
    label: str = ""
    detail: str = ""
    todos: tuple[Todo, ...] = field(default=())


def iter_activity(chunk: dict[str, Any]) -> Iterator[Event]:
    """Turn one ``stream_mode="updates"`` chunk into renderable events.

    Interrupt chunks (``__interrupt__``) carry no messages and yield nothing;
    callers detect those separately because they change control flow rather
    than the display.
    """
    for node, update in chunk.items():
        if not isinstance(update, dict):
            continue
        for message in update.get("messages", []) or []:
            yield from _message_events(node, message)


def _message_events(node: str, message: Any) -> Iterator[Event]:
    tool_calls = getattr(message, "tool_calls", None) or []
    for call in tool_calls:
        yield _call_event(call)

    text = getattr(message, "text", None)
    if callable(text):  # langchain messages expose `.text` as a method
        text = text()
    # Only the orchestrator node's untooled messages are prose worth showing;
    # a message that carries tool calls has already been rendered as those.
    if text and node == "model" and not tool_calls:
        yield Event(MESSAGE, detail=text)


def _call_event(call: dict[str, Any]) -> Event:
    name = call.get("name", "?")
    args = call.get("args", {}) or {}

    if name == "task":
        # `agent` is the older spelling; accept both so an upgrade degrades to
        # the right label rather than a blank one.
        target = args.get("subagent_type") or args.get("agent") or ""
        return Event(DELEGATE, label=str(target))

    if name == "write_todos":
        todos = tuple(
            Todo(str(todo.get("status", "")), str(todo.get("content", "")))
            for todo in (args.get("todos") or [])
        )
        return Event(PLAN, todos=todos)

    subject = next((args[key] for key in _SUBJECT_KEYS if args.get(key)), "")
    return Event(TOOL, label=str(name), detail=str(subject))


def pending_action_requests(agent: Any, config: dict[str, Any]) -> list[dict[str, Any]]:
    """All tool calls awaiting approval, flattened across pending interrupts.

    ``HumanInTheLoopMiddleware`` bundles every interrupted tool call from one
    turn into a single interrupt as ``action_requests``, and on resume it
    requires exactly one decision per request. So callers count requests, not
    interrupts -- resuming a two-write turn with one decision fails.
    """
    requests: list[dict[str, Any]] = []
    state = agent.get_state(config)
    for task in state.tasks:
        for interrupt in getattr(task, "interrupts", []) or []:
            value = interrupt.value
            if isinstance(value, dict):
                requests.extend(value.get("action_requests", []) or [])
    return requests


def is_parked(agent: Any, config: dict[str, Any]) -> bool:
    """Whether the thread is stopped on an interrupt at all.

    Deliberately not `bool(pending_action_requests(...))`, and the difference is
    the whole reason this exists. That function returns `[]` just as readily for
    a parked thread whose payload it could not parse -- a renamed `deepagents`
    key, a non-dict interrupt value, both of which it skips without raising --
    as for a thread with nothing pending at all. Deciding *may a new turn start*
    on the count of readable requests therefore fails open in precisely the case
    the blind-approval panel exists for: the graph is parked, the list is empty,
    and a fresh turn streams over a submission-bound write no one ever saw.

    Reading a request is a display concern and may fail; whether the graph is
    parked is a control-flow one and must not. Callers that need both should ask
    this first -- see invariant 5.
    """
    state = agent.get_state(config)
    return any(getattr(task, "interrupts", None) for task in state.tasks)


def approval_decisions(
    requests: list[dict[str, Any]],
    *,
    approve: bool,
    message: str = "",
) -> list[dict[str, Any]]:
    """One resume decision per pending action request.

    The count is what matters and what is easy to get wrong -- resume rejects a
    list whose length does not match the interrupted calls. Lives beside
    `pending_action_requests` so both frontends build the payload the same way;
    a change to the decision schema that fixed only one of them would leave the
    other resuming into a stuck graph with no exception raised.

    The floor of one covers a pending interrupt whose requests could not be
    read, which is how the CLI has always behaved.
    """
    count = max(len(requests), 1)
    if approve:
        return [{"type": "approve"} for _ in range(count)]
    reason = message.strip() or "Rejected."
    return [{"type": "reject", "message": reason} for _ in range(count)]
