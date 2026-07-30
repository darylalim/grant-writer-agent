"""Custom tools.

Deliberately small. The agent already gets ``ls``/``read_file``/``write_file``/
``edit_file``/``glob``/``grep`` from the filesystem middleware, so these only
cover what the harness cannot do: read a PDF solicitation, count words
reliably, search the web, and query the grants.gov opportunity index.

The grants.gov pair is separate from web search rather than folded into it
because the two return different *kinds* of thing. Search returns prose a model
has to believe; grants.gov returns stated fields -- eligible applicant types,
award ceiling, close date -- which are exactly what a fit judgement rests on.
A score citing `applicantEligibilityDesc` is citing the funder; a score citing
a search snippet is citing whoever wrote the page.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import httpx
from langchain.tools import tool

# Rough conversions used for page-limit estimates. Funders specify limits in
# pages but drafts exist as plain text, so these are the bridge -- always
# approximate, never a substitute for the real typeset PDF.
WORDS_PER_PAGE_SINGLE = 500
WORDS_PER_PAGE_DOUBLE = 250

# What each real-disk writer may reach, per tool rather than per project.
#
# These are deliberately narrower than `config.CONTENT_DIRS`, which is the union
# the *permission rules* legitimately cover. A tool that writes past the backend
# gets only the directories its own graph has business in: `extract_pdf_text`
# belongs to drafting and archives a solicitation beside the drafts;
# `fetch_grants_gov_opportunity` belongs to discovery and has no reason to touch
# an application at all -- least of all `final/`, where a write would bypass the
# approval interrupt entirely. See `_resolve_output_path`.
_DRAFTING_WRITE_DIRS: tuple[str, ...] = ("applications", "memories")
_DISCOVERY_WRITE_DIRS: tuple[str, ...] = ("opportunities",)


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


def _resolve_output_path(out_path: str, allowed_dirs: tuple[str, ...]) -> Path:
    """Map a virtual write path onto disk, refusing anything outside `allowed_dirs`.

    The tools below write to the real filesystem rather than through the
    backend, so they do not inherit the FilesystemPermission rules and have to
    enforce the same boundary themselves (invariant 2). Every such writer routes
    through this one function; a tool that joined a path itself would be the
    variant CLAUDE.md warns against.

    **`allowed_dirs` is required, and it is not `CONTENT_DIRS`.** That is the
    correction: this used to apply the union of every content directory to every
    caller, which meant it had no idea which graph was calling. The discovery
    orchestrator carries `fetch_grants_gov_opportunity`, and its own permission
    rules deny `/applications/**` -- but those rules govern the *backend*, and a
    tool writing to real disk never consults them. So a single
    ``out_path="/applications/x/final/proposal.md"`` overwrote a submission-bound
    file, with no approval interrupt and no error, straight through the boundary
    the whole approval mechanism rests on.

    A union cannot express "this tool belongs to that graph", so the caller
    states its own set. `CONTENT_DIRS` remains the union `build_permissions`
    needs -- the permission rules really do cover all three -- but no writer
    should inherit the union just because it exists.
    """
    from grant_writer.config import PROJECT_ROOT

    root = PROJECT_ROOT.resolve()
    allowed = tuple(root / name for name in allowed_dirs)

    # Resolve BEFORE checking. Validating the raw string first lets
    # "/applications/../src/agent.py" pass the prefix test and then escape when
    # `..` collapses -- the check has to run on the final path, not the input.
    virtual = "/" + out_path.strip().lstrip("/")
    resolved = (root / virtual.lstrip("/")).resolve()

    if not any(resolved.is_relative_to(base) for base in allowed):
        # Names the caller's own set, not the union. A tool that may only write
        # to /opportunities/ must say so when it refuses, or the message invites
        # a retry into a directory it will refuse again.
        listed = " or ".join(f"/{name}/" for name in allowed_dirs)
        msg = f"refusing to write outside {listed}: {out_path!r}"
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
            target = _resolve_output_path(out_path, _DRAFTING_WRITE_DIRS)
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


_GRANTS_GOV_BASE = "https://api.grants.gov/v1/api"

# Read is the long one: a synopsis can be tens of kilobytes. Connect stays
# short so an unreachable host fails fast instead of stalling a delegated turn
# that the recursion limit is counting steps against.
_GRANTS_GOV_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)

# Enough to triage from, not enough to bury the model. A bare keyword can match
# hundreds of opportunities, and a scout that reads 200 one-line summaries has
# spent its context before it fetches a single synopsis.
_GRANTS_GOV_MAX_ROWS = 50


def _grants_gov_post(path: str, payload: dict) -> dict | str:
    """POST to one grants.gov endpoint. Returns its ``data`` object, or an
    ``"Error: ..."`` string.

    Both endpoints are public and keyless -- there is no header to set and no
    credential to leave out -- but both wrap a *logical* failure (an unknown
    opportunity id, a malformed filter) in an HTTP 200 carrying a non-zero
    ``errorcode``. So ``raise_for_status`` alone reports success on a response
    that contains none, which is why that check is here and not at each call
    site: two tools, one place that knows this.

    Never raises. A tool that raises reaches the model as a framework-level
    error it cannot act on; a returned string is something it can read and
    respond to by narrowing the search or trying the web instead.
    """
    try:
        response = httpx.post(
            f"{_GRANTS_GOV_BASE}/{path}", json=payload, timeout=_GRANTS_GOV_TIMEOUT
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return f"Error: grants.gov request failed: {exc}"

    try:
        body = response.json()
    except ValueError:
        return "Error: grants.gov returned a response that was not JSON."

    # Valid JSON is not necessarily an object. A captive portal or an
    # intercepting proxy answers 200 with `[]` or a bare string, which clears
    # `raise_for_status` and `response.json()` alike -- and `body.get(...)`
    # then raises AttributeError straight past this function's "never raises"
    # contract, killing the sweep instead of telling the model to try the web.
    if not isinstance(body, dict):
        return "Error: grants.gov returned JSON that was not an object."

    if body.get("errorcode"):
        return (
            f"Error: grants.gov reported {body.get('msg') or 'an unspecified error'}."
        )

    data = body.get("data")
    if not isinstance(data, dict):
        return "Error: grants.gov response carried no data object."

    # A *second* place failures hide, and the one that matters most here.
    # Verified against the live endpoint: an opportunity id that does not exist
    # comes back HTTP 200 with `errorcode: 0` and `msg: "Webservice Succeeds"`,
    # and reports the actual failure only in `data.errorMessages`. Every field
    # is then absent, so without this check the formatter below renders a
    # complete, plausible, entirely empty document -- the scout archives it as
    # a candidate and scores it, and an opportunity that does not exist reaches
    # the shortlist with citations quoting nothing. That is the fabrication this
    # feature exists to prevent, arriving by the one route where neither the
    # model nor the prompt is at fault.
    if messages := data.get("errorMessages"):
        joined = "; ".join(str(message) for message in messages)
        return f"Error: grants.gov reported {joined}"
    return data


def _format_search_hits(data: dict) -> str:
    """Render a ``search2`` payload as one line per opportunity.

    Split from the tool so it can be tested against a captured response with no
    network access -- the suite is offline by design, and a field renamed
    upstream would otherwise show up only as `?` in a real run.

    Field names are the ones `search2` actually returns, which are **not** the
    ones the detail endpoint returns: the agency is `agency` here and
    `synopsis.agencyName` there. Reading the detail spelling off a search hit
    is a silent `?` on every row, not an error.
    """
    hits = data.get("oppHits") or []
    if not hits:
        return (
            "No grants.gov opportunities matched. Try broader keywords, or "
            "clear the agency filter."
        )

    total = data.get("hitCount", len(hits))
    lines = [f"[{total} match(es), showing {len(hits)}]"]
    for hit in hits:
        lines.append(
            f"- id={hit.get('id', '?')} | {hit.get('number', '?')} | "
            f"{hit.get('title') or '(untitled)'} | "
            f"{hit.get('agency') or hit.get('agencyCode') or '?'} | "
            f"closes {hit.get('closeDate') or 'not stated'} | "
            f"{hit.get('oppStatus', '?')}"
        )
    if total > len(hits):
        lines.append(
            f"[{total - len(hits)} further match(es) not shown. Narrow the keyword "
            f"rather than raising `rows` -- a longer list is not a better shortlist.]"
        )
    return "\n".join(lines)


def _format_opportunity(data: dict) -> str:
    """Render a ``fetchOpportunity`` payload as the text a scout scores from.

    Ordered so the fields a fit judgement rests on come first and the long
    prose last: eligibility, money, and the deadline are what decide whether an
    opportunity is worth reading about at all.
    """
    synopsis = data.get("synopsis") or {}
    agency = synopsis.get("agencyName") or data.get("owningAgencyCode", "?")
    applicants = ", ".join(
        entry.get("description", "")
        for entry in synopsis.get("applicantTypes") or []
        if entry.get("description")
    )

    # `*Formatted` carries thousands separators but no currency symbol
    # ("3,500,000"), so the unit is stated here rather than left for the scout
    # to supply. Every grants.gov award is USD, but a citation that reads
    # "$100,000" against a source that never said "$" is a figure the scout
    # invented -- small, and exactly the kind of invention the rest of this
    # system refuses to make.
    def amount(*keys: str) -> str | None:
        """First key that carries a value, as text.

        Emptiness is tested against None and "" rather than by truthiness,
        because a floor of 0 is a stated fact -- grants.gov reports one
        routinely -- and `or` reads it as absent. That dropped the *whole*
        range, ceiling included, so a $3.5M opportunity archived as "not
        stated" and the scout scored `award-size-fit` against a document that
        had withheld the number the funder actually published.
        """
        for key in keys:
            value = synopsis.get(key)
            if value is not None and value != "":
                return str(value)
        return None

    ceiling = amount("awardCeilingFormatted", "awardCeiling")
    floor = amount("awardFloorFormatted", "awardFloor")
    if floor and ceiling:
        award_range = f"USD {floor} to USD {ceiling}"
    elif ceiling:
        award_range = f"up to USD {ceiling}"
    elif floor:
        award_range = f"from USD {floor}"
    else:
        award_range = "not stated"

    # Rendered as words, never as the raw value. `costSharing` is absent on
    # plenty of listings, and `{None}` formats to the literal "None" -- which
    # reads as "no cost sharing required", an affirmative claim the funder
    # never made, in the one file the gating eligibility criterion is scored
    # from. Every neighbouring line already says "not stated"; this was the
    # only one that did not.
    sharing = synopsis.get("costSharing")
    cost_sharing = "not stated" if sharing is None else ("yes" if sharing else "no")

    return "\n".join(
        [
            f"# {data.get('opportunityTitle') or '(untitled)'}",
            "",
            f"- Number: {data.get('opportunityNumber', '?')}",
            f"- Agency: {agency}",
            f"- Close date: {synopsis.get('responseDate') or 'not stated'}",
            f"- Award range: {award_range}",
            f"- Expected awards: {synopsis.get('numberOfAwards') or 'not stated'}",
            f"- Cost sharing required: {cost_sharing}",
            f"- Eligible applicant types: {applicants or 'not stated'}",
            f"- Solicitation: {synopsis.get('fundingDescLinkUrl') or 'not stated'}",
            "",
            "## Eligibility",
            synopsis.get("applicantEligibilityDesc") or "(none stated)",
            "",
            "## Synopsis",
            synopsis.get("synopsisDesc") or "(none stated)",
        ]
    )


@tool
def search_grants_gov(
    keyword: str = "",
    agencies: str = "",
    opp_statuses: str = "forecasted|posted",
    rows: int = 25,
) -> str:
    """Search grants.gov for federal funding opportunities. No API key needed.

    This is the sweep that finds candidates worth scoring. It returns one line
    per opportunity; read the promising ones in full with
    `fetch_grants_gov_opportunity` before you score or quote any of them.

    Covers US federal opportunities only. Private foundations, state agencies,
    and non-US funders are not in this index -- use web search for those.

    Args:
        keyword: Free text, built from what the organization actually does.
            Start broad ("rural health education"), then narrow once you have
            seen what comes back. The filters alone are rarely selective enough
            to be useful.
        agencies: Pipe-separated agency codes, e.g. "USDA|NSF". Empty searches
            every agency, and the `agency` on each result teaches you the codes
            worth filtering on next time.
        opp_statuses: Pipe-separated status filter. The default excludes closed
            and archived listings, which cannot be applied for.
        rows: How many results to return. Capped at 50 whatever you pass -- if
            the sweep is too broad, narrow the keyword rather than reading more
            of a list that is mostly noise.

    Returns:
        One line per opportunity: the numeric `id` to pass to
        `fetch_grants_gov_opportunity`, the public opportunity number, title,
        agency, close date, and status. Starts with "Error: " on failure.
    """
    payload = {
        "rows": max(1, min(rows, _GRANTS_GOV_MAX_ROWS)),
        "keyword": keyword,
        "agencies": agencies,
        "oppStatuses": opp_statuses,
    }
    data = _grants_gov_post("search2", payload)
    if isinstance(data, str):
        return data
    return _format_search_hits(data)


@tool
def fetch_grants_gov_opportunity(
    opportunity_id: str, max_chars: int = 12000, out_path: str = ""
) -> str:
    """Read one grants.gov opportunity in full: eligibility, award size,
    deadline, and synopsis. No API key needed.

    Args:
        opportunity_id: The numeric `id` from a `search_grants_gov` result --
            NOT the public opportunity number like "PD-24-1340". They are
            different fields and only the numeric one works here.
        max_chars: Truncation limit when the text comes back to you. Ignored
            when `out_path` is set.
        out_path: If set (e.g. "/opportunities/rural-2026/candidates/353201.md"),
            the COMPLETE text is written straight to that file and you get back
            a short receipt. Use this for anything you intend to score. A
            citation has to quote the real document, and text relayed through
            your own context gets silently abridged -- the same hazard
            `extract_pdf_text` warns about for a solicitation PDF.

    Returns:
        A write receipt when `out_path` is set, otherwise the formatted text
        with an explicit notice if it was truncated. Starts with "Error: " on
        failure.
    """
    identifier = str(opportunity_id).strip()
    # `isdecimal`, not `isdigit`: the latter admits superscripts and other
    # non-decimal Unicode digits ("²".isdigit() is True) that `int()` then
    # rejects, so the guard passed and the conversion below raised -- handing
    # the model a framework-level traceback instead of the message this branch
    # exists to give it.
    if not identifier.isdecimal():
        return (
            f"Error: opportunity_id must be the numeric id from a search result, "
            f"got {opportunity_id!r}. The public number (e.g. 'PD-24-1340') is a "
            f"different field and will not resolve."
        )

    data = _grants_gov_post("fetchOpportunity", {"opportunityId": int(identifier)})
    if isinstance(data, str):
        return data

    out = _format_opportunity(data)

    if out_path:
        try:
            target = _resolve_output_path(out_path, _DISCOVERY_WRITE_DIRS)
        except ValueError as exc:
            return f"Error: {exc}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(out, encoding="utf-8")
        return (
            f"Wrote {len(out)} characters to {out_path}. Read it back with "
            f"read_file before scoring or quoting it -- do not cite from memory."
        )

    if len(out) > max_chars:
        remaining = len(out) - max_chars
        return (
            f"{out[:max_chars]}\n\n[TRUNCATED: {remaining} more characters. "
            f"Pass `out_path` to save the whole document to a file.]"
        )
    return out


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
