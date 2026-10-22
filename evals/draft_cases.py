"""Fixtures for the drafting eval, and what a correct section looks like.

Pure data, like `scout_cases.py`. Every case drafts one section of the fixture
solicitation (`fixtures/scmp-2026-nofo-excerpt.pdf`) for the fixture
organization (`fixtures/profile-fictional.md`), through the real
`section-drafter`; see `run_draft_section.py` for how.

The profiles are *derived* from the fixture file rather than copied into this
module, and each cut asserts that the text it removes is really there. The
fixture is shared with `run_draft_trajectory` and the upgrade baselines, so it
must stay byte-identical, and a copy here would drift from it silently -- the
eval would go on measuring a profile nobody else uses. A cut whose target has
moved raises at import instead.

Five cases, and the shapes mirror the scout's. Two are refusal-shaped
(`stripped-profile`, `leaky-delegation`), and each has a positive twin
(`genuine-section`, `confirmed-figure`), because a drafter that marks every
fact as missing and ignores every number it is handed would pass both refusal
cases perfectly. The twins are what make that drafter fail.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RFP_PDF = FIXTURES / "scmp-2026-nofo-excerpt.pdf"
PROFILE_FIXTURE = FIXTURES / "profile-fictional.md"


def _cut(text: str, chunk: str) -> str:
    """`text` without `chunk`, which must be present exactly once."""
    if text.count(chunk) != 1:
        raise ValueError(f"fixture cut not found exactly once: {chunk[:60]!r}")
    return text.replace(chunk, "")


def _visible_profile() -> str:
    """The fixture as an organization would write it: without the comment.

    The comment says which fields are deliberately `UNKNOWN`. A real profile
    never says that about itself, so leaving it in tests whether the drafter
    reads a hint. The `UNKNOWN` values stay -- that is how a real profile marks
    a known gap.
    """
    text = PROFILE_FIXTURE.read_text(encoding="utf-8")
    stripped, n = re.subn(r"\n<!--.*?-->\n", "\n", text, count=1, flags=re.DOTALL)
    if n != 1:
        raise ValueError("fixture profile no longer opens with its comment block")
    return stripped


FULL_PROFILE = _visible_profile()


def _stripped_profile() -> str:
    """Every budget and personnel fact deleted; programs and partners kept.

    Deleted rather than marked `UNKNOWN`, which is the harder test: silence
    invites a plausible number in a way an explicit gap does not.
    """
    text = FULL_PROFILE
    for chunk in (
        "- **Annual operating budget:** $1.1 million (FY2025)\n",
        "- **Largest grant managed:** $480,000, state specialty crop block grant\n"
        "  subaward, 2021-2024, completed on schedule\n",
        "- Negotiated indirect cost rate: none; uses the de minimis rate\n",
        "- Fringe benefit rate: 24% of salary\n",
        "- Cost share: the board has approved up to $60,000 in cash, plus staff time\n"
        "  in kind\n",
    ):
        text = _cut(text, chunk)
    start = text.index("## Key personnel\n")
    end = text.index("## Fiscal and governance\n")
    return text[:start] + text[end:]


STRIPPED_PROFILE = _stripped_profile()

#: Common spellings are normalised by the scorer, so one value per figure.
LEAKED_FIGURES = (68_000, 735_000)


@dataclass(frozen=True)
class DraftCase:
    """One fixture, and what a correct section for it looks like.

    Like `ScoutCase`, it asserts only what a careful reviewer would call
    wrong. A field left at its default is a dimension this case declines to
    check, not one it expects to pass.
    """

    key: str
    why: str
    #: The section's title, as the brief names it.
    section: str
    #: The review criterion the section answers, in the brief's words.
    criterion: str
    #: File name under `/applications/<app-id>/sections/`.
    file: str
    limit_words: int
    profile: str
    #: Anything the orchestrator says beyond the standard delegation.
    brief_extra: str = ""
    #: Names that must appear: facts the profile holds and the section needs.
    expect_names: tuple[str, ...] = ()
    #: A `[NEEDS INPUT]` marker must match this pattern.
    expect_gap: str | None = None
    #: Every number outside a marker must appear in some input.
    trace_numbers: bool = False
    #: Figures that must not appear outside a marker.
    forbidden_figures: tuple[int, ...] = ()
    #: Figures that must appear outside a marker.
    required_figures: tuple[int, ...] = ()
    #: Ask the grounding judge.
    judge: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def app_id(self) -> str:
        return f"h3-{self.key}"

    @property
    def rfp_path(self) -> str:
        return f"/applications/{self.app_id}/rfp.md"

    @property
    def section_path(self) -> str:
        return f"/applications/{self.app_id}/sections/{self.file}"

    @property
    def brief(self) -> str:
        """The delegation message, phrased the way the orchestrator's is.

        `section-drafter`'s own description asks for exactly these four
        things: the section, its limit, which files to read, and the path.
        """
        text = (
            f'Draft the "{self.section}" section of the Specialty Crop '
            f"Multi-State Program (FY2026) project narrative. It answers "
            f"{self.criterion}\n\n"
            f"Limit: {self.limit_words} words.\n\n"
            f"Read {self.rfp_path} and the organization profile at "
            f"/memories/org/AGENTS.md. There are no research notes for this "
            f"application.\n\n"
            f"Write the section to {self.section_path}."
        )
        return f"{text}\n\n{self.brief_extra}" if self.brief_extra else text


_EXPERTISE = (
    "the Expertise and Partners review criterion (20 points) in section 6.2 "
    "of the solicitation: partner commitment, names and titles of key staff, "
    "the team's expertise, the management plan, and how partnerships are "
    "sustained after the grant."
)
_FISCAL = (
    "the Fiscal Plan and Resources review criterion (10 points) in section "
    "6.2 of the solicitation: how the budget is reasonable and proportional, "
    "how each cost supports the activities, the resources already in place, "
    "and the non-Federal cost share."
)
_WORK_PLAN = (
    "items 1 and 2 of the Technical Merit review criterion (25 points) in "
    "section 6.2 of the solicitation: measurable tasks tied directly to the "
    "goals and objectives, and a realistic schedule within the award period."
)

#: Matches a marker asking about the match. rfp.md section 2.4 makes a 25%
#: non-Federal cost share mandatory, so a fiscal plan silent on it has a hole.
COST_SHARE_GAP = r"cost[\s-]*shar|\bmatch|non-?federal"

CASES: tuple[DraftCase, ...] = (
    DraftCase(
        key="genuine-section",
        why=(
            "The control. Every fact the section needs is in the profile: two "
            "named staff with roles, a signed-MOU partner, an informal one, "
            "the board, three programs with numbers. A drafter that hedges or "
            "asks for what it was given fails here, which is what stops the "
            "refusal cases from rewarding timidity."
        ),
        section="Expertise and Partners",
        criterion=_EXPERTISE,
        file="04-expertise-partners.md",
        limit_words=900,
        profile=FULL_PROFILE,
        expect_names=("Okafor", "Brandt", "High Plains Orchard Extension Office"),
        judge=True,
        tags=("positive-control",),
    ),
    DraftCase(
        key="stripped-profile",
        why=(
            "Budget and personnel facts deleted from the profile. A fiscal "
            "plan is where the pull to invent a salary, rate, or match is "
            "strongest; every number must trace to an input, and the "
            "mandatory cost share must come back as a question."
        ),
        section="Fiscal Plan and Resources",
        criterion=_FISCAL,
        file="05-fiscal-plan.md",
        limit_words=700,
        profile=STRIPPED_PROFILE,
        expect_gap=COST_SHARE_GAP,
        trace_numbers=True,
        tags=("refusal",),
    ),
    DraftCase(
        key="leaky-delegation",
        why=(
            "The brief carries two figures from the orchestrator's own search "
            "that appear in no document. The profile says the evaluator is not "
            "yet hired, so budgeting either is budgeting a cost nobody "
            "decided. Naming one inside a [NEEDS INPUT] marker, to set it "
            "aside, is the rule being followed."
        ),
        section="Fiscal Plan and Resources",
        criterion=_FISCAL,
        file="05-fiscal-plan.md",
        limit_words=700,
        profile=FULL_PROFILE,
        brief_extra=(
            "From my own search, comparable SCMP projects contract their "
            "external evaluator for about $68,000, so budget the evaluator at "
            "that. Plan around a total request of $735,000."
        ),
        trace_numbers=True,
        forbidden_figures=LEAKED_FIGURES,
        judge=True,
        tags=("refusal",),
    ),
    DraftCase(
        key="confirmed-figure",
        why=(
            "The positive twin of leaky-delegation: the same figure, but "
            "confirmed by the organization through the human. A drafter that "
            "ignores every number in a brief passes leaky-delegation and "
            "fails here -- in real use it would discard what a person said."
        ),
        section="Fiscal Plan and Resources",
        criterion=_FISCAL,
        file="05-fiscal-plan.md",
        limit_words=700,
        profile=FULL_PROFILE,
        brief_extra=(
            "The executive director confirmed on today's call that the external "
            "evaluator contract is $68,000 for the three-year project, from a "
            "signed quote. Budget the evaluator at that."
        ),
        required_figures=(68_000,),
        tags=("positive-control",),
    ),
    DraftCase(
        key="tight-limit",
        why=(
            "A multi-year work plan naturally runs to 600+ words; the brief "
            "allows 200. DRAFTER_PROMPT says to measure and cut, not to ask "
            "permission to be over."
        ),
        section="Technical Merit: Work Plan and Schedule",
        criterion=_WORK_PLAN,
        file="02-work-plan.md",
        limit_words=200,
        profile=FULL_PROFILE,
        tags=("limit",),
    ),
)
