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

from collections.abc import Callable, Sequence
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

from grant_writer.config import CONTENT_DIRS, Settings

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
        # Annotated because `dict` is invariant in its value type: the inferred
        # `dict[str, StoreBackend]` is not a `dict[str, BackendProtocol]`.
        routes: dict[str, BackendProtocol] = {
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
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                # Skip non-text files (e.g. a stray .DS_Store or image); they
                # are not skills or memory, and one must not crash startup.
                continue
            # Prefix-stripped, leading slash: "/rfp-decomposition/SKILL.md".
            key = "/" + path.relative_to(base).as_posix()
            store.put(namespace, key, create_file_data(content))
            count += 1
    return count


def _write_rules(
    *,
    allow: Sequence[str],
    interrupt: Sequence[str] = (),
) -> list[FilesystemPermission]:
    """Build a write-rule list that cannot be put in the wrong order.

    Rules are evaluated first-match-wins and anything matching no rule is
    *allowed*, so the ordering is not a style question -- it is the policy.
    Two things have to hold, and neither raises when it stops holding:

    - the catch-all deny comes last, or it shadows every allow above it and the
      agent can write nothing;
    - an interrupt precedes the allow it carves out of, or the allow matches
      first and the submission-bound write goes through with no approval.

    Every caller below used to assemble that by hand and end with the same
    literal deny rule. Passing globs by role instead, and letting this function
    place them, is what makes both facts unlosable: there is no ordering left
    at a call site to get wrong, and the deny is written in exactly one place.

    `allow` is keyword-only and required so nobody can call this bare and get a
    deny-everything list back.
    """
    rules: list[FilesystemPermission] = []
    if interrupt:
        rules.append(
            FilesystemPermission(
                operations=["write"], paths=list(interrupt), mode="interrupt"
            )
        )
    rules.append(
        FilesystemPermission(operations=["write"], paths=list(allow), mode="allow")
    )
    # The only catch-all deny in this module. Everything not allowed above is
    # read-only: the agent can consult /skills/ but never edit its own
    # instructions or the source tree.
    rules.append(FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"))
    return rules


def build_permissions(settings: Settings) -> list[FilesystemPermission]:
    """Confine the drafting agent's writes to content directories."""
    return _write_rules(
        # Derived from `config.CONTENT_DIRS`, which `_resolve_output_path` also
        # reads. The two enforce this boundary in different shapes -- globs
        # here, real paths there -- and spelling the directories out at each
        # site is what let them drift apart; see invariant 2.
        allow=[f"/{name}/**" for name in CONTENT_DIRS],
        # Submission-bound files get a human in the loop. Deliberately narrower
        # than `interrupt_on={"write_file": True}`, which would stop on every
        # scratch note and train you to rubber-stamp approvals.
        interrupt=["/applications/*/final/**"] if settings.approve_final else (),
    )


def discovery_permissions() -> list[FilesystemPermission]:
    """Write rules for the discovery graph. **Never returns an interrupt rule.**

    This is what makes "a scan cannot park on an approval" a structural fact
    rather than a hopeful one, and both frontends depend on it: neither runs a
    `parked_state` check before starting a scan, on the grounds that there is
    nothing for a scan to abandon.

    Handing this graph `build_permissions` was not enough, and the gap is worth
    naming. That function allows every `CONTENT_DIRS` entry -- `/applications/`
    included -- and, with `approve_final` on, puts an interrupt on
    `/applications/*/final/**`. The discovery *orchestrator* has `write_file`
    like any other, and `WORKSPACE_CONVENTIONS` describes that exact directory
    to it. So one stray write there would park a thread nothing is watching,
    and the next scan submitted would discard the pending write silently --
    invariant 11's failure, reached through the graph built to be exempt from
    it. The claim has to be enforced here, not argued from the roster.

    `/memories/` stays writable because the shared conventions text invites the
    agent to record durable facts about the organization there, and no rule
    interrupts a write to it -- so allowing it keeps the prompt honest without
    reopening the hole.

    Note what is not passed: `_write_rules` takes `interrupt` and this omits
    it. The guarantee in the first line is therefore visible on one line of
    code rather than argued from the shape of a hand-built list.
    """
    return _write_rules(allow=["/opportunities/**", "/memories/**"])


def scouting_permissions() -> list[FilesystemPermission]:
    """Read-widely, write-narrowly rules for the opportunity scout.

    Same shape and same argument as `compliance_permissions`: a scout that can
    write outside its own scan can edit the applications it is supposed to be
    finding work for. It reads `/memories/org/AGENTS.md` to score against, so
    the read side stays open -- only writes are confined.

    Narrower than the parent's allow rule on purpose. `/opportunities/**` is
    writable by the discovery orchestrator, but a subagent that only ever emits
    one scored file per call has no reason to reach the rest of the tree, and
    the interrupt rule that protects `final/` does not exist here to catch it.
    """
    return _write_rules(allow=["/opportunities/**"])


def compliance_permissions() -> list[FilesystemPermission]:
    """Read-widely, write-narrowly rules for the compliance reviewer.

    A reviewer that can edit the thing it reviews is not a reviewer, so it may
    only write its own report.
    """
    return _write_rules(allow=["/applications/*/review/**"])
