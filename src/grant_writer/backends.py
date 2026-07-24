"""Backend and permission wiring.

The two profiles exist because "persistence" means different things in the two
places this agent runs:

``local``
    The filesystem *is* the cross-session memory. A plain ``FilesystemBackend``
    rooted at the repo gives durable drafts you can open in any editor, and
    ``/memories/org/AGENTS.md`` survives restarts for free.

``server``
    There is no durable disk. Working drafts live in graph state (ephemeral,
    thread-scoped) while ``/skills/`` and ``/memories/`` are routed to a
    ``Store`` that is seeded from disk at startup (see ``seed_store_from_disk``)
    so the drafter still gets its section guides and the org profile.
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
from deepagents.backends.utils import create_file_data
from langgraph.store.base import BaseStore

from grant_writer.config import Settings

BackendFactory = BackendProtocol | Callable[[Any], BackendProtocol]

# Each routed prefix gets its OWN Store namespace. This matters: CompositeBackend
# strips the route prefix before delegating (``/memories/org/x`` -> ``/org/x``),
# so a single shared namespace would let ``ls /skills/`` also surface memory
# files and vice versa. Keys are stored prefix-stripped, exactly as the routed
# reads address them. Mapping: disk dir -> (virtual route prefix, namespace).
_SEED_ROUTES = {
    "skills": ("/skills/", ("filesystem", "skills")),
    "memories": ("/memories/", ("filesystem", "memories")),
}


def _namespace_factory(namespace: tuple[str, ...]) -> Callable[[Any], tuple[str, ...]]:
    return lambda _ctx: namespace


def build_backend(settings: Settings) -> BackendFactory:
    """Return the backend for the configured profile."""
    if settings.backend_profile == "local":
        settings.ensure_dirs()
        # virtual_mode confines the agent to `root`, rejecting `..` and `~`
        # escapes. It does NOT stop it writing to src/ -- permissions do that.
        return FilesystemBackend(root_dir=settings.root, virtual_mode=True)

    # `server`: drafts are ephemeral (StateBackend); /skills/ and /memories/ are
    # each routed to their own namespace in the Store, which `seed_store_from_disk`
    # fills at startup. Route prefixes must match exactly -- a bare "/memory/..."
    # path would silently fall through to StateBackend and vanish.
    def factory(_runtime: Any) -> CompositeBackend:
        # Newer deepagents resolves the store/context at call time, so the
        # backends take no runtime argument (passing one is deprecated).
        routes = {
            prefix: StoreBackend(namespace=_namespace_factory(namespace))
            for prefix, namespace in _SEED_ROUTES.values()
        }
        return CompositeBackend(default=StateBackend(), routes=routes)

    return factory


def seed_store_from_disk(store: BaseStore, settings: Settings) -> int:
    """Load on-disk skills and memory into the Store for the server profile.

    Without this, ``/skills/`` and ``/memories/`` route to an empty Store and
    the drafter/reviewer silently get no section guides and no org profile.
    Keys are stored prefix-stripped and namespaced per route, matching how
    CompositeBackend addresses them. Returns the number of files seeded.
    """
    count = 0
    for disk_dir, (_prefix, namespace) in _SEED_ROUTES.items():
        base = settings.root / disk_dir
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            # Prefix-stripped, leading slash: "/rfp-decomposition/SKILL.md".
            key = "/" + path.relative_to(base).as_posix()
            store.put(
                namespace,
                key,
                create_file_data(path.read_text(encoding="utf-8")),
            )
            count += 1
    return count


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
