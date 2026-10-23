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
#
# The grader is `RubricMiddleware`'s model, and it judges the drafter's output
# against the funder's criteria. A weaker model grading a stronger one's prose
# passes what it cannot see the fault in, so it sits in the drafter's tier.
DEFAULT_MODELS = {
    "drafting": "anthropic:claude-opus-5-5",
    "research": "anthropic:claude-sonnet-5-5",
    "compliance": "anthropic:claude-sonnet-5-5",
    "grader": "anthropic:claude-opus-5-5",
    "discovery": "anthropic:claude-sonnet-5-5",
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
# the truncation, never binds. Every shipped model thinks, and reasoning is
# spent from the same budget, which is why the headroom is generous.
MAX_OUTPUT_TOKENS = 64000

# Set explicitly for the same reason as the ceiling: the provider default moves
# under us. Opus 5.5 defaults to `medium` where Opus 5 defaulted to `high`, so
# the upgrade alone would have made drafting think less -- no error, just a
# shallower proposal. Thinking cannot be disabled on these models; effort is
# the only depth control. Anthropic specs only: other providers reject the key.
#
# Per role, keyed like DEFAULT_MODELS and overridable the same way, because the
# roles do not need the same depth: a scan that decides whether to apply at all
# is not a section someone submits. Discovery runs at `medium`: one scout call
# per candidate, a human reads the shortlist, and the scout eval held its
# citations at 42% less cost. Research stays `high` despite gathering rather
# than writing: it is under 1% of a drafting run, and at `medium` it dropped
# the per-criterion framing every section is shaped around (D4, 2026-10-22).
# Drafting writes what is submitted, and the grader and compliance reviewer
# judge it -- a judge cheaper than the work it judges passes what it should
# catch -- so those stay `high` too. An override outside
# langchain-anthropic's accepted levels fails loudly, at construction: the
# field is a `Literal`.
DEFAULT_EFFORT = {
    "drafting": "high",
    "research": "high",
    "compliance": "high",
    "grader": "high",
    "discovery": "medium",
}

DRAFTING_EFFORT = os.getenv("GRANT_WRITER_DRAFTING_EFFORT", DEFAULT_EFFORT["drafting"])
RESEARCH_EFFORT = os.getenv("GRANT_WRITER_RESEARCH_EFFORT", DEFAULT_EFFORT["research"])
COMPLIANCE_EFFORT = os.getenv(
    "GRANT_WRITER_COMPLIANCE_EFFORT", DEFAULT_EFFORT["compliance"]
)
GRADER_EFFORT = os.getenv("GRANT_WRITER_GRADER_EFFORT", DEFAULT_EFFORT["grader"])
DISCOVERY_EFFORT = os.getenv(
    "GRANT_WRITER_DISCOVERY_EFFORT", DEFAULT_EFFORT["discovery"]
)

# A deployment's database. Unprefixed because hosting platforms inject exactly
# this name when they attach a managed Postgres. Read only by
# `persistent_settings`, so `Settings()` built directly (tests, library use)
# never reaches a database because of the shell it happens to run in. `or
# None` because an empty value means unset, as it does for every key here.
DATABASE_URL = os.getenv("DATABASE_URL") or None
# Parsed here rather than defaulted on a bad value: a typo should stop the
# process at import, not quietly run a deployment on a pool nobody sized.
DB_POOL_MAX_SIZE = int(os.getenv("GRANT_WRITER_DB_POOL_MAX_SIZE") or 10)
DB_POOL_TIMEOUT = float(os.getenv("GRANT_WRITER_DB_POOL_TIMEOUT") or 30)

# The limits a signed-in session is held to (decision 13, invariant 26): an
# estimated spend per user per UTC day, and the turns in flight at once across
# the deployment. Conservative by default -- sized for a low Anthropic
# rate-limit tier -- and parsed at import for the pool sizes' reason. They
# reach a `Settings` only through `persistent_settings`, like the database.
DAILY_BUDGET_USD = float(os.getenv("GRANT_WRITER_DAILY_BUDGET_USD") or 10)
MAX_CONCURRENT_RUNS = int(os.getenv("GRANT_WRITER_MAX_CONCURRENT_RUNS") or 3)
SEARCH_COST_USD = float(os.getenv("GRANT_WRITER_SEARCH_COST_USD") or 0.008)

# Operations (E7). The tracing mode is decision 16: `hidden` sends LangSmith
# timing, tokens, errors and the call tree but no inputs, outputs or metadata.
# Unset, it is chosen by `tracing_mode` from whether the process is shared.
# Rejected at import when misspelt, like the pool sizes: a typo here must not
# quietly fall back to sending full content from a deployment.
TracingMode = Literal["full", "hidden", "off"]
_TRACING_MODES: dict[str, TracingMode] = {
    "full": "full",
    "hidden": "hidden",
    "off": "off",
}
_tracing_raw = (os.getenv("GRANT_WRITER_TRACING") or "").strip().lower()
if _tracing_raw and _tracing_raw not in _TRACING_MODES:
    msg = f"GRANT_WRITER_TRACING must be full, hidden or off, not {_tracing_raw!r}"
    raise ValueError(msg)
TRACING: TracingMode | None = _TRACING_MODES.get(_tracing_raw)
LOG_LEVEL = os.getenv("GRANT_WRITER_LOG_LEVEL") or "INFO"
# How long a thread may sit on an approval before `grant-writer ops-check`
# reports it. Three days covers a weekend without paging anyone.
PARKED_ALERT_DAYS = float(os.getenv("GRANT_WRITER_PARKED_ALERT_DAYS") or 3)


def tracing_mode(*, shared: bool) -> TracingMode:
    """The tracing mode for this process: the explicit setting, if any.

    Otherwise `hidden` for a shared process -- signed in, or on the
    deployment's database -- and `full` for one person's machine, where the
    traces are that person's own and full content is what debugging needs.
    The default leans toward hiding, so a deployment that forgot the variable
    sends no proposal text.
    """
    if TRACING is not None:
        return TRACING
    return "hidden" if shared else "full"


def _allowlist(name: str, *, strip: str = "") -> frozenset[str]:
    """A comma-separated variable as a set of lowercased entries."""
    raw = os.getenv(name) or ""
    return frozenset(
        item.strip().lower().lstrip(strip) for item in raw.split(",") if item.strip()
    )


# Who may sign in (invariant 26). Signing in proves only that the provider
# knows the account; a provider configured for any Google account admits the
# whole internet. Both empty means nobody: a signed-in deployment with no
# allowlist refuses to start rather than admitting everyone.
ALLOWED_DOMAINS = _allowlist("GRANT_WRITER_ALLOWED_DOMAINS", strip="@")
ALLOWED_EMAILS = _allowlist("GRANT_WRITER_ALLOWED_EMAILS")


def build_model(spec: str, *, effort: str) -> BaseChatModel:
    """Resolve a `provider:model` spec into a chat model with a real ceiling.

    Called lazily from `build_agent`/`build_subagents` rather than at import, so
    importing this module stays cheap and the CLI's `require_api_keys` check
    still reports missing credentials before any client is constructed.

    `effort` is required, with no default, so a new call site cannot quietly
    inherit some other role's depth: each caller names its own role's
    `*_EFFORT`, and the eval judge names its pinned `JUDGE_EFFORT`, so a
    before/after comparison of an effort change is scored by the same judge on
    both sides. It is ignored for non-Anthropic specs, which reject the key.

    It is also put in the model's trace `metadata`, because langchain-anthropic
    records the model and `max_tokens` on a run but not the effort sent -- and
    with roles at different efforts on the same model id, the trace is where a
    surprising cost or a shallow answer gets traced back to its setting. That
    field labels the run only; it is not the API's request `metadata`.
    """
    if spec.startswith("anthropic:"):
        return init_chat_model(
            spec,
            max_tokens=MAX_OUTPUT_TOKENS,
            effort=effort,
            metadata={"effort": effort},
        )
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

    database_url: str | None = None
    """A Postgres connection string. When set, it wins over ``checkpoint_db``:
    the checkpointer *and* the Store live in Postgres, so threads and org
    memory outlive the process and are shared by every process pointed at it.
    For a deployment; ``None`` keeps SQLite or memory as above."""

    db_pool_max_size: int = 10
    """Most Postgres connections one process holds, shared by both graphs."""

    db_pool_timeout: float = 30.0
    """Seconds to wait for a free connection before raising ``PoolTimeout``,
    so an exhausted pool or an unreachable database fails with a message
    rather than hanging a run."""

    daily_budget_usd: float = 10.0
    """Estimated model and search spend one signed-in user may reach per UTC
    day before a turn is refused. Single-user runs are not limited."""

    max_concurrent_runs: int = 3
    """Signed-in turns that may be in flight at once across the deployment, so
    a busy hour is told to wait rather than failing part-way on HTTP 429."""

    allowed_domains: frozenset[str] = frozenset()
    """Email domains whose verified accounts may sign in, matched exactly."""

    allowed_emails: frozenset[str] = frozenset()
    """Individual verified addresses that may sign in, beside the domains."""

    search_cost_usd: float = 0.008
    """What one web search is estimated to cost, for the daily limit. Tavily
    bills per search credit, and a basic search is one."""

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
    def memory_template_path(self) -> Path:
        """The blank profile that ships, which `memory_path` is created from.

        Deliberately outside every `CONTENT_DIRS` entry, so no agent can write
        it: the catch-all deny covers it, and the real-disk writers refuse it.
        Kept under `memories/`, the first real run's facts could land in a
        tracked file -- exactly the leak untracking the profile closes.
        """
        return self.root / "templates" / "org-profile.md"

    @property
    def default_checkpoint_db(self) -> Path:
        return self.root / ".grant_writer" / "checkpoints.sqlite"

    @property
    def run_table_db(self) -> Path:
        """The run table's SQLite file, when there is no ``database_url``.

        A file of its own rather than a table in the checkpoint file, so a
        limit check never waits on the lock `grant-writer chat` holds there.
        """
        return self.root / ".grant_writer" / "runs.sqlite"

    def ensure_dirs(self) -> None:
        """Create the directories the agent expects to write into.

        Spelled out rather than looped over `CONTENT_DIRS`: `memories/` is
        named here by the file inside it, not by the directory, so a loop
        would need a special case for it anyway. The listing functions below
        tolerate a missing directory regardless -- this only means a fresh
        checkout has somewhere to put a scan before the first one runs.

        It also creates the organisation profile from its template, which
        only the `local` profile needs: `server` seeds the Store instead, and
        `seed_store_from_disk` falls back to the same template itself.
        """
        self.applications_path.mkdir(parents=True, exist_ok=True)
        self.opportunities_path.mkdir(parents=True, exist_ok=True)
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        # The profile is the organisation's accumulated record and is not
        # tracked, so a fresh checkout has none. Created from the template
        # once, and never again: mode "x" refuses an existing file atomically,
        # where an exists() check would race a second process.
        if self.memory_template_path.is_file():
            try:
                with self.memory_path.open("x", encoding="utf-8") as profile:
                    profile.write(self.memory_template_path.read_text(encoding="utf-8"))
            except FileExistsError:
                pass


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


def application_dir(settings: Settings, app_id: str, *, user_id: str | None) -> Path:
    """Resolve an application id to its directory, refusing anything unsafe.

    Frontends that touch the real filesystem must route through this rather
    than joining user input inline. `fetch_grants_gov_opportunity` needs the
    same boundary for a different input (a virtual path from the model) and
    enforces it in `tools._resolve_output_path`; both use the same ordering --
    resolve first, check second -- because validating the raw string lets `..`
    pass the prefix test and escape when it collapses.

    `user_id` is required, with no default, for the reason `build_model`'s
    `effort` is: a default is how a new call site would land in the shared
    tree without anyone choosing it. None is the single-user tree; a user id
    resolves inside that user's own directory (see `user_content_root`).

    Raises:
        ValueError: if the id is empty, not a plain path segment, or would
            resolve outside `applications/` (or the user's directory in it).
    """
    return _resolve_content_id(
        user_content_root(settings.applications_path, user_id),
        app_id,
        noun="application id",
    )


def opportunities_dir(settings: Settings, scan_id: str, *, user_id: str | None) -> Path:
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

    `user_id` is required, as on `application_dir`.

    Raises:
        ValueError: if the id is empty, not a plain path segment, or would
            resolve outside `opportunities/` (or the user's directory in it).
    """
    return _resolve_content_id(
        user_content_root(settings.opportunities_path, user_id),
        scan_id,
        noun="scan id",
    )


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


# A user's thread is `user:<user id>:<thread id>`, where the inner thread id is
# an application id or a `discovery_thread_id`. `_SAFE_ID` forbids `:` in a user
# id and in an application id alike, so the first colon after the prefix always
# ends the user id, and no typed id can spell another user's thread -- invariant
# 12's construction, one level up. Invariant 23.
_USER_THREAD_PREFIX = "user:"


def _checked_user_id(user_id: str) -> str:
    """Refuse a user id that is not a single plain path segment.

    Not stripped, unlike a typed id: a user id comes from the sign-in session,
    never from a text box, so a padded one is a bug upstream to surface rather
    than to tidy away.
    """
    if user_id in {".", ".."} or not _SAFE_ID.match(user_id):
        msg = f"invalid user id {user_id!r}"
        raise ValueError(msg)
    return user_id


def user_thread_id(user_id: str | None, thread_id: str) -> str:
    """The LangGraph thread id for `thread_id` as seen by `user_id`.

    Two people who both name an application `nsf-2027` must not resume each
    other's conversation, plan, drafts, or pending approval -- invariant 12's
    collision, between users instead of graphs, and the same fix: a prefix
    whose separator no valid id can contain. It composes with that one,
    `user_thread_id(u, discovery_thread_id(s))`, so the two namespaces nest
    rather than compete.

    None is single-user and returns `thread_id` unchanged. That is what the CLI
    always passes (decision 9) and what the UI passes until sign-in supplies a
    user, so an existing checkpoint keeps resuming and a run started in one
    frontend still continues in the other.

    Every caller goes through this, never a literal: `thread_user_id` parses
    the same shape back out, and the real-disk tools trust that parse.
    """
    if user_id is None:
        return thread_id
    return f"{_USER_THREAD_PREFIX}{_checked_user_id(user_id)}:{thread_id}"


def thread_user_id(thread_id: str | None) -> str | None:
    """The user a thread belongs to, or None for a single-user thread.

    The inverse of `user_thread_id`, and the reason the tools that write to
    real disk need no second key in `configurable`: the thread id is the one
    value every checkpointed run must carry, so the user travels inside it and
    a caller cannot forget to send one without also losing the thread. A
    separate `user_id` key, forgotten, would have written into the shared tree.

    A single-user id can never start with the prefix: an application id has no
    `:`, and a discovery id starts `discover:`.

    Raises:
        ValueError: if `thread_id` has the prefix but not the shape.
    """
    if not thread_id or not thread_id.startswith(_USER_THREAD_PREFIX):
        return None
    user_id, sep, inner = thread_id.removeprefix(_USER_THREAD_PREFIX).partition(":")
    if not sep or not inner:
        msg = f"malformed per-user thread id {thread_id!r}"
        raise ValueError(msg)
    return _checked_user_id(user_id)


def user_content_root(base: Path, user_id: str | None) -> Path:
    """`base` itself, or `user_id`'s own directory directly beneath it.

    The directory is `@<user id>`. `_SAFE_ID` has no `@`, so no application or
    scan id names a user's directory -- the single-user tree cannot reach into
    one, the listing drops them as names the boundary refuses, and one user's
    id can only ever resolve under their own. The disk half of invariant 23:
    `applications/` and `opportunities/` are written past the permission rules
    (invariant 2), so separating thread ids alone would have left user B's scan
    `x` overwriting user A's.
    """
    if user_id is None:
        return base
    return base / f"@{_checked_user_id(user_id)}"


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


def application_ids(settings: Settings, *, user_id: str | None) -> list[str]:
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
        user_content_root(settings.applications_path, user_id),
        lambda name: application_dir(settings, name, user_id=user_id),
    )


def opportunity_scan_ids(settings: Settings, *, user_id: str | None) -> list[str]:
    """Every discovery scan on disk, as ids `opportunities_dir` will accept.

    The read-side counterpart to `opportunities_dir`, and the same argument as
    `application_ids` above in every particular -- it backs the same kind of
    picker, beside the same kind of text box, over a directory the agent is
    still writing into. It runs each candidate through the boundary rather than
    re-testing `_SAFE_ID`, for the reason spelled out there.
    """
    return _list_content_ids(
        user_content_root(settings.opportunities_path, user_id),
        lambda name: opportunities_dir(settings, name, user_id=user_id),
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

    It is also the one place ``DATABASE_URL`` reaches a ``Settings``: set, both
    the checkpoint and the Store go to Postgres and the SQLite path is unused.
    The spending and concurrency limits arrive here too, for the same reason:
    a library caller or a test is never limited by its shell's environment.
    """
    base = Settings(
        backend_profile=backend_profile,
        approve_final=approve_final,
        enable_search=enable_search,
        database_url=DATABASE_URL,
        db_pool_max_size=DB_POOL_MAX_SIZE,
        db_pool_timeout=DB_POOL_TIMEOUT,
        daily_budget_usd=DAILY_BUDGET_USD,
        max_concurrent_runs=MAX_CONCURRENT_RUNS,
        search_cost_usd=SEARCH_COST_USD,
        allowed_domains=ALLOWED_DOMAINS,
        allowed_emails=ALLOWED_EMAILS,
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
