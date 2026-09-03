"""Runtime configuration for the grant writer agent.

Everything that varies between a laptop run and a deployed run is resolved
here, so the rest of the package never reads ``os.environ`` directly.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sized
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

load_dotenv()

BackendProfile = Literal["local", "server"]

# Prose quality is the whole product, so drafting gets the strongest model.
# Research is synthesis over search results and compliance checking is largely
# mechanical, so both run cheaper without hurting the output.
#
# The defaults live in a dict rather than inline in the `getenv` calls so tests
# can pin what the project *ships* regardless of any GRANT_WRITER_*_MODEL set in
# the developer's environment or .env -- see test_wiring, which asserts each one
# resolves to a real output ceiling.
DEFAULT_MODELS = {
    "drafting": "anthropic:claude-opus-5",
    "research": "anthropic:claude-sonnet-5",
    "compliance": "anthropic:claude-sonnet-5",
    "grader": "anthropic:claude-sonnet-5",
    "discovery": "anthropic:claude-sonnet-5",
}

DRAFTING_MODEL = os.getenv("GRANT_WRITER_DRAFTING_MODEL", DEFAULT_MODELS["drafting"])
RESEARCH_MODEL = os.getenv("GRANT_WRITER_RESEARCH_MODEL", DEFAULT_MODELS["research"])
COMPLIANCE_MODEL = os.getenv(
    "GRANT_WRITER_COMPLIANCE_MODEL", DEFAULT_MODELS["compliance"]
)
GRADER_MODEL = os.getenv("GRANT_WRITER_GRADER_MODEL", DEFAULT_MODELS["grader"])
# Discovery searches, reads synopses, and fills in a fixed rubric. That is
# triage against stated criteria rather than prose anyone submits, so it sits
# with research and compliance rather than with drafting -- and it is its own
# key rather than a reuse of RESEARCH_MODEL because a scan is the cheap step
# that decides whether the expensive one happens at all, which is exactly the
# knob someone will want to turn independently.
DISCOVERY_MODEL = os.getenv("GRANT_WRITER_DISCOVERY_MODEL", DEFAULT_MODELS["discovery"])

# Set the output ceiling here rather than inheriting whichever profile the
# installed langchain-anthropic happens to bundle. A model id it does not
# recognize silently resolves to 4096 -- valid id, HTTP 200, no exception, and a
# narrative that stops mid-sentence. That fallback applies to
# GRANT_WRITER_*_MODEL overrides too, which no test can enumerate, so the fix
# belongs at the construction site rather than in a version pin. 64000 tokens is
# roughly 48,000 words: far past any section limit, so this only ever removes
# the truncation, never binds. Models that think (Opus 5 does so by default)
# spend reasoning from the same budget, which is why the headroom is generous.
MAX_OUTPUT_TOKENS = 64000


def build_model(spec: str) -> BaseChatModel:
    """Resolve a `provider:model` spec into a chat model with a real ceiling.

    Called lazily from `build_agent`/`build_subagents` rather than at import, so
    importing this module stays cheap and the CLI's `require_api_keys` check
    still reports missing credentials before any client is constructed.
    """
    return init_chat_model(spec, max_tokens=MAX_OUTPUT_TOKENS)


def _resolve_root() -> Path:
    """Locate the project root (the directory holding ``skills/`` etc.).

    ``Path(__file__).parents[2]`` is correct for a repo checkout or an editable
    install, but a non-editable install (``pipx``/``uv tool install`` of the
    declared console script) puts this file under ``site-packages`` where none
    of ``skills/``, ``memories/``, or ``applications/`` exist. So we prefer an
    explicit ``GRANT_WRITER_ROOT``, then the source-relative root if it actually
    looks like the project, then the current working directory.
    """
    env_root = os.getenv("GRANT_WRITER_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    src_root = Path(__file__).resolve().parents[2]
    if (src_root / "skills").is_dir():
        return src_root

    cwd = Path.cwd()
    if (cwd / "skills").is_dir():
        return cwd

    # Nothing looks like a project; fall back to the source-relative guess so
    # behaviour is at least deterministic (and ensure_dirs will create dirs).
    return src_root


PROJECT_ROOT = _resolve_root()

# Virtual paths the agent sees. These are the *agent's* view of the world; the
# backend maps them onto real directories (local) or a Store (server).
SKILLS_DIR = "/skills/"
MEMORY_FILE = "/memories/org/AGENTS.md"
APPLICATIONS_DIR = "/applications/"

# The directories the agent may write into, named once.
#
# Two places enforce this boundary and they need two different shapes of the
# same list: `backends.build_permissions` wants `/<name>/**` globs for writes
# that go through the backend, and `tools._resolve_output_path` wants
# `root / <name>` paths for the tools that write to real disk and so bypass
# `FilesystemPermission` entirely (invariant 2). Bare names, because deriving
# both shapes from one list is what keeps them in step -- before this, each
# site spelled the pair of directories out itself, and a third directory added
# to one and not the other reopens exactly the gap invariant 2 exists to close.
#
# `prompts.WORKSPACE_CONVENTIONS` is the third place this appears, in prose the
# model reads. It cannot be generated from here without making the prompt text
# depend on a tuple's repr, so it stays hand-written -- and stays something to
# update alongside this.
CONTENT_DIRS: tuple[str, ...] = ("applications", "memories", "opportunities")


@dataclass(frozen=True)
class Settings:
    """Resolved settings for one agent build."""

    root: Path = PROJECT_ROOT
    backend_profile: BackendProfile = "local"
    """``local`` writes real files to disk; ``server`` keeps drafts in graph
    state and org memory in a Store. Never use ``local`` inside a web server."""

    approve_final: bool = False
    """Require human approval before anything under ``/applications/*/final/``
    is written."""

    enable_search: bool = True
    """Whether funder research may use web search. ``False`` forces the search
    tool off even when ``TAVILY_API_KEY`` is set (what ``--no-search`` sets)."""

    checkpoint_db: Path | None = None
    """When set, the agent persists its checkpoint to this SQLite file so
    conversation and todos survive across separate processes. ``None`` uses an
    in-memory checkpointer (tests, and library-default behaviour)."""

    max_rubric_iterations: int = 3

    @property
    def applications_path(self) -> Path:
        return self.root / "applications"

    @property
    def opportunities_path(self) -> Path:
        return self.root / "opportunities"

    @property
    def memory_path(self) -> Path:
        return self.root / "memories" / "org" / "AGENTS.md"

    @property
    def default_checkpoint_db(self) -> Path:
        return self.root / ".grant_writer" / "checkpoints.sqlite"

    def ensure_dirs(self) -> None:
        """Create the directories the agent expects to write into.

        Spelled out rather than looped over `CONTENT_DIRS`: `memories/` is
        named here by the file inside it, not by the directory, so a loop
        would need a special case for it anyway. The listing functions below
        tolerate a missing directory regardless -- this only means a fresh
        checkout has somewhere to put a scan before the first one runs.
        """
        self.applications_path.mkdir(parents=True, exist_ok=True)
        self.opportunities_path.mkdir(parents=True, exist_ok=True)
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)


# An application id names a directory on disk and doubles as the LangGraph
# thread id. A scan id names a directory the same way. Both are user input in
# every frontend, so both have to be a single plain path segment:
# `Path("/repo/applications") / "/etc/passwd"` silently discards the base and
# yields `/etc/passwd`, and `..` walks out of the tree.
#
# Named for the shape of the hazard rather than for applications, because it
# now guards two kinds of id. A second regex for scan ids would have been a
# fork of this rule, not an extension of it.
_SAFE_ID = re.compile(r"\A[A-Za-z0-9._-]+\Z")


def _resolve_content_id(root: Path, item_id: str, *, noun: str) -> Path:
    """The shared mechanics behind `application_dir` and `opportunities_dir`.

    Both take a user-typed id and join it onto a real directory outside
    `FilesystemPermission`'s reach, so both need the identical resolve-first,
    check-second order invariant 2 requires -- validating the raw string lets
    `..` pass the prefix test and then escape when it collapses. One function
    rather than two copies, so that ordering cannot be fixed in one place and
    left wrong in the other: a boundary duplicated is a boundary that drifts,
    which is the whole argument invariant 2 makes about the writers.

    `noun` only shapes the message, so a refusal names the field the caller
    actually typed into rather than whichever one this happens to guard.
    """
    candidate = item_id.strip()
    if candidate in {".", ".."} or not _SAFE_ID.match(candidate):
        msg = (
            f"invalid {noun} {item_id!r}: use letters, digits, dots, "
            "dashes, or underscores -- no path separators"
        )
        raise ValueError(msg)

    base = root.resolve()
    resolved = (base / candidate).resolve()
    if not resolved.is_relative_to(base):
        msg = f"refusing to resolve {item_id!r} outside {base.name}/"
        raise ValueError(msg)
    return resolved


def application_dir(settings: Settings, app_id: str) -> Path:
    """Resolve an application id to its directory, refusing anything unsafe.

    Frontends that touch the real filesystem must route through this rather
    than joining user input inline. `extract_pdf_text` needs the same boundary
    for a different input (a virtual path from the model) and enforces it in
    `tools._resolve_output_path`; both use the same ordering -- resolve first,
    check second -- because validating the raw string lets `..` pass the prefix
    test and escape when it collapses.

    Raises:
        ValueError: if the id is empty, not a plain path segment, or would
            resolve outside `applications/`.
    """
    return _resolve_content_id(
        settings.applications_path, app_id, noun="application id"
    )


def opportunities_dir(settings: Settings, scan_id: str) -> Path:
    """Resolve a discovery scan id to its directory, refusing anything unsafe.

    Structurally identical to `application_dir` -- see `_resolve_content_id`
    for why that is one function and not two.

    The UI reads through this rather than writing an upload through it, which
    is a weaker-looking reason to have it and is not: `application_ids`'
    docstring already spells out that an id of `..` lists the repo root, and
    the file browser's download button would then hand `.env` -- live API
    keys -- to the browser. The read side carries that risk whether or not
    anything writes here yet.

    A scan id and an application id share this character set and one
    checkpoint file, but never a thread id -- see `agent.discovery_thread_id`.

    Raises:
        ValueError: if the id is empty, not a plain path segment, or would
            resolve outside `opportunities/`.
    """
    return _resolve_content_id(settings.opportunities_path, scan_id, noun="scan id")


def discovery_thread_id(scan_id: str) -> str:
    """The LangGraph thread id for a discovery scan.

    Prefixed, because a scan id and an application id are the same shape and
    share one checkpoint file. The expected workflow is to reuse the name --
    scan `rural-health-2026`, then draft the winner as `rural-health-2026` --
    and an unprefixed thread id would make those the same checkpoint row: two
    structurally different graphs resuming each other's conversation, with
    nothing raised and no symptom until someone notices the drafter answering
    as though it had been searching.

    `_SAFE_ID` forbids `:`, so this prefix cannot collide with any application
    thread id by construction rather than by convention. Every caller goes
    through this function rather than repeating the literal, for the reason
    `CONTENT_DIRS` exists one file over.
    """
    return f"discover:{scan_id}"


def _list_content_ids(directory: Path, resolver: Callable[[str], Path]) -> list[str]:
    """The shared mechanics behind `application_ids` and `opportunity_scan_ids`.

    `resolver` is *called*, never re-implemented. That is the whole point: see
    `application_ids` below for the argument, which applies verbatim to scans.
    """
    try:
        names = sorted(path.name for path in directory.iterdir())
    except OSError:
        return []

    ids = []
    for name in names:
        if name.startswith(".") or name != name.strip():
            continue
        try:
            resolved = resolver(name)
        except ValueError:
            continue
        # After the boundary, not before: a name that escapes must be refused on
        # that ground, and `is_dir` on the resolved path is what drops the loose
        # `notes.md` sitting beside the directories.
        if resolved.is_dir():
            ids.append(name)
    return ids


def application_ids(settings: Settings) -> list[str]:
    """Every application on disk, as ids `application_dir` will accept.

    The read-side counterpart to `application_dir`. Each candidate is run
    through that boundary rather than through a matching copy of its rules, so
    the two agree *literally* -- an option this offers cannot be one the
    boundary then refuses. Re-checking `_SAFE_ID` here instead was not the
    same thing: `ln -s /elsewhere applications/legacy` passes any name test, and
    `application_dir` resolves the symlink, finds it outside the tree, and
    raises. The directory is *listed*, not trusted.

    Two names are dropped on grounds the boundary has no opinion about, because
    acceptance is not the only question a listing has to answer.

    Dot-leading names are dropped because the regex admits them --
    only `.` and `..` are special-cased -- so `.ipynb_checkpoints` and `.git`
    would otherwise sit in the picker beside real applications. This is a
    listing, and hiding the dotfiles is what a listing does.

    `iterdir` is guarded because the Streamlit frontend calls this from its
    script body, outside any try: an unreadable `applications/` must cost the
    picker, not the whole page. It also covers the fresh-checkout case where the
    directory does not exist yet, which is why there is no `is_dir` precheck.

    A padded name is dropped for a reason the boundary has no opinion about.
    `application_dir` strips before it validates, so `"nsf-26 "` is accepted
    and resolves to `applications/nsf-26` -- a different directory from the one
    being listed under that name. The narrow case this changes is both existing
    at once: `is_dir()` already drops the padded name when the unpadded
    directory does not exist, so what is removed is a second picker row that
    looks like `nsf-26`, is not, and opens it anyway. Not a containment fix --
    the boundary is still what decides acceptance.

    Sorted by name, not by mtime. This backs a dropdown, and a list that
    reorders itself under the pointer between reruns is worse than one that is
    merely not in recency order.
    """
    return _list_content_ids(
        settings.applications_path, lambda name: application_dir(settings, name)
    )


def opportunity_scan_ids(settings: Settings) -> list[str]:
    """Every discovery scan on disk, as ids `opportunities_dir` will accept.

    The read-side counterpart to `opportunities_dir`, and the same argument as
    `application_ids` above in every particular -- it backs the same kind of
    picker, beside the same kind of text box, over a directory the agent is
    still writing into. It runs each candidate through the boundary rather than
    re-testing `_SAFE_ID`, for the reason spelled out there.
    """
    return _list_content_ids(
        settings.opportunities_path, lambda name: opportunities_dir(settings, name)
    )


def persistent_settings(
    *,
    backend_profile: BackendProfile = "local",
    approve_final: bool = False,
    enable_search: bool = True,
) -> Settings:
    """Settings for an interactive frontend, checkpointed to disk.

    Every frontend wants the same thing an id-per-application implies: the app
    id doubles as the LangGraph ``thread_id``, and conversation, plan, and
    pending approvals survive the process, so a run started in the UI can be
    continued from ``grant-writer chat`` and vice versa. Constructing
    ``Settings`` directly instead (tests, library use) stays in memory.
    """
    base = Settings(
        backend_profile=backend_profile,
        approve_final=approve_final,
        enable_search=enable_search,
    )
    return replace(base, checkpoint_db=base.default_checkpoint_db)


TraceCommand = Literal["draft", "chat", "discover"]
TraceFrontend = Literal["cli", "ui"]


def trace_config(
    settings: Settings,
    *,
    command: TraceCommand,
    frontend: TraceFrontend,
    ref: str,
    thread_id: str,
    recursion_limit: int,
    details: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """The `RunnableConfig` for one turn -- its labels, and part of its row.

    Both frontends build their turn config here, because the alternative is
    what was here before: four literal dicts carrying `thread_id` and
    `recursion_limit` and nothing else. Tracing is automatic for a LangGraph
    app, so those runs did reach LangSmith -- they simply arrived as one of two
    graph names with no way to tell `draft` from `chat`, the UI from the CLI,
    or an `--approve` run from a plain one. Free traces, unfilterable.

    **`run_name`, `tags`, and `metadata` are top-level keys, not `configurable`
    ones.** Nested under `configurable` they are accepted, carried as graph
    configuration, and ignored by the tracer: the run keeps the compiled
    graph's name, the filters stay empty, and nothing raises. `thread_id` sits
    where it does for the opposite reason -- the checkpointer reads it from
    `configurable` and nowhere else -- so the two halves of this dict look
    alike and are not, which is exactly the mistake worth centralising.

    The split is by shape. `tags` are flat strings, which is what the LangSmith
    UI and `langsmith trace list --tags` filter on, so they carry the kind of
    run and the flags it was made under. `metadata` carries identifiers, which
    are values to read rather than facets to filter by: the ids, the funder,
    the focus.

    **`metadata` is not tracing-only; `tags` and `run_name` are.**
    `langgraph.checkpoint.base.get_checkpoint_metadata` copies every str,
    int, float or bool in `config["metadata"]` *and* `config["configurable"]`
    into the `CheckpointMetadata` the saver writes on every step, skipping
    only `EXCLUDED_METADATA_KEYS` -- which holds `thread_id` and langgraph's
    own keys, and none of ours. So a drafting turn puts `frontend`,
    `command`, `app_id`, `funder` and `rubric` into
    `.grant_writer/checkpoints.sqlite` as well as into the trace. `tags` and
    `run_name` reach no row at all.

    Documented rather than worked around, because none of it is a new fact:
    `app_id` *is* the thread id, `scan_id` is the thread id minus its
    invariant-12 prefix, the rubric text is already checkpointed whole as
    `state["rubric"]`, and the funder, focus and agencies are already in the
    brief the same checkpoint stores verbatim. The copy runs from the shared
    audience to the local one, so the caution that keeps a facet fit for a
    trace -- `cli._draft` sending the rubric's name and never its `$HOME`
    path -- keeps it fit for the row by construction.

    What it costs is a rule in the other direction: **nothing may read these
    back.** One thread accumulates rows whose `command` disagrees -- `draft`
    from the opening run, `chat` from every later turn -- so a
    `checkpointer.list(filter={"command": ...})` added later would cut one
    conversation down to a subset of its own steps, selecting by a word
    chosen to read well in a trace, and raise nothing. `details` may
    therefore carry only values already recoverable from the thread.
    `test_review_fixes.py` §⑫ pins the copying through the upstream function
    rather than restating it, so a release that stops copying fails there
    and this paragraph is relaxed rather than left quietly wrong.

    `ref` is what the human typed; `thread_id` is what the checkpoint uses.
    They differ on purpose -- a scan runs under a namespaced id (invariant 12)
    -- so naming the run after `thread_id` would read `discover:discover:x`.
    That those two words are the same word is a coincidence of
    `discovery_thread_id` spelling its prefix with the verb this function takes
    as `command`; it makes `run_name` and `thread_id` identical strings for a
    scan today, and would stop the moment either is renamed. Callers pass both
    rather than have this re-derive one from the other, because re-deriving
    means owning a second copy of the prefix rule.

    A resumed segment carries the command of the turn it resumes, not one of
    its own: an approval landing mid-run is that turn continuing, so it
    belongs under the run name that turn was given. `cli._run` gets that by
    construction -- it loops on the one config its caller built -- and the UI
    by leaving `active_command` alone in `resume_with`.

    A follow-up is the opposite case: a new turn on an existing thread, which
    is what `chat` is for. `cli._chat` builds its own config and the UI's
    follow-up box asks for the same one, so on a shared thread `thread_id`
    says the two are one conversation while `run_name` says which of them was
    the opening brief. A UI follow-up on a *scan* is `chat` too -- the command
    names the shape of the turn, not the graph, and which graph ran stays
    legible from the `scan_id` metadata key and the `discover:` thread id.
    """
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": recursion_limit,
        "run_name": f"{command}:{ref}",
        "tags": [
            frontend,
            command,
            f"profile:{settings.backend_profile}",
            f"approve:{'on' if settings.approve_final else 'off'}",
            f"search:{'on' if settings.enable_search else 'off'}",
        ],
        "metadata": {
            "frontend": frontend,
            "command": command,
            "thread_id": thread_id,
            # Dropped when empty rather than sent as null: an absent `--funder`
            # should leave the facet off the run, not add one whose value is
            # None to every trace that did not use the flag. `""`, `[]`, `{}`
            # and `()` are that same absence, and the earlier
            # `value not in (None, "")` could not see the containers --
            # equality against two scalars misses an empty list, which then
            # arrives as an empty facet on exactly the runs this exists to
            # keep clean. Emptiness is asked of `Sized` rather than of the
            # value itself, because `if value` would drop `0` and `False`
            # with it -- a falsy number or flag is an answer about the run,
            # not a missing one.
            **{
                key: value
                for key, value in (details or {}).items()
                if value is not None
                and (not isinstance(value, Sized) or len(value) > 0)
            },
        },
    }


def require_api_keys(*, needs_search: bool = True) -> list[str]:
    """Return the names of any required keys that are missing."""
    missing = [k for k in ("ANTHROPIC_API_KEY",) if not os.getenv(k)]
    if needs_search and not os.getenv("TAVILY_API_KEY"):
        missing.append("TAVILY_API_KEY")
    return missing
