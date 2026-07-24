"""Custom tools.

Deliberately small. The agent already gets ``ls``/``read_file``/``write_file``/
``edit_file``/``glob``/``grep`` from the filesystem middleware, so these only
cover what the harness cannot do: read a PDF solicitation, count words
reliably, and search the web.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from langchain.tools import tool

# Rough conversions used for page-limit estimates. Funders specify limits in
# pages but drafts exist as plain text, so these are the bridge -- always
# approximate, never a substitute for the real typeset PDF.
WORDS_PER_PAGE_SINGLE = 500
WORDS_PER_PAGE_DOUBLE = 250


def _parse_page_spec(spec: str, n_pages: int) -> list[int]:
    """Turn ``"1-3,7"`` into zero-based page indices."""
    if not spec.strip():
        return list(range(n_pages))
    wanted: list[int] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start_s, _, end_s = chunk.partition("-")
            start, end = int(start_s), int(end_s)
        else:
            start = end = int(chunk)
        wanted.extend(range(start - 1, end))
    return [p for p in wanted if 0 <= p < n_pages]


def _resolve_output_path(out_path: str) -> Path:
    """Map a virtual write path onto disk, refusing anything outside content dirs.

    This tool writes to the real filesystem rather than through the backend, so
    it does not inherit the FilesystemPermission rules and has to enforce the
    same boundary itself. Keep this in sync with ``build_permissions``.
    """
    from grant_writer.config import PROJECT_ROOT

    root = PROJECT_ROOT.resolve()
    allowed = (root / "applications", root / "memories")

    # Resolve BEFORE checking. Validating the raw string first lets
    # "/applications/../src/agent.py" pass the prefix test and then escape when
    # `..` collapses -- the check has to run on the final path, not the input.
    virtual = "/" + out_path.strip().lstrip("/")
    resolved = (root / virtual.lstrip("/")).resolve()

    if not any(resolved.is_relative_to(base) for base in allowed):
        msg = f"refusing to write outside /applications/ or /memories/: {out_path!r}"
        raise ValueError(msg)
    return resolved


@tool
def extract_pdf_text(
    pdf_path: str, pages: str = "", max_chars: int = 20000, out_path: str = ""
) -> str:
    """Extract text from a PDF solicitation, RFP, or funder guideline document.

    Args:
        pdf_path: Path to the PDF on the local machine.
        pages: Optional 1-based page selection such as "1-4,9". Empty means all
            pages. Use this to page through a long RFP when reading it.
        max_chars: Truncation limit when returning text to you. Ignored when
            `out_path` is set.
        out_path: If set (e.g. "/applications/nsf-26/rfp.md"), the COMPLETE
            extracted text is written straight to that file and you get back
            only a short receipt. Always use this to archive a solicitation.
            Never read a long PDF and copy it into `write_file` yourself -- the
            text gets silently abridged in transit and downstream compliance
            checks then run against an incomplete document.

    Returns:
        A write receipt when `out_path` is set, otherwise the extracted text
        with an explicit notice if it was truncated.
    """
    path = Path(pdf_path).expanduser()
    if not path.is_file():
        return f"Error: no such file: {path}"

    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - dependency is declared
        return "Error: pypdf is not installed."

    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001 - surface any parse failure to the agent
        return f"Error: could not read PDF: {exc}"

    total = len(reader.pages)
    try:
        indices = _parse_page_spec(pages, total)
    except ValueError:
        return f"Error: could not parse page spec {pages!r}. Use forms like '1-4,9'."
    if not indices:
        return f"Error: page spec {pages!r} selected no pages (document has {total})."

    parts = [f"[{path.name} - {total} page(s), showing {len(indices)}]"]
    for i in indices:
        try:
            text = reader.pages[i].extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            text = f"<page {i + 1} could not be extracted: {exc}>"
        parts.append(f"\n--- page {i + 1} ---\n{text.strip()}")

    out = "\n".join(parts)

    if out_path:
        try:
            target = _resolve_output_path(out_path)
        except ValueError as exc:
            return f"Error: {exc}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(out, encoding="utf-8")
        return (
            f"Wrote {len(out)} characters covering {len(indices)} of {total} page(s) "
            f"to {out_path}. The full text is on disk -- read it back with "
            f"read_file, in ranges if it is long. Do not re-transcribe it."
        )

    if len(out) > max_chars:
        shown = out[:max_chars]
        remaining = len(out) - max_chars
        return (
            f"{shown}\n\n[TRUNCATED: {remaining} more characters. "
            f"Re-call with a narrower `pages` range to read the rest, or pass "
            f"`out_path` to save the whole document to a file.]"
        )
    return out


@tool
def measure_text(text: str) -> str:
    """Count words and characters and estimate page length.

    Use this before declaring any section complete. Language models cannot
    count reliably by inspection, and an over-length narrative is one of the
    most common causes of a proposal being rejected without review.

    Args:
        text: The exact section text to measure. Pass the real content, not a
            summary of it.
    """
    words = len(re.findall(r"\S+", text))
    chars = len(text)
    single = words / WORDS_PER_PAGE_SINGLE
    double = words / WORDS_PER_PAGE_DOUBLE
    return (
        f"words={words} characters={chars} "
        f"est_pages_single_spaced={single:.2f} est_pages_double_spaced={double:.2f} "
        f"(page estimates assume ~{WORDS_PER_PAGE_SINGLE}/~{WORDS_PER_PAGE_DOUBLE} "
        f"words per page; confirm against the funder's formatting rules)"
    )


def build_search_tool(enabled: bool = True):
    """Return a Tavily web-search tool, or ``None`` if search is unavailable.

    Returns ``None`` when ``enabled`` is False (what ``--no-search`` requests) or
    when no key is configured. Returning ``None`` rather than raising lets the
    agent still run for drafting and compliance work without search.
    """
    if not enabled:
        return None
    if not os.getenv("TAVILY_API_KEY"):
        return None
    try:
        from langchain_tavily import TavilySearch
    except ImportError:  # pragma: no cover - dependency is declared
        return None
    return TavilySearch(max_results=6, topic="general")
