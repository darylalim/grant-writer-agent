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
from collections.abc import Sequence
from pathlib import Path

from grant_writer.opportunities import ScoredOpportunity, parse_scored_markdown

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

# The same, for a discovery scan: raw candidate text first, then the scoring
# written from it.
SCAN_DIRECTORY_ORDER = ("candidates", "scored")

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


def _ordered_files(root: Path, order: Sequence[str]) -> list[Path]:
    """Every file under `root`, sorted to read the way a convention lays it out.

    `order` names top-level entries -- a file or a subdirectory. Shared by the
    application and scan listings because what differs between those two is the
    order, not the walk, and a second copy of the walk would be a second place
    for the missing-directory guard to be forgotten.
    """
    if not root.is_dir():
        return []

    def sort_key(path: Path) -> tuple[int, str]:
        relative = path.relative_to(root)
        head = relative.parts[0]
        rank = order.index(head) if head in order else len(order)
        return rank, str(relative)

    return sorted((p for p in root.rglob("*") if p.is_file()), key=sort_key)


def application_files(app_dir: Path) -> list[Path]:
    """Every file under an application directory, in workspace order."""
    return _ordered_files(app_dir, DIRECTORY_ORDER)


def scan_files(scan_dir: Path) -> list[Path]:
    """Every file under a discovery scan directory, in workspace order."""
    return _ordered_files(scan_dir, SCAN_DIRECTORY_ORDER)


def read_scored_opportunities(scan_dir: Path) -> list[ScoredOpportunity]:
    """Parse every scored candidate in a scan, unranked.

    One file per candidate, and the filename stem is the candidate's key --
    see `opportunities.parse_scored_markdown` on why that beats anything in the
    body. A file that vanished or will not decode parses as empty rather than
    raising, so one bad candidate costs its own row and not the shortlist:
    `read_text` already returns "" for both, and the parser records the absence
    as warnings.

    Unranked on purpose. `opportunities.rank_opportunities` is pure and this is
    not, so keeping them apart is what lets the ordering rules be tested
    without a directory on disk.
    """
    # Suffix compared case-insensitively, matching `count_gaps`. `glob("*.md")`
    # is case-sensitive on this filesystem, so a scout that wrote `NSF-26.MD`
    # had its markers counted by the metric above the list and its candidate
    # dropped from the list itself -- absent rather than flagged, since
    # `warnings` only exist for a file that was parsed. That is the same drift
    # `count_gaps` documents, inverted: there a case-sensitive test would have
    # shown gaps on screen and left them out of the count.
    # Guarded because `iterdir` raises on a directory that is not there, where
    # the `glob` it replaced returned []. A scan has no `scored/` until its
    # first scoring call lands, and both frontends read this from their script
    # body -- so the window between starting a scan and the first score is one
    # where an unguarded read takes down the whole page.
    try:
        entries = sorted((scan_dir / "scored").iterdir())
    except OSError:
        return []

    return [
        parse_scored_markdown(read_text(path), key=path.stem)
        for path in entries
        if path.is_file() and path.suffix.lower() == ".md"
    ]


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
