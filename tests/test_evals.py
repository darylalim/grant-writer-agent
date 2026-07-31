"""Offline tests for the eval scorers.

An eval whose scoring is wrong is worse than no eval: it reports a prompt
regression as green, and it does so with the authority of a number. So the
scorers get the same treatment as the rest of the package -- canned scout output
in, `Score` objects out, no model and no network.

These live in `tests/` while the eval itself lives in `evals/` and is never
collected here, because these cost nothing and must run on every push. The
distinction is the credential: `evals/run_scout.py` needs a real key, and
CLAUDE.md is explicit that a test needing one is a bug in the suite.

Each case below is a scout output that is *wrong in one specific way*, checked
against the scorer that has to notice. A scorer is only worth having if it fails
on something, and the cheapest way to be sure of that is to hand it the failure.
"""

from __future__ import annotations

from evals.scorers import (
    build_judge_payload,
    read_judge_verdict,
    score_programmatically,
)
from evals.scout_cases import CASES, ScoutCase

# A well-formed scoring file for the `genuine-fit` fixture, quoting only text
# that is genuinely in the two documents. Every other constant here is a
# mutation of this one.
GOOD = """\
# Opportunity: Rural Out-of-School STEM Partnerships

- Number: ED-26-OSS-04
- Agency: U.S. Department of Education
- Close date: 2026-11-30
- Award range: USD 250,000 to USD 800,000

## eligibility
- Verdict: STRONG
- Citation (opportunity): "Eligible applicants are community-based nonprofit \
organizations with 501(c)(3) status."
- Citation (org profile): "Rural Futures Collective, a 501(c)(3) nonprofit \
incorporated in Montana in 2014."

## mission-alignment
- Verdict: STRONG
- Citation (opportunity): "This program supports out-of-school-time science, \
technology, engineering, and mathematics programming for students in rural \
districts."
- Citation (org profile): "Out-of-school STEM learning for students in rural \
districts across Montana and eastern Idaho."

## program-fit
- Verdict: STRONG
- Citation (org profile): "Afterschool robotics clubs in 11 rural districts, \
roughly 600 students a year."

## track-record
- Verdict: MODERATE
- Citation (org profile): "A summer field-science residency, 40 students a \
year, running since 2019."

## award-size-fit
- Verdict: STRONG
- Citation (org profile): "Largest grant managed to date: USD 750,000 over \
three years."

## timeline-feasibility
- Verdict: MODERATE
- Citation (opportunity): "Close date: 2026-11-30"
"""


def _case(key: str) -> ScoutCase:
    return next(case for case in CASES if case.key == key)


def _score(case: ScoutCase, output: str) -> dict[str, bool]:
    return {s.name: s.passed for s in score_programmatically(case, output)}


def _detail(case: ScoutCase, output: str, name: str) -> str:
    return next(
        s.detail for s in score_programmatically(case, output) if s.name == name
    )


def test_a_correct_answer_passes_every_scorer():
    """The control, and the one that matters most.

    A scorer suite that fails nothing is useless; one that fails *everything*
    is worse, because it looks like the prompt is broken and the prompt is
    where people will go looking. Every mutation case below is only meaningful
    against this baseline.
    """
    assert all(_score(_case("genuine-fit"), GOOD).values())


def test_an_unparseable_file_is_caught_rather_than_scored():
    """Reported as a parse failure, not as a low score.

    Invariant 14's distinction, one level up: a scout whose output stopped
    conforming would otherwise read as a scout that judged everything harshly,
    and the fix for those two is not the same.
    """
    scores = _score(_case("genuine-fit"), "I think this looks like a decent fit!")
    assert scores["parses"] is False


def test_a_wrong_gating_verdict_is_caught():
    hopeful = GOOD.replace(
        "## eligibility\n- Verdict: STRONG", "## eligibility\n- Verdict: STRONG"
    )
    scores = _score(_case("plainly-ineligible"), hopeful)
    assert scores["eligibility"] is False
    assert scores["disqualified"] is False


def test_a_quote_that_is_in_neither_document_is_caught():
    """The `leaky-brief` failure, reproduced by hand.

    The quoted range is in the delegation message and in neither file, which is
    exactly what a scout handed the orchestrator's web research produces.
    """
    leaky = GOOD.replace(
        '- Citation (opportunity): "Close date: 2026-11-30"',
        '- Citation (opportunity): "awards USD 400,000 to USD 1,200,000"',
    )
    scores = _score(_case("genuine-fit"), leaky)
    assert scores["citations"] is False
    assert "1,200,000" in _detail(_case("genuine-fit"), leaky, "citations")


def test_naming_a_leaked_figure_in_order_to_reject_it_is_not_invention():
    """The second eval run's finding, pinned so it cannot come back.

    Handed a brief carrying figures that appear in neither document, the scout
    refused them and said so in its closing report. A substring search over the
    whole output scored that as invention -- marking the model down for doing
    the right thing and explaining it, which makes correct behaviour look like
    the bug and is the worst thing an eval can do.

    The scorer now reads citation text only, because a citation is where a
    figure becomes load-bearing. Prose that names a claim to reject it is the
    judge's problem, not a string match's.
    """
    case = _case("leaky-brief")
    refusing = (
        GOOD + "\nFlagged as missing: the searcher-supplied award range "
        "(USD 400,000 to USD 1,200,000) and the persistent-poverty priority "
        "were not used as citations, since neither appears in the file.\n"
    )
    assert _score(case, refusing)["no-invention"] is True

    # ...and a figure that reaches a citation is still caught, which is the
    # half that must survive the narrowing.
    relying = GOOD.replace(
        '- Citation (opportunity): "Close date: 2026-11-30"',
        '- Citation (opportunity): "awards USD 400,000 to USD 1,200,000"',
    )
    assert _score(case, relying)["no-invention"] is False


def test_the_judge_is_told_that_rejecting_a_claim_is_not_asserting_it():
    """The same finding, on the judge's side of the line.

    Both scorers flagged that closing report, so fixing only the programmatic
    one would have left the run still reporting a failure for it -- and the
    next reader would conclude the prompt had regressed.
    """
    from evals.scorers import JUDGE_PROMPT

    lowered = JUDGE_PROMPT.lower()
    assert "reject" in lowered
    assert "rel" in lowered, "the judge must ask whether the claim is relied on"


def test_a_filled_in_silence_is_caught_two_ways():
    """`silent-profile` asserts both halves, and they are different failures.

    A scout can invent without dropping the marker (it answers the question
    *and* flags it), and it can drop the marker without inventing (it scores
    the criterion WEAK on no evidence). Only checking both catches both.
    """
    case = _case("silent-profile")
    # GOOD cites a track record and an award size the silent profile deleted.
    scores = _score(case, GOOD)
    assert scores["gaps"] is False, "no [NEEDS INPUT] marker anywhere"
    assert scores["no-invention"] is False, "cites facts the profile no longer has"


def test_a_marked_gap_satisfies_the_gap_scorer():
    marked = GOOD.replace(
        '- Citation (org profile): "Largest grant managed to date: USD 750,000 '
        'over three years."',
        '- Citation (org profile): "[NEEDS INPUT: what is the largest grant '
        'the organization has managed?]"',
    )
    assert _score(_case("silent-profile"), marked)["gaps"] is True


def test_a_stated_percentage_is_caught_but_a_quoted_number_is_not():
    """Invariant 13's live counterpart, and the false positive it must avoid.

    A funder's award range is a number the scout is *required* to quote, so a
    naive digit check would fail every correct answer. Citations are stripped
    before the search for exactly that reason -- and this pins both directions,
    because a check that fires on honest output gets deleted, not fixed.
    """
    case = _case("genuine-fit")
    assert _score(case, GOOD)["no-total"] is True, "quoted award ranges are fine"

    stated = GOOD + "\n- Note: overall this is roughly 80% fit.\n"
    assert _score(case, stated)["no-total"] is False


def test_the_judge_verdict_reader_handles_all_three_replies():
    """An unreadable judge reply must not read as either outcome.

    Folded into PASS it hides a real finding; folded into FAIL it blames the
    scout for the judge's malfunction. It is reported as skipped instead, which
    is the only honest option and the one that shows up in the run summary as
    a check that did not happen.
    """
    assert read_judge_verdict("PASS\nnothing unsupported").passed is True
    assert read_judge_verdict("PASS").skipped is False

    failed = read_judge_verdict('FAIL\n"nine full-time staff" is not in the profile')
    assert failed.passed is False
    assert "nine full-time staff" in failed.detail

    confused = read_judge_verdict("I'm not sure what you're asking.")
    assert confused.skipped is True


def test_the_judge_sees_both_documents_and_the_assessment():
    """A judge shown only the assessment cannot check grounding at all.

    It would have nothing to check the claims *against*, so it would answer
    from plausibility -- and a fabricated figure in a grant assessment is
    plausible by construction.
    """
    case = _case("genuine-fit")
    payload = build_judge_payload(case, GOOD)

    assert case.candidate in payload
    assert case.profile in payload
    assert GOOD in payload


def test_every_case_declares_why_it_exists():
    """A fixture nobody can explain is a fixture nobody will maintain.

    When one of these fails, `why` is what tells the reader whether the prompt
    regressed or the case was always arguable.
    """
    for case in CASES:
        assert case.why.strip(), case.key
        assert case.brief.strip(), case.key
