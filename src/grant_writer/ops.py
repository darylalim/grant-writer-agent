"""Operations: server-side logs, the tracing mode, and the parked-thread check.

What a team deployment needs that one laptop does not (E7). On a laptop the
person running the app is also its operator and reads its errors on screen; in
a deployment they are not, and LangSmith may be hidden or off (decision 16),
so these are the records that remain.

**No content, by construction rather than by care** (invariant 27). A log line
carries only the fields `LOG_FIELDS` names -- ids, a phase, an outcome, an
exception's class and message -- and `log_event` refuses any other, so a field
that would carry a message, a file's text or the profile cannot be added
without editing that set, which is where the argument has to be made. The same
rule as the run table's columns, applied to the other record.

Like `activity.py` this imports nothing from the package, so both frontends and
the CLI's operator commands can share it.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

LOGGER_NAME = "grant_writer"

# Every field a log line may carry. A new one is a claim that it holds no
# proposal content; `test_ops.py` pins the set so the claim is made in review.
LOG_FIELDS = frozenset(
    {
        "user_id",
        "thread_id",
        "run_id",
        "frontend",
        "phase",
        "kind",
        "outcome",
        "tool",
        "path",
        "decision",
        "count",
        "error_type",
        "error",
    }
)

LogValue = str | int | float | bool | None

# An exception's message is logged, capped: the handlers that reach
# `log_event` catch infrastructure failures (a locked database, a 429, a pool
# timeout), whose messages name the cause and are the point of logging them.
# The cap bounds what one could carry if that ever stopped being true.
MAX_ERROR_CHARS = 300


class _JsonFormatter(logging.Formatter):
    """One JSON object per line, which every host's log collector parses."""

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created).astimezone().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        line.update(getattr(record, "fields", {}))
        return json.dumps(line, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Send this package's log records to stderr as JSON lines. Idempotent.

    Scoped to the `grant_writer` logger, never the root: a root handler would
    also collect what third-party libraries log, which this module cannot vet
    for content. Propagation is left on, so a host that configures the root
    logger itself still receives these, and so does pytest's `caplog`.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level.upper())
    if not any(isinstance(h, _JsonHandler) for h in logger.handlers):
        logger.addHandler(_JsonHandler())


class _JsonHandler(logging.StreamHandler):
    """Stderr, JSON lines; a class of its own so `configure_logging` can tell
    whether it already installed one."""

    def __init__(self) -> None:
        super().__init__(sys.stderr)
        self.setFormatter(_JsonFormatter())


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    exc: BaseException | None = None,
    context: Mapping[str, LogValue] | None = None,
    **fields: LogValue,
) -> None:
    """Log one event with content-free fields only.

    `exc` contributes its class name and capped message, never its traceback's
    local values; standard logging prints no locals, so neither does this.
    `context` is fields shared by every line of one turn, passed once.
    Raises on a field outside `LOG_FIELDS` -- loudly, because a log line is
    read long after it is written, and a field nobody vetted is how a draft
    ends up in a log collector.
    """
    fields = {**(context or {}), **fields}
    unknown = set(fields) - LOG_FIELDS
    if unknown:
        msg = f"not a content-free log field: {', '.join(sorted(unknown))}"
        raise ValueError(msg)
    if exc is not None:
        fields["error_type"] = type(exc).__name__
        fields["error"] = str(exc)[:MAX_ERROR_CHARS]
    logger.log(
        level,
        event,
        extra={"fields": {k: v for k, v in fields.items() if v is not None}},
    )


# --- Tracing -------------------------------------------------------------------

TracingMode = Literal["full", "hidden", "off"]
TRACING_MODES: tuple[TracingMode, ...] = ("full", "hidden", "off")


def configure_tracing(mode: str) -> None:
    """Apply decision 16's tracing mode to this process's LangSmith client.

    `hidden` keeps timing, token counts, errors and the call tree and sends no
    inputs, outputs or metadata -- metadata too, because ours carries the
    rubric text, the funder and the user-prefixed thread id (`trace_config`).
    It works by installing a client built with all three `hide_*` flags as
    LangSmith's global one, which is the client `LangChainTracer` takes when
    none is passed. Not by setting `LANGSMITH_HIDE_*` in the environment:
    `langsmith.utils.get_env_var` is `lru_cache`d, so a variable set after the
    first read is never seen, with nothing raised.

    `off` disables tracing globally, which outranks every one of the four
    environment spellings (invariant 19) rather than adding a fifth.
    `full` changes nothing: the environment decides, as before E7.
    """
    import langsmith
    from langsmith import Client

    if mode not in TRACING_MODES:
        msg = f"tracing mode must be one of {', '.join(TRACING_MODES)}, not {mode!r}"
        raise ValueError(msg)
    if mode == "full":
        return
    if mode == "off":
        langsmith.configure(enabled=False)
        return
    langsmith.configure(
        client=Client(hide_inputs=True, hide_outputs=True, hide_metadata=True)
    )


# --- Parked threads --------------------------------------------------------------

_INTERRUPT_CHANNEL = "__interrupt__"


@dataclass(frozen=True)
class ParkedThread:
    thread_id: str
    since: float
    """Epoch seconds of the checkpoint the thread is parked on."""


def parked_threads(
    checkpoints: Iterable[Any], *, older_than_s: float, now: float | None = None
) -> list[ParkedThread]:
    """Threads whose latest checkpoint is waiting on an interrupt, oldest first.

    Fed `checkpointer.list(None)`: every checkpoint of every thread. Only the
    latest root checkpoint of each thread counts -- a subagent's namespaced
    one is not where the parent parks, and an earlier checkpoint that was
    parked and then resumed is not parked now. "Waiting" is read the way
    `activity.is_parked` reads it, from the interrupt itself (a pending write
    on the interrupt channel), never from whether its requests can be parsed.

    The approval design keeps a paused write safe indefinitely (invariant 11);
    in a team, that is also how an unanswered approval sits unnoticed, which
    is what this finds.
    """
    now = time.time() if now is None else now
    latest: dict[str, Any] = {}
    for item in checkpoints:
        configurable = item.config.get("configurable", {})
        if configurable.get("checkpoint_ns"):
            continue
        thread_id = configurable.get("thread_id")
        seen = latest.get(thread_id)
        if seen is None or item.checkpoint["id"] > seen.checkpoint["id"]:
            latest[thread_id] = item
    parked = []
    for thread_id, item in latest.items():
        writes = item.pending_writes or ()
        if not any(channel == _INTERRUPT_CHANNEL for _task, channel, _v in writes):
            continue
        since = datetime.fromisoformat(item.checkpoint["ts"]).timestamp()
        if now - since >= older_than_s:
            parked.append(ParkedThread(thread_id=thread_id, since=since))
    return sorted(parked, key=lambda p: p.since)
