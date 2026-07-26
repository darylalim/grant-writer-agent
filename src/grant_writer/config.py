"""Runtime configuration for the grant writer agent.

Everything that varies between a laptop run and a deployed run is resolved
here, so the rest of the package never reads ``os.environ`` directly.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

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
}

DRAFTING_MODEL = os.getenv("GRANT_WRITER_DRAFTING_MODEL", DEFAULT_MODELS["drafting"])
RESEARCH_MODEL = os.getenv("GRANT_WRITER_RESEARCH_MODEL", DEFAULT_MODELS["research"])
COMPLIANCE_MODEL = os.getenv(
    "GRANT_WRITER_COMPLIANCE_MODEL", DEFAULT_MODELS["compliance"]
)
GRADER_MODEL = os.getenv("GRANT_WRITER_GRADER_MODEL", DEFAULT_MODELS["grader"])

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
    def memory_path(self) -> Path:
        return self.root / "memories" / "org" / "AGENTS.md"

    @property
    def default_checkpoint_db(self) -> Path:
        return self.root / ".grant_writer" / "checkpoints.sqlite"

    def ensure_dirs(self) -> None:
        """Create the directories the agent expects to write into."""
        self.applications_path.mkdir(parents=True, exist_ok=True)
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)


# An application id names a directory on disk and doubles as the LangGraph
# thread id. It is user input in every frontend, so it has to be a single plain
# path segment: `Path("/repo/applications") / "/etc/passwd"` silently discards
# the base and yields `/etc/passwd`, and `..` walks out of the tree.
_SAFE_APP_ID = re.compile(r"\A[A-Za-z0-9._-]+\Z")


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
    candidate = app_id.strip()
    if candidate in {".", ".."} or not _SAFE_APP_ID.match(candidate):
        msg = (
            f"invalid application id {app_id!r}: use letters, digits, dots, "
            "dashes, or underscores -- no path separators"
        )
        raise ValueError(msg)

    root = settings.applications_path.resolve()
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root):
        msg = f"refusing to resolve {app_id!r} outside applications/"
        raise ValueError(msg)
    return resolved


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


def require_api_keys(*, needs_search: bool = True) -> list[str]:
    """Return the names of any required keys that are missing."""
    missing = [k for k in ("ANTHROPIC_API_KEY",) if not os.getenv(k)]
    if needs_search and not os.getenv("TAVILY_API_KEY"):
        missing.append("TAVILY_API_KEY")
    return missing
