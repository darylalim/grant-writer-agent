"""Reading back what the agent wrote.

An application directory is the agent's output, and summarising it -- which
files exist, how many questions are still open, what the reviewer concluded --
is not presentation logic. It lives here so it can be tested against a real
directory rather than through a UI, and so a second reader does not have to
re-derive the conventions in WORKSPACE_CONVENTIONS.

Every reader is defensive about disappearing files. The listing is a snapshot
and the agent keeps writing, so a file can vanish between the listing and the
read; a raised FileNotFoundError here would take down whichever frontend asked.
"""

from __future__ import annotations

import re
from pathlib import Path

# The order WORKSPACE_CONVENTIONS lays out an application directory in, so a
# listing reads the way the agent works rather than alphabetically.
DIRECTORY_ORDER = (
    "rfp.md",
    "requirements.md",
    "research",
    "sections",
    "review",
    "final",
)

# The two verdicts COMPLIANCE_PROMPT requires. Matched as whole tokens so a
# report that merely discusses one in prose cannot decide the headline.
VERDICT_PATTERN = re.compile(r"\b(?:SUBMIT-READY|NOT-READY)\b")

NO_VERDICT = "—"


def read_text(path: Path) -> str:
    """File contents, or empty string if it cannot be read."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def read_bytes(path: Path) -> bytes | None:
    """Raw contents, or None if the file is gone."""
    try:
        return path.read_bytes()
    except OSError:
        return None


def modified_at(path: Path) -> float:
    """Modification time, or 0.0 if the file is gone -- sorts oldest."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def application_files(app_dir: Path) -> list[Path]:
    """Every file under an application directory, in workspace order."""
    if not app_dir.is_dir():
        return []

    def sort_key(path: Path) -> tuple[int, str]:
        relative = path.relative_to(app_dir)
        head = relative.parts[0]
        rank = (
            DIRECTORY_ORDER.index(head)
            if head in DIRECTORY_ORDER
            else len(DIRECTORY_ORDER)
        )
        return rank, str(relative)

    return sorted((p for p in app_dir.rglob("*") if p.is_file()), key=sort_key)


def count_gaps(files: list[Path]) -> int:
    """Unresolved `[NEEDS INPUT: ...]` markers across the drafts.

    The most important number a frontend can show: each one is a fact the agent
    refused to invent and a human still has to supply. Review reports are
    excluded because the compliance reviewer's job is to *collect* these
    markers, so counting its report would double every gap it found.

    The suffix test is case-insensitive, and has to agree with whatever the
    frontends call a draft: the file browser renders `NEEDS.MD` through the
    markdown branch, so a case-sensitive test here would render a file's gaps
    on screen while leaving them out of the count above it. Under-reporting
    this number is the one direction that matters -- each marker is a fact the
    agent refused to invent, and a total that silently omits some reads as
    fewer questions outstanding rather than as a bug.
    """
    return sum(
        read_text(path).count("[NEEDS INPUT")
        for path in files
        if path.suffix.lower() == ".md" and path.parent.name != "review"
    )


def compliance_verdict(files: list[Path]) -> str:
    """The compliance reviewer's most recent verdict, as it wrote it.

    `review/` accumulates a report per pass and COMPLIANCE_PROMPT fixes no
    filename, so the newest file wins rather than whichever sorts first --
    otherwise a stale NOT-READY outlives the fixes that cleared it. Within a
    file the *last* match wins, because the prompt asks for the verdict at the
    end and the prose above it may well say "no longer NOT-READY".

    Both shortcuts produce a confident wrong answer rather than an error, and
    COMPLIANCE_PROMPT calls a false all-clear the most expensive mistake in
    this system.
    """
    reviews = sorted(
        (path for path in files if path.parent.name == "review"),
        key=modified_at,
        reverse=True,
    )
    for path in reviews:
        if matches := VERDICT_PATTERN.findall(read_text(path)):
            return matches[-1]
    return NO_VERDICT
