"""Tests for the fit-scoring grammar and its parser.

The parser is the only thing standing between what a model wrote and a number
a human acts on, and every way it can be wrong is quiet: a criterion silently
unscored reads as a poor fit, an unparseable file reads as a zero, a total that
drifts reads as a ranking. So these pin the arithmetic exactly, and pin each
malformed-input case to the specific wrong answer it would otherwise give.

All pure strings in, dataclasses out -- no disk, no network, no model.
"""

from __future__ import annotations

import re

import pytest

from grant_writer.opportunities import (
    MAX_TOTAL_POINTS,
    RUBRIC,
    VERDICTS,
    ScoredOpportunity,
    parse_scored_markdown,
    rank_opportunities,
    rubric_brief,
    untraceable_citations,
)

# The one import here that is not the module under test. It stays inside this
# file's "no disk, no network, no model" rule -- importing `prompts` only
# builds strings -- and it belongs beside the parser rather than in
# test_wiring, because what invariant 13 protects is the tie between the two.
from grant_writer.prompts import SCOUT_PROMPT

# The format SCOUT_PROMPT asks for, filled in. Every other case in this file is
# a mutation of this one, so a change to the grammar shows up here first.
WELL_FORMED = """\
# Opportunity: Tribal Colleges and Universities Program

- Number: 21-595
- Agency: U.S. National Science Foundation
- Close date: Sep 01, 2026
- Award range: $100,000 to $3,500,000

## eligibility
- Verdict: STRONG
- Citation (opportunity): "eligible applicants are Tribal Colleges"
- Citation (org profile): "Entity type: Tribal College"

## mission-alignment
- Verdict: STRONG
- Citation (opportunity): "improve the quality of STEM education"

## program-fit
- Verdict: MODERATE
- Citation (org profile): "we serve 240 students across 6 Title I schools"

## track-record
- Verdict: WEAK
- Citation (org profile): "[NEEDS INPUT: any prior NSF awards not yet recorded?]"

## award-size-fit
- Verdict: STRONG
- Citation (opportunity): "Award floor $100,000"

## timeline-feasibility
- Verdict: MODERATE
- Citation (opportunity): "closes Sep 01, 2026"
"""


def _criterion(opportunity, key):
    return next(c for c in opportunity.criteria if c.key == key)


def _without(section: str) -> str:
    """WELL_FORMED with one `## section` block removed."""
    blocks = WELL_FORMED.split("\n## ")
    return "\n## ".join(b for b in blocks if not b.startswith(section))


def test_the_worked_example_scores_exactly():
    """The arithmetic, pinned to the number and not to a range.

    A weight edited without meaning to changes a total silently -- there is no
    exception to raise, only a shortlist that ranks differently than it did.
    """
    opportunity = parse_scored_markdown(WELL_FORMED, key="nsf-21-595")

    assert [(c.key, c.verdict, c.points) for c in opportunity.criteria] == [
        ("eligibility", "STRONG", 20),
        ("mission-alignment", "STRONG", 25),
        ("program-fit", "MODERATE", 12),
        ("track-record", "WEAK", 3),
        ("award-size-fit", "STRONG", 10),
        ("timeline-feasibility", "MODERATE", 6),
    ]
    assert opportunity.total_points == 76
    assert opportunity.fit_percent == 76.0
    assert opportunity.warnings == ()
    assert not opportunity.disqualified


def test_the_weights_sum_to_one_hundred():
    """So a score reads as a percentage with no conversion anywhere.

    `fit_percent` divides by this. Weights that sum to 97 still produce a
    plausible-looking number, just not the one any caption claims it is.
    """
    assert MAX_TOTAL_POINTS == 100
    assert sum(c.max_points for c in RUBRIC) == 100


def test_the_header_and_title_are_captured():
    opportunity = parse_scored_markdown(WELL_FORMED, key="nsf-21-595")
    assert opportunity.title == "Tribal Colleges and Universities Program"
    assert opportunity.fields["number"] == "21-595"
    assert opportunity.fields["agency"] == "U.S. National Science Foundation"


def test_the_caller_s_key_wins_over_anything_in_the_body():
    """The filename is true by construction; an `- id:` line is model output.

    If the body won, a scout that garbled its own id would file the candidate
    under a label no frontend can match back to a file -- and the "Read the
    source" link would open nothing.
    """
    opportunity = parse_scored_markdown(
        WELL_FORMED.replace("- Number: 21-595", "- id: something-else"),
        key="nsf-21-595",
    )
    assert opportunity.key == "nsf-21-595"


def test_an_ineligible_candidate_is_flagged_without_losing_its_score():
    """Disqualification and score stay independent.

    Zeroing the total on a gating NONE would hide the evidence a human needs to
    challenge a wrong eligibility call -- which is exactly the call most worth
    challenging, since it is the one that removes an opportunity entirely.

    Pins invariant 15.
    """
    text = WELL_FORMED.replace(
        "## eligibility\n- Verdict: STRONG", "## eligibility\n- Verdict: NONE"
    )
    opportunity = parse_scored_markdown(text, key="x")

    assert opportunity.disqualified
    assert opportunity.total_points == 56
    assert opportunity.fit_percent == 56.0


def test_a_gating_none_is_the_only_thing_that_disqualifies():
    """A NONE on a non-gating criterion is a bad score, not a bar."""
    text = WELL_FORMED.replace(
        "## track-record\n- Verdict: WEAK", "## track-record\n- Verdict: NONE"
    )
    assert not parse_scored_markdown(text, key="x").disqualified


def test_nothing_parseable_is_unscored_rather_than_zero():
    """`None`, never 0.0 -- the distinction the whole shortlist rests on.

    "We could not read this" and "this is a bad fit" are opposite instructions
    to the person reading. Collapsed together, a candidate written in the wrong
    format is indistinguishable from one that was judged and rejected, and it
    sinks to the bottom of the list with no way to tell.
    """
    opportunity = parse_scored_markdown("just prose the model wrote instead", key="x")

    assert opportunity.fit_percent is None
    assert opportunity.total_points == 0
    assert opportunity.scored_count == 0
    # One per missing section, plus the missing title.
    assert len(opportunity.warnings) == len(RUBRIC) + 1


def test_a_missing_section_is_warned_and_not_silently_zero():
    opportunity = parse_scored_markdown(_without("program-fit"), key="x")
    criterion = _criterion(opportunity, "program-fit")

    assert criterion.verdict is None
    assert not criterion.is_scored
    assert criterion.points == 0
    assert any("program-fit" in w and "no section" in w for w in opportunity.warnings)
    # The rest still score: one bad block must not cost the whole candidate.
    assert opportunity.total_points == 64


def test_an_unrecognized_verdict_scores_zero_and_says_so():
    """The one case where tolerance would mean guessing.

    Everything else here is kept and flagged. A verdict word outside the
    vocabulary cannot be scored without inventing what the model meant, which
    is the single thing this module exists to prevent -- so it is the one
    deviation that costs its points.
    """
    text = WELL_FORMED.replace(
        "## program-fit\n- Verdict: MODERATE", "## program-fit\n- Verdict: PRETTY-GOOD"
    )
    opportunity = parse_scored_markdown(text, key="x")

    assert _criterion(opportunity, "program-fit").points == 0
    assert any("PRETTY-GOOD" in w for w in opportunity.warnings)


def test_a_verdict_with_no_citation_still_scores_but_is_flagged():
    """Tolerance runs toward keeping the score, deliberately.

    Silently zeroing a real judgement over a formatting slip buries a genuine
    opportunity in a list nobody re-checks. The flag is visible in both
    frontends; the lost score would not have been.
    """
    text = WELL_FORMED.replace('- Citation (opportunity): "Award floor $100,000"\n', "")
    opportunity = parse_scored_markdown(text, key="x")

    assert _criterion(opportunity, "award-size-fit").points == 10
    assert any("no citation" in w for w in opportunity.warnings)


def test_an_unrecognized_citation_source_keeps_the_quote():
    """Only the label is unexpected; the quote is still evidence."""
    text = WELL_FORMED.replace(
        'Citation (org profile): "we serve 240', 'Citation (memory): "we serve 240'
    )
    opportunity = parse_scored_markdown(text, key="x")
    criterion = _criterion(opportunity, "program-fit")

    assert criterion.citations[0].text.startswith("we serve 240")
    assert any("memory" in w for w in opportunity.warnings)


def test_the_last_verdict_wins_when_a_section_has_two():
    """Freshest wins, the rule `compliance_verdict` already applies.

    A scout that revised itself mid-file meant the second one; taking the first
    would score the judgement it withdrew.
    """
    text = WELL_FORMED.replace(
        "## program-fit\n- Verdict: MODERATE",
        "## program-fit\n- Verdict: WEAK\n- Verdict: MODERATE",
    )
    opportunity = parse_scored_markdown(text, key="x")

    assert _criterion(opportunity, "program-fit").verdict == "MODERATE"
    assert any("Verdict lines" in w for w in opportunity.warnings)


@pytest.mark.parametrize("written", ["strong", "Strong", "STRONG.", " STRONG "])
def test_verdict_case_and_punctuation_are_normalised(written):
    """A correct judgement must not be thrown away over shift-key noise."""
    text = WELL_FORMED.replace(
        "## award-size-fit\n- Verdict: STRONG",
        f"## award-size-fit\n- Verdict: {written}",
    )
    assert (
        _criterion(parse_scored_markdown(text, key="x"), "award-size-fit").points == 10
    )


def test_asterisk_bullets_are_accepted():
    """Both markdown bullet characters mean the same thing to a reader."""
    text = WELL_FORMED.replace("\n- Verdict:", "\n* Verdict:")
    assert parse_scored_markdown(text, key="x").total_points == 76


def test_a_citation_containing_a_colon_is_not_truncated_at_it():
    """The label/value split takes the FIRST colon; the quote may contain more.

    A ratio, a time, or a "Note:" inside the funder's own wording would
    otherwise silently shorten the evidence a verdict rests on.
    """
    text = WELL_FORMED.replace(
        '- Citation (opportunity): "Award floor $100,000"',
        '- Citation (opportunity): "Match required: 1:1 for years 2:3"',
    )
    citation = _criterion(
        parse_scored_markdown(text, key="x"), "award-size-fit"
    ).citations[0]
    assert citation.text == "Match required: 1:1 for years 2:3"


def test_gap_markers_are_recognised_and_collected():
    """The same substring `workspace.count_gaps` scans for.

    That agreement is what lets a scan directory be counted by the function
    written for an application directory -- a marker spelled differently here
    would be shown on screen but left out of the count above it.
    """
    opportunity = parse_scored_markdown(WELL_FORMED, key="x")
    gaps = opportunity.gaps

    assert len(gaps) == 1
    assert gaps[0].is_gap
    assert "[NEEDS INPUT" in gaps[0].text
    assert not _criterion(opportunity, "mission-alignment").citations[0].is_gap


def test_an_unknown_heading_is_ignored_without_a_warning():
    """A scout adding its own prose section is harmless.

    Warning on it would train the reader to skim past warnings, which is the
    one habit that makes the real ones useless.
    """
    text = WELL_FORMED + "\n## overall-thoughts\n- Verdict: STRONG\n"
    opportunity = parse_scored_markdown(text, key="x")

    assert opportunity.total_points == 76
    assert opportunity.warnings == ()


def test_ranking_puts_judged_before_unjudged_and_scored_before_both():
    """Three tiers, because "not at the top" has two different meanings.

    A disqualified candidate has been read and ruled out; an unscorable one has
    not been read at all. Sorting them by score alone would interleave the two,
    and an unscored candidate would land wherever 0 happens to fall.
    """
    high = parse_scored_markdown(WELL_FORMED, key="high")
    low = parse_scored_markdown(
        WELL_FORMED.replace(
            "## mission-alignment\n- Verdict: STRONG",
            "## mission-alignment\n- Verdict: WEAK",
        ),
        key="low",
    )
    barred = parse_scored_markdown(
        WELL_FORMED.replace(
            "## eligibility\n- Verdict: STRONG", "## eligibility\n- Verdict: NONE"
        ),
        key="barred",
    )
    unreadable = parse_scored_markdown("", key="unreadable")

    ranked = rank_opportunities([unreadable, barred, low, high])
    assert [o.key for o in ranked] == ["high", "low", "barred", "unreadable"]


def test_ranking_is_stable_for_equal_scores():
    """Equal scores keep the order they were found in.

    A list that reshuffles between reruns of the same scan reads as new
    information when nothing changed.
    """
    first = parse_scored_markdown(WELL_FORMED, key="a")
    second = parse_scored_markdown(WELL_FORMED, key="b")
    assert [o.key for o in rank_opportunities([first, second])] == ["a", "b"]
    assert [o.key for o in rank_opportunities([second, first])] == ["b", "a"]


def test_the_prompt_s_rubric_names_every_criterion_the_parser_scores():
    """The tie between what the scout is asked for and what is counted.

    These are two different files. If the prompt named a criterion the parser
    does not look for, the scout would answer it and score zero for it, and the
    only symptom would be a total that came out low.

    Pins invariant 13.
    """
    brief = rubric_brief()
    for criterion in RUBRIC:
        assert criterion.key in brief
        assert criterion.label in brief
    # And no weights: the model must have no numbers to add up.
    for criterion in RUBRIC:
        assert str(criterion.weight) not in brief


def test_the_whole_scout_prompt_states_no_weight_and_no_total():
    """Pins invariant 13.

    Its sibling above checks `rubric_brief()`, which is only the fragment of
    the prompt rendered from `RUBRIC`. The invariant is about the whole prompt:
    a hand-written "## Scoring" section, or a `Total:` line added to the format
    block, puts an addable number in from outside the rendered fragment and the
    sibling test cannot see it.

    Weights are matched as standalone numbers, derived from `RUBRIC` rather
    than hardcoded, so changing a weight re-aims the check instead of leaving
    it guarding the old value. Whole-word, because a bare substring test
    reports "20" inside "2026" and teaches the next reader to loosen the test
    rather than trust it.

    As it stands the prompt contains no digits at all, which is the property
    doing the real work: the scout picks words, and there is nowhere in the
    grammar to put a total even if it wanted one.
    """
    for criterion in RUBRIC:
        assert not re.search(rf"\b{criterion.weight}\b", SCOUT_PROMPT), (
            f"weight {criterion.weight} ({criterion.key}) has reached the "
            f"prompt -- the scout can now do arithmetic nobody checks"
        )
    for label in ("Total:", "Score:", "Points:"):
        assert label not in SCOUT_PROMPT, label


def test_the_score_label_keeps_unscored_and_zero_distinct():
    """Both frontends print this string; neither decides it.

    Two copies of the `None` check is how "unscored" starts rendering as "0%"
    in one frontend and not the other, under captions claiming they mean the
    same thing.

    Pins invariant 14.
    """
    assert parse_scored_markdown(WELL_FORMED, key="x").score_label == "76%"
    assert parse_scored_markdown("", key="x").score_label == "unscored"

    # An honest zero is a judgement and must print as one.
    all_none = WELL_FORMED
    for criterion in RUBRIC:
        all_none = re.sub(
            rf"(## {re.escape(criterion.key)}\n- Verdict: )\w+", r"\1NONE", all_none
        )
    scored_zero = parse_scored_markdown(all_none, key="x")
    assert scored_zero.fit_percent == 0.0
    assert scored_zero.score_label == "0%"


def test_a_candidate_with_no_title_falls_back_to_its_key():
    """A blank row is harder to act on than one labelled by its filename."""
    assert parse_scored_markdown("", key="nsf-21-595").display_title == "nsf-21-595"
    assert (
        parse_scored_markdown(WELL_FORMED, key="nsf-21-595").display_title
        == "Tribal Colleges and Universities Program"
    )


# ---- citation traceability --------------------------------------------------
#
# A false positive here is the expensive direction. Flagging an honest citation
# teaches the reader to skim past the flag, which costs more than the
# unverifiable citation it was meant to catch -- so most of these pin quotes
# that must NOT be reported.


def _one_citation(source: str, text: str) -> ScoredOpportunity:
    """A minimal scored file carrying exactly one citation."""
    return parse_scored_markdown(
        "# Opportunity: T\n\n"
        f'## eligibility\n- Verdict: STRONG\n- Citation ({source}): "{text}"\n',
        key="k",
    )


def test_a_quote_split_across_a_wrapped_line_is_still_traceable():
    """The case that made a hand check report two false alarms.

    An archived candidate file is hard-wrapped, so a quotation routinely spans
    a line break -- `"...providing lab\\n  equipment for K-8 schools"`. Any
    line-oriented comparison reads that as absent, which is how a scan whose
    every citation was genuine got reported as having two it could not support.
    """
    source = (
        "STEM programs, including providing lab\n  equipment for K-8 urban schools.\n"
    )
    opportunity = _one_citation(
        "opportunity", "including providing lab equipment for K-8 urban schools"
    )
    assert (
        untraceable_citations(opportunity, opportunity_text=source, profile_text="")
        == ()
    )


@pytest.mark.parametrize(
    ("source", "quoted"),
    [
        # Emphasis the scout added that the source does not carry.
        ("eligible states include Ohio and Texas", "eligible states include **Ohio**"),
        # Emphasis in the source that the quote drops.
        ("eligible states include **Ohio**", "eligible states include Ohio"),
        # Re-capitalised at a sentence start.
        ("awards range from $25,000", "Awards range from $25,000"),
        # Collapsed runs of whitespace.
        ("awards   range     from $25,000", "awards range from $25,000"),
    ],
)
def test_cosmetic_differences_do_not_make_a_citation_unverifiable(source, quoted):
    opportunity = _one_citation("opportunity", quoted)
    assert (
        untraceable_citations(opportunity, opportunity_text=source, profile_text="")
        == ()
    )


def test_a_quote_that_is_not_in_the_source_is_reported():
    """The failure this exists for.

    The scout has no tools, so a quote it could not have read came from the
    orchestrator's delegation -- true, most likely, and unverifiable by anyone
    reading the scan afterwards, because the source pane cannot show it.
    """
    opportunity = _one_citation("opportunity", "awards up to $5,000,000")
    missing = untraceable_citations(
        opportunity, opportunity_text="awards up to $100,000", profile_text=""
    )
    assert [c.text for c in missing] == ["awards up to $5,000,000"]


def test_each_source_is_checked_against_its_own_document():
    """An `org profile` citation found only in the opportunity is still wrong.

    Checking against the concatenation of both would let a claim about the
    organization be supported by the funder's own marketing.

    Pins invariant 16.
    """
    opportunity = _one_citation("org profile", "we serve 240 students")
    assert untraceable_citations(
        opportunity, opportunity_text="we serve 240 students", profile_text=""
    )
    assert not untraceable_citations(
        opportunity, opportunity_text="", profile_text="we serve 240 students"
    )


def test_a_gap_marker_is_never_reported_as_unverifiable():
    """`[NEEDS INPUT: ...]` is the scout saying it had nothing to quote.

    That is the opposite of an unsupported quote, and reporting it as one would
    punish the exact behaviour the anti-fabrication rule asks for.
    """
    opportunity = _one_citation("org profile", "[NEEDS INPUT: prior federal awards?]")
    assert (
        untraceable_citations(opportunity, opportunity_text="", profile_text="") == ()
    )


def test_a_missing_candidate_file_makes_its_citations_unverifiable():
    """An archive that is not there cannot support anything quoted from it."""
    opportunity = _one_citation("opportunity", "awards up to $100,000")
    assert (
        len(untraceable_citations(opportunity, opportunity_text="", profile_text=""))
        == 1
    )


def test_every_verdict_word_is_scoreable():
    """`VERDICTS` is what SCOUT_PROMPT lists. A word listed there but unknown
    to the scorer would be answered in good faith and counted as zero."""
    for verdict in VERDICTS:
        text = WELL_FORMED.replace(
            "## award-size-fit\n- Verdict: STRONG",
            f"## award-size-fit\n- Verdict: {verdict}",
        )
        criterion = _criterion(parse_scored_markdown(text, key="x"), "award-size-fit")
        assert criterion.verdict == verdict, verdict
        assert criterion.is_scored, verdict
