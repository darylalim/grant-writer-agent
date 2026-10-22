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

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

# Event kinds. The orchestrator's own prose (MESSAGE) is worth surfacing
# distinctly from its tool calls -- it is where it reports what it could not do.
DELEGATE = "delegate"
PLAN = "plan"
TOOL = "tool"
MESSAGE = "message"
# A reply the API declined. Its own kind, never MESSAGE: a refusal arrives as
# HTTP 200 with empty content, so drawn as prose it is an empty turn.
DECLINED = "declined"

# Tool-call argument names that identify *what* a call is acting on, in the
# order we prefer them. Different tools spell it differently and none of them
# is guaranteed present.
_SUBJECT_KEYS = ("file_path", "path", "query")

# The sentence `refusals.RefusalMiddleware` writes into a declined reply, and
# the parser that reads it back. Both live here, in a leaf, so the writer and
# the reader are one format rather than two that agree today. Text rather
# than metadata because text is the only thing `deepagents` carries from a
# subagent up to its `task` result: it forwards the subagent's last non-empty
# reply and drops `response_metadata`, so a declined drafter otherwise reaches
# the orchestrator as an empty result -- or as stale prose from an earlier turn.
_DECLINED_LEAD = "Declined by the safety classifier"
_DECLINED_RE = re.compile(
    rf"^{_DECLINED_LEAD} \(category: (?P<category>[a-z_]+)\) "
    r"in (?P<agent>[\w-]+)\.(?: Note: (?P<explanation>.*))?\Z",
    re.DOTALL | re.MULTILINE,
)
#: Shown in place of a category the API omitted or spelled unexpectedly. The
#: category is API output and reaches a markdown renderer, so anything outside
#: `[a-z_]+` is replaced rather than passed through.
UNSPECIFIED = "unspecified"


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
    #: DECLINED only: the classifier's category. ``label`` is then the agent
    #: that was declined and ``detail`` the API's explanation, which is model
    #: output and must be drawn as plain text, never as markdown.
    category: str = ""


def declined_text(agent: str, category: Any, explanation: Any) -> str:
    """The sentence a declined reply carries, for the model and the parser.

    Written for both readers at once: the orchestrator reads it as a `task`
    result and learns why a section is missing, and `iter_activity` reads it
    back as a DECLINED event. A category outside `[a-z_]+` becomes
    ``unspecified``, so the parser's character class can never fail to match
    what this wrote.
    """
    text = f"{_DECLINED_LEAD} (category: {_category(category)}) in {agent}."
    note = str(explanation or "").strip()
    return f"{text} Note: {note}" if note else text


def _category(value: Any) -> str:
    value = str(value or "")
    return value if re.fullmatch(r"[a-z_]+", value) else UNSPECIFIED


def _declined_event(text: str) -> Event | None:
    match = _DECLINED_RE.search(text)
    if match is None:
        return None
    return Event(
        DECLINED,
        label=match["agent"],
        detail=(match["explanation"] or "").strip(),
        category=match["category"],
    )


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

    metadata = getattr(message, "response_metadata", None) or {}
    if node == "model" and metadata.get("stop_reason") == "refusal":
        # The orchestrator's own reply. Read from the sentence the middleware
        # wrote, which names the agent; failing that, from the metadata, so a
        # graph built without the middleware still says it was declined.
        details = metadata.get("stop_details") or {}
        yield _declined_event(_text_of(message)) or Event(
            DECLINED,
            detail=str(details.get("explanation") or "").strip(),
            category=_category(details.get("category")),
        )
        return
    if getattr(message, "type", None) == "tool" and message.name == "task":
        # A subagent's result. Keyed on `task` so a `read_file` of a document
        # that happens to quote the sentence is not mistaken for a refusal.
        if (event := _declined_event(_text_of(message))) is not None:
            yield event
        return

    text = _text_of(message)
    # Only the orchestrator node's untooled messages are prose worth showing;
    # a message that carries tool calls has already been rendered as those.
    if text and node == "model" and not tool_calls:
        yield Event(MESSAGE, detail=text)


def _text_of(message: Any) -> str:
    text = getattr(message, "text", None)
    # `str` first, `callable` second, and the order is the whole point. On
    # langchain-core 1.x `.text` is a property returning a `TextAccessor` --
    # a `str` subclass that is *also* callable, kept that way so older code
    # calling `.text()` still works. Testing `callable` first therefore took
    # the back-compat path on every message and printed a deprecation warning
    # per event, which on a long run buries the activity trace it is
    # interleaved with. Testing `str` first takes the property, and the
    # callable branch stays for a message type that still exposes a plain
    # method -- there the value is a bound method, and rendering that into the
    # feed would put `<bound method ...>` on screen where the prose goes.
    if not isinstance(text, str) and callable(text):
        text = text()
    return text if isinstance(text, str) else ""


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
