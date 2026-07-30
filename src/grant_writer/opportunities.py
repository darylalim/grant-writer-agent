"""Fit scoring: the rubric, the grammar a scout writes, and the parser that
turns it back into a ranked shortlist.

Pure. No filesystem, no network, no model calls -- `workspace.py` owns the
reading side. That split is what makes the scoring itself testable offline,
which matters more here than anywhere else in the package: a shortlist is read
once, acted on, and never audited, so a scorer that is quietly wrong is a
funder never applied to.

## Why the model never writes a number

The scout picks one of four *words* per criterion. The weights and the
arithmetic live here, and `SCOUT_PROMPT` never shows them -- so the model has
nothing to add up even if it tried. That is a stronger guarantee than
`COMPLIANCE_PROMPT`'s "write the arithmetic out so it is checkable", which
still relies on a human checking it. Here a fabricated total is not caught,
it is unrepresentable: there is nowhere in the grammar to put one.

## Why the parser never raises

It is read by a frontend that must keep rendering a ranked list even when one
file in it does not conform -- the same standing rule `workspace.py`'s readers
follow for a vanished file or an unparseable compliance report. Every
ambiguity resolves to a value plus an entry in `warnings`.

The direction of that tolerance is deliberate and is the same one
`workspace.count_gaps` argues: under-reporting is the dangerous direction. A
verdict with a missing citation still scores, flagged -- because silently
zeroing it would bury a real opportunity in a ranked list nobody re-checks.
Only an *unrecognized verdict word* scores zero, because the alternative is
guessing what the model meant, which is the one thing this module exists to
prevent.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

# The four words a scout may write, best first. One vocabulary across every
# criterion rather than a bespoke set per criterion: the scout has to hold
# these in mind for all six, and a per-criterion vocabulary is one more thing
# for it to get subtly wrong in a format that is then parsed literally.
VERDICTS: tuple[str, ...] = ("STRONG", "MODERATE", "WEAK", "NONE")

# Verdict -> the fraction of a criterion's weight it earns, as a ratio so the
# points stay integers. Floats here would make a total depend on binary
# rounding, and "76" is a number a human compares between two scans.
_VERDICT_FRACTION: dict[str, tuple[int, int]] = {
    "STRONG": (1, 1),
    "MODERATE": (3, 5),
    "WEAK": (1, 5),
    "NONE": (0, 1),
}

# The citation sources a scout may cite. Anything else is kept verbatim and
# flagged -- see `_parse_citations`.
CITATION_SOURCES: tuple[str, ...] = ("opportunity", "org profile")


@dataclass(frozen=True)
class Criterion:
    """One row of the rubric.

    `key` is what appears as a `## <key>` heading in the file, so it is
    lowercase and hyphenated -- a heading the model has to reproduce exactly is
    easier to get right when it has no spaces or capitals to remember.
    """

    key: str
    label: str
    weight: int
    question: str
    gating: bool = False

    def points_for(self, verdict: str | None) -> int:
        numerator, denominator = _VERDICT_FRACTION.get(verdict or "", (0, 1))
        return self.weight * numerator // denominator

    @property
    def max_points(self) -> int:
        return self.weight


# Weights sum to 100, so a score reads as a percentage without any conversion.
#
# The ordering is the order a human triages in: can we even apply, do we want
# to, can we win it, can we deliver it, is it worth the effort, can we make the
# date. Mission alignment outweighs everything else that is not gating, because
# a proposal that does not fit the funder's purpose loses on the first read
# regardless of how well the rest scores.
RUBRIC: tuple[Criterion, ...] = (
    Criterion(
        key="eligibility",
        label="Eligibility",
        weight=20,
        question=(
            "Does the applicant meet every stated eligibility requirement -- "
            "entity type, location, registrations, cost-sharing capacity?"
        ),
        gating=True,
    ),
    Criterion(
        key="mission-alignment",
        label="Mission alignment",
        weight=25,
        question=(
            "Does what this funds match what the organization exists to do, in "
            "the funder's own words rather than a generous reading of them?"
        ),
    ),
    Criterion(
        key="program-fit",
        label="Program fit",
        weight=20,
        question=(
            "Do the organization's current programs and populations served "
            "match what the solicitation asks to be delivered, and to whom?"
        ),
    ),
    Criterion(
        key="track-record",
        label="Track record",
        weight=15,
        question=(
            "Does the organization have the demonstrated history a reviewer "
            "would look for -- comparable awards, outcomes, audit standing?"
        ),
    ),
    Criterion(
        key="award-size-fit",
        label="Award size fit",
        weight=10,
        question=(
            "Is the award range worth pursuing and within what the "
            "organization has managed before?"
        ),
    ),
    Criterion(
        key="timeline-feasibility",
        label="Timeline feasibility",
        weight=10,
        question=(
            "Is there enough time before the deadline to assemble a "
            "competitive submission, including anything that needs a partner?"
        ),
    ),
)

RUBRIC_BY_KEY: dict[str, Criterion] = {c.key: c for c in RUBRIC}
MAX_TOTAL_POINTS: int = sum(c.max_points for c in RUBRIC)

_TITLE_RE = re.compile(r"^#\s+Opportunity:\s*(?P<title>.+?)\s*$", re.MULTILINE)
_HEADER_FIELD_RE = re.compile(
    r"^[-*]\s*(?P<label>[A-Za-z][\w -]*?)\s*:\s*(?P<value>.+?)\s*$", re.MULTILINE
)
_SECTION_RE = re.compile(r"^##\s+(?P<key>\S+)\s*$", re.MULTILINE)
_VERDICT_RE = re.compile(
    r"^[-*]\s*Verdict\s*:\s*(?P<verdict>\S+)\s*$", re.MULTILINE | re.IGNORECASE
)
# The citation text is captured between the first and last double quote on the
# line rather than by a non-greedy match, so an inner quote in the funder's own
# wording does not truncate the citation at it.
_CITATION_RE = re.compile(
    r"^[-*]\s*Citation\s*\((?P<source>[^)]*)\)\s*:\s*\"(?P<text>.*)\"\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_NOTE_RE = re.compile(r"^[-*]\s*Note\s*:\s*(?P<note>.*?)\s*$", re.MULTILINE)

# The marker the rest of the system already uses for a fact nobody may invent.
# Matching `workspace.count_gaps`'s substring exactly is what lets a scan
# directory be counted by the same function an application directory is.
GAP_MARKER = "[NEEDS INPUT"


@dataclass(frozen=True)
class Citation:
    """One quoted piece of evidence behind a verdict."""

    source: str
    text: str

    @property
    def is_gap(self) -> bool:
        return self.text.startswith(GAP_MARKER)


@dataclass(frozen=True)
class ScoredCriterion:
    """One criterion as the scout answered it, with the points that follow."""

    key: str
    label: str
    verdict: str | None
    points: int
    max_points: int
    citations: tuple[Citation, ...]
    note: str

    @property
    def is_scored(self) -> bool:
        """Whether a recognized verdict was found at all.

        Distinct from `points == 0`: an honest NONE is a judgement, a missing
        section is an absence, and a shortlist that shows them identically
        invites acting on the second as though it were the first.
        """
        return self.verdict in _VERDICT_FRACTION


@dataclass(frozen=True)
class ScoredOpportunity:
    """One candidate, parsed. `criteria` is always the full rubric, in order."""

    key: str
    title: str
    fields: dict[str, str]
    criteria: tuple[ScoredCriterion, ...]
    warnings: tuple[str, ...]

    @property
    def total_points(self) -> int:
        return sum(c.points for c in self.criteria)

    @property
    def scored_count(self) -> int:
        return sum(1 for c in self.criteria if c.is_scored)

    @property
    def display_title(self) -> str:
        """What to call this candidate on screen.

        A scout that omitted the title heading still produced a scored file,
        and a blank row is harder to act on than one labelled by its key.
        """
        return self.title or self.key

    @property
    def score_label(self) -> str:
        """The score as both frontends print it.

        Here rather than at each call site because it encodes the `None`
        distinction `fit_percent` documents -- and a second copy of that
        decision is exactly how "unscored" starts rendering as "0%" in one
        frontend and not the other.
        """
        if self.fit_percent is None:
            return "unscored"
        return f"{self.fit_percent:.0f}%"

    @property
    def fit_percent(self) -> float | None:
        """The score as a percentage, or None when nothing could be scored.

        `None` rather than `0.0`, and the distinction is the point: "we could
        not read this" and "this is a bad fit" are opposite instructions to the
        person reading a shortlist, and collapsing the first into the second
        buries a candidate that may simply have been written in the wrong
        format. Both frontends render None as "unscored", never as 0%.
        """
        if not self.scored_count:
            return None
        return round(100.0 * self.total_points / MAX_TOTAL_POINTS, 1)

    @property
    def disqualified(self) -> bool:
        """Whether a gating criterion was answered NONE.

        Deliberately independent of `total_points`, which stays the honest sum
        either way. A reader who sees "62/100, ineligible" can go and check a
        surprising call; one who sees a zeroed score has been told only that
        the system disagreed with them.
        """
        return any(
            c.verdict == "NONE" and RUBRIC_BY_KEY[c.key].gating
            for c in self.criteria
            if c.key in RUBRIC_BY_KEY
        )

    @property
    def gaps(self) -> tuple[Citation, ...]:
        """Every `[NEEDS INPUT: ...]` citation, across all criteria."""
        return tuple(
            citation
            for criterion in self.criteria
            for citation in criterion.citations
            if citation.is_gap
        )


def _parse_citations(
    section: str, key: str, warnings: list[str]
) -> tuple[Citation, ...]:
    citations = []
    for match in _CITATION_RE.finditer(section):
        source = match.group("source").strip().lower()
        if source not in CITATION_SOURCES:
            # Kept, not dropped. The quote is still evidence; only the label on
            # it is unexpected, and discarding real evidence over a label is the
            # under-reporting this module's docstring rules out.
            warnings.append(f"{key}: unrecognized citation source {source!r}")
        citations.append(Citation(source=source, text=match.group("text")))
    return tuple(citations)


def _parse_criterion(
    criterion: Criterion, section: str | None, warnings: list[str]
) -> ScoredCriterion:
    if section is None:
        warnings.append(f"{criterion.key}: no section found; scored 0")
        return ScoredCriterion(
            key=criterion.key,
            label=criterion.label,
            verdict=None,
            points=0,
            max_points=criterion.max_points,
            citations=(),
            note="",
        )

    verdicts = _VERDICT_RE.findall(section)
    if len(verdicts) > 1:
        # Last wins, the same "freshest wins" rule `workspace.compliance_verdict`
        # applies within a report -- a scout that revised itself mid-file meant
        # the second one.
        warnings.append(
            f"{criterion.key}: {len(verdicts)} Verdict lines; used the last"
        )

    verdict: str | None = None
    if verdicts:
        raw = verdicts[-1].strip().upper().strip(".,;:")
        if raw in _VERDICT_FRACTION:
            verdict = raw
        else:
            warnings.append(
                f"{criterion.key}: unrecognized verdict {verdicts[-1]!r}; scored 0"
            )
    else:
        warnings.append(f"{criterion.key}: no Verdict line found; scored 0")

    citations = _parse_citations(section, criterion.key, warnings)
    if verdict is not None and not citations:
        # Scored anyway, flagged. See the module docstring on why the tolerance
        # runs this direction.
        warnings.append(f"{criterion.key}: verdict given with no citation")

    note_match = _NOTE_RE.search(section)
    return ScoredCriterion(
        key=criterion.key,
        label=criterion.label,
        verdict=verdict,
        points=criterion.points_for(verdict),
        max_points=criterion.max_points,
        citations=citations,
        note=note_match.group("note") if note_match else "",
    )


def parse_scored_markdown(text: str, *, key: str = "") -> ScoredOpportunity:
    """Parse one scored-opportunity file. Never raises.

    `key` is the caller's identifier for this candidate -- in practice the
    filename stem. It wins over anything in the body: a scout that garbles its
    own `- id:` line would otherwise misfile the candidate under a label the
    frontend cannot match back to a file, and the filename is the one thing
    that is true by construction.
    """
    warnings: list[str] = []

    title_match = _TITLE_RE.search(text)
    title = title_match.group("title") if title_match else ""
    if not title:
        warnings.append("no `# Opportunity: <title>` heading found")

    # Header fields are the bullets before the first `##`. Bullets inside a
    # criterion section are that criterion's, not the header's.
    first_section = _SECTION_RE.search(text)
    header = text[: first_section.start()] if first_section else text
    fields = {
        match.group("label").strip().lower(): match.group("value")
        for match in _HEADER_FIELD_RE.finditer(header)
    }

    # Slice the body into `## key` -> section text. An unknown heading is
    # ignored rather than warned about: a scout appending "## Notes" is
    # harmless, and warning on it would train the reader to ignore warnings.
    sections: dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group("key").strip().lower()] = text[match.end() : end]

    criteria = tuple(
        _parse_criterion(criterion, sections.get(criterion.key), warnings)
        for criterion in RUBRIC
    )

    return ScoredOpportunity(
        key=key,
        title=title,
        fields=fields,
        criteria=criteria,
        warnings=tuple(warnings),
    )


def rank_opportunities(
    opportunities: Sequence[ScoredOpportunity],
) -> list[ScoredOpportunity]:
    """Best fit first; disqualified and unscorable candidates last.

    Three tiers rather than one sort key, because the two kinds of "not at the
    top" mean different things. A disqualified candidate has been judged; an
    unscorable one has not been read. Both belong below everything that was
    scored, and neither belongs below the other by accident of arithmetic.

    Stable within a tier, so equal scores keep the order they were found in
    rather than reordering between reruns of the same scan.
    """

    def sort_key(opportunity: ScoredOpportunity) -> tuple[int, float]:
        if opportunity.fit_percent is None:
            tier = 2
        elif opportunity.disqualified:
            tier = 1
        else:
            tier = 0
        return tier, -(opportunity.fit_percent or 0.0)

    return sorted(opportunities, key=sort_key)


def rubric_brief() -> str:
    """The rubric as the scout is shown it -- labels, questions, no weights.

    Rendered from `RUBRIC` rather than written out in `prompts.py`, so the
    criteria the prompt asks for and the criteria the parser looks for cannot
    drift into disagreement. That failure is silent in the worst way: the scout
    answers a criterion nobody scores, and the total quietly comes out low.
    """
    lines = []
    for criterion in RUBRIC:
        gate = (
            " (gating: NONE here disqualifies the opportunity)"
            if criterion.gating
            else ""
        )
        lines.append(
            f"- `{criterion.key}` — **{criterion.label}**{gate}\n  {criterion.question}"
        )
    return "\n".join(lines)
