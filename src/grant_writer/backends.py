"""Backend and permission wiring.

The two profiles exist because "persistence" means different things in the two
places this agent runs:

``local``
    The filesystem *is* the cross-session memory. A plain ``FilesystemBackend``
    rooted at the repo gives durable drafts you can open in any editor, and
    ``/memories/org/AGENTS.md`` survives restarts for free.

``server``
    There is no durable disk. Working drafts live in graph state (ephemeral,
    thread-scoped) and only ``/memories/`` is routed to a ``Store`` so org
    identity outlives a single conversation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from deepagents import FilesystemPermission
from deepagents.backends import (
    BackendProtocol,
    CompositeBackend,
    FilesystemBackend,
    StateBackend,
    StoreBackend,
)

from grant_writer.config import Settings

BackendFactory = BackendProtocol | Callable[[Any], BackendProtocol]


def build_backend(settings: Settings) -> BackendFactory:
    """Return the backend for the configured profile."""
    if settings.backend_profile == "local":
        settings.ensure_dirs()
        # virtual_mode confines the agent to `root`, rejecting `..` and `~`
        # escapes. It does NOT stop it writing to src/ -- permissions do that.
        return FilesystemBackend(root_dir=settings.root, virtual_mode=True)

    # `server`: drafts are ephemeral, org memory persists via the Store.
    # The route prefix must match exactly -- "/memories/" catches
    # "/memories/org/AGENTS.md", but a bare "/memory/..." path would silently
    # fall through to StateBackend and vanish at the end of the thread.
    return lambda runtime: CompositeBackend(
        default=StateBackend(runtime),
        routes={"/memories/": StoreBackend(runtime)},
    )


def build_permissions(settings: Settings) -> list[FilesystemPermission]:
    """Confine the agent's writes to content directories.

    Rules are evaluated in order and the *first* match wins; anything that
    matches no rule is allowed. So the specific allows must precede the
    catch-all deny.
    """
    rules: list[FilesystemPermission] = []

    if settings.approve_final:
        # Submission-bound files get a human in the loop. This is deliberately
        # narrower than `interrupt_on={"write_file": True}`, which would stop
        # on every scratch note and train you to rubber-stamp approvals.
        rules.append(
            FilesystemPermission(
                operations=["write"],
                paths=["/applications/*/final/**"],
                mode="interrupt",
            )
        )

    rules.extend(
        [
            FilesystemPermission(
                operations=["write"],
                paths=["/applications/**", "/memories/**"],
                mode="allow",
            ),
            # Everything else is read-only: the agent can consult /skills/ but
            # never edit its own instructions or the source tree.
            FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
        ]
    )
    return rules


def compliance_permissions() -> list[FilesystemPermission]:
    """Read-widely, write-narrowly rules for the compliance reviewer.

    A reviewer that can edit the thing it reviews is not a reviewer, so it may
    only write its own report.
    """
    return [
        FilesystemPermission(
            operations=["write"],
            paths=["/applications/*/review/**"],
            mode="allow",
        ),
        FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
    ]
