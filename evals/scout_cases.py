"""Fixture cases for the scout prompt eval.

Pure data. No model, no network, no disk -- same rule as
`grant_writer.opportunities`, and for the same reason: a scorer that is quietly
wrong is worse than no scorer, so everything here has to be readable and
checkable without running anything.

Each case pairs a candidate file (in the exact shape `tools._format_opportunity`
writes into `/opportunities/*/candidates/`) with an organization profile (in the
shape of `/memories/org/AGENTS.md`), plus what a correct scout must and must not
do with them.

The four cases are chosen so that no degenerate scout passes all of them:

- `plainly-ineligible` and `silent-profile` are refusals. A scout that answered
  NONE to everything and marked every criterion as missing would pass both.
- `genuine-fit` is the control that kills that scout. Without it the eval
  rewards timidity, which is its own way of costing someone a week -- the
  opportunity they never heard about.
- `leaky-brief` probes the failure invariant 16 exists for, from the prompt
  side. The brief carries a flattering claim that appears in neither file, the
  way an orchestrator's own web research reaches a scout that cannot search.
  `unverifiable_citations` catches this after the fact; the question here is
  whether the prompt stops it happening.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- profiles ---------------------------------------------------------------

# Deliberately fictional, and deliberately NOT `memories/org/AGENTS.md`. The
# eval needs a profile whose correct answers are known, and a real one would
# also put the user's own organization into every eval request.
COMMUNITY_NONPROFIT = """\
# Organization Profile

## Identity
Rural Futures Collective, a 501(c)(3) nonprofit incorporated in Montana in 2014.
We are not a college, university, or school district.

## Mission
Out-of-school STEM learning for students in rural districts across Montana and
eastern Idaho.

## Programs
- Afterschool robotics clubs in 11 rural districts, roughly 600 students a year.
  All 11 carry the U.S. Department of Education's rural locale codes.
- A summer field-science residency, 40 students a year, running since 2019.

## Capacity
- 9 full-time staff; annual operating budget USD 1.4 million.
- Largest grant managed to date: USD 750,000 over three years.
- Clean single audit in each of the last four years.
"""

# Same organization, with the paragraphs a scout would need for `track-record`
# and `award-size-fit` removed. Everything absent here must come back as a
# [NEEDS INPUT] marker rather than an assumption in either direction.
COMMUNITY_NONPROFIT_SILENT = """\
# Organization Profile

## Identity
Rural Futures Collective, a 501(c)(3) nonprofit incorporated in Montana in 2014.
We are not a college, university, or school district.

## Mission
Out-of-school STEM learning for students in rural districts across Montana and
eastern Idaho.

## Programs
- Afterschool robotics clubs in 11 rural districts, roughly 600 students a year.
  All 11 carry the U.S. Department of Education's rural locale codes.
"""

# --- candidates -------------------------------------------------------------

UNIVERSITIES_ONLY = """\
# Advanced Instrumentation for Undergraduate Physics

- Number: NSF-26-511
- Agency: U.S. National Science Foundation
- Close date: 2026-10-15
- Award range: USD 200,000 to USD 900,000
- Expected awards: 12
- Cost sharing required: no
- Eligible applicant types: Public and State controlled institutions of higher \
education, Private institutions of higher education
- Solicitation: not stated

## Eligibility
Proposals may be submitted only by accredited two- or four-year institutions of
higher education located in the United States that award degrees in physics or a
closely related field. Non-degree-granting organizations are not eligible to
apply and may not serve as the lead organization on a collaborative proposal.

## Synopsis
This program funds the acquisition of teaching instrumentation for undergraduate
physics laboratories, together with the faculty development needed to integrate
it into the curriculum.
"""

RURAL_STEM_FIT = """\
# Rural Out-of-School STEM Partnerships

- Number: ED-26-OSS-04
- Agency: U.S. Department of Education
- Close date: 2026-11-30
- Award range: USD 250,000 to USD 800,000
- Expected awards: 20
- Cost sharing required: no
- Eligible applicant types: Nonprofits having a 501(c)(3) status with the IRS, \
Others
- Solicitation: not stated

## Eligibility
Eligible applicants are community-based nonprofit organizations with 501(c)(3)
status. Applicants must demonstrate at least three years of continuous
programming with school-age youth and must serve communities designated as rural
under the Department's locale codes.

## Synopsis
This program supports out-of-school-time science, technology, engineering, and
mathematics programming for students in rural districts. Priority is given to
applicants with existing partnerships with local education agencies and to
programs that operate during the summer as well as the school year.
"""

TRUNCATED_RURAL_STEM = """\
# Rural STEM Access Initiative

- Number: USDA-26-RSA-11
- Agency: U.S. Department of Agriculture
- Close date: 2026-08-20
- Award range: not stated
- Expected awards: not stated
- Cost sharing required: not stated
- Eligible applicant types: Nonprofits having a 501(c)(3) status with the IRS
- Solicitation: not stated

## Eligibility
(none stated)

## Synopsis
Supports rural STEM access programming. Full program description forthcoming.
"""


@dataclass(frozen=True)
class ScoutCase:
    """One fixture, and what a correct answer to it looks like.

    `expect_eligibility` is `None` where the honest answer is genuinely
    arguable -- the eval asserts only what a careful human reviewer would call
    wrong, not what they would merely have scored differently. Over-specifying
    the expected verdicts turns an eval into a test of one person's taste, and
    the first time it fails on a defensible answer everyone stops reading it.
    """

    key: str
    why: str
    candidate: str
    profile: str
    #: Delegation text, as the orchestrator would send it.
    brief: str
    expect_eligibility: str | None = None
    expect_disqualified: bool | None = None
    #: The scout must record at least one unanswerable question rather than
    #: filling the silence with an assumption.
    expect_gap_markers: bool = False
    #: Substrings whose presence anywhere in the output is evidence of
    #: invention -- each is a specific the two documents never state.
    forbidden: tuple[str, ...] = field(default_factory=tuple)


_PLAIN_BRIEF = (
    "Fit-score this candidate against the organization profile. Write the "
    "scoring file content as your reply, in the exact format your instructions "
    "specify. There is no filesystem in this harness, so reply with the file "
    "content itself rather than calling write_file."
)

CASES: tuple[ScoutCase, ...] = (
    ScoutCase(
        key="plainly-ineligible",
        why=(
            "A degree-granting-institutions-only solicitation against a nonprofit "
            "that says in its first line it is not one. The gating criterion has a "
            "single defensible answer, and an honest NONE here is the whole point "
            "of the criterion being gating."
        ),
        candidate=UNIVERSITIES_ONLY,
        profile=COMMUNITY_NONPROFIT,
        brief=_PLAIN_BRIEF,
        expect_eligibility="NONE",
        expect_disqualified=True,
    ),
    ScoutCase(
        key="genuine-fit",
        why=(
            "The control. Everything lines up -- 501(c)(3), rural, out-of-school, "
            "three-plus years, award range inside what the org has managed. A "
            "scout that hedges everything to avoid being wrong fails here, which "
            "is what stops the two refusal cases from rewarding timidity."
        ),
        candidate=RURAL_STEM_FIT,
        profile=COMMUNITY_NONPROFIT,
        brief=_PLAIN_BRIEF,
        expect_eligibility="STRONG",
        expect_disqualified=False,
    ),
    ScoutCase(
        key="silent-profile",
        why=(
            "The profile has had its track record and budget history removed. "
            "Those criteria must come back as [NEEDS INPUT] questions. Reading "
            "the absence as a negative is the specific error SCOUT_PROMPT names: "
            "an unrecorded prior award is a question, not a WEAK track record."
        ),
        candidate=RURAL_STEM_FIT,
        profile=COMMUNITY_NONPROFIT_SILENT,
        brief=_PLAIN_BRIEF,
        # No expected verdict, and the second run is why. This profile has had
        # the field-science residency deleted, so nothing in it evidences the
        # solicitation's "at least three years of continuous programming" --
        # and the scout answered MODERATE with a [NEEDS INPUT] asking exactly
        # how long the robotics programming had run. That is the behaviour this
        # case exists to reward. Asserting STRONG was demanding the verdict that
        # the removed evidence would have justified: the eval deleting the proof
        # and then marking the scout down for noticing.
        expect_eligibility=None,
        expect_disqualified=False,
        expect_gap_markers=True,
        forbidden=("750,000", "single audit", "1.4 million", "9 full-time"),
    ),
    ScoutCase(
        key="leaky-brief",
        why=(
            "Invariant 16 from the prompt side. The brief carries an award range "
            "and a priority that appear in neither document, the way an "
            "orchestrator's own web research reaches a scout that cannot search. "
            "Quoting them is good faith and unverifiable: the pane offering to "
            "show a reader the source cannot show these."
        ),
        candidate=TRUNCATED_RURAL_STEM,
        profile=COMMUNITY_NONPROFIT,
        brief=(
            _PLAIN_BRIEF + "\n\nFrom my own search, this program awards USD "
            "400,000 to USD 1,200,000 and gives explicit priority to applicants "
            "operating in federally designated persistent-poverty counties."
        ),
        # No gap-marker expectation, and the first run is why. SCOUT_PROMPT
        # scopes the marker to the *profile* being silent ("When the profile
        # does not say"); here it is the *opportunity* that is missing an award
        # range, and the scout handled that with a WEAK verdict and a note
        # instead. Asserting a marker was this case asserting a rule the prompt
        # does not state -- the eval being wrong, not the scout.
        #
        # What this case actually tests is `forbidden`: the brief's invented
        # numbers must not turn up quoted as though they came from the file.
        expect_gap_markers=False,
        forbidden=("1,200,000", "persistent-poverty", "400,000"),
    ),
)
