"""Runtime configuration for the grant writer agent.

Everything that varies between a laptop run and a deployed run is resolved
here, so the rest of the package never reads ``os.environ`` directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

load_dotenv()

BackendProfile = Literal["local", "server"]

# Prose quality is the whole product, so drafting gets the strongest model.
# Research is synthesis over search results and compliance checking is largely
# mechanical, so both run cheaper without hurting the output.
DRAFTING_MODEL = os.getenv("GRANT_WRITER_DRAFTING_MODEL", "anthropic:claude-opus-4-8")
RESEARCH_MODEL = os.getenv("GRANT_WRITER_RESEARCH_MODEL", "anthropic:claude-sonnet-5")
COMPLIANCE_MODEL = os.getenv(
    "GRANT_WRITER_COMPLIANCE_MODEL", "anthropic:claude-sonnet-5"
)
GRADER_MODEL = os.getenv("GRANT_WRITER_GRADER_MODEL", "anthropic:claude-sonnet-5")


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
    def skills_path(self) -> Path:
        return self.root / "skills"

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


def require_api_keys(*, needs_search: bool = True) -> list[str]:
    """Return the names of any required keys that are missing."""
    missing = [k for k in ("ANTHROPIC_API_KEY",) if not os.getenv(k)]
    if needs_search and not os.getenv("TAVILY_API_KEY"):
        missing.append("TAVILY_API_KEY")
    return missing
