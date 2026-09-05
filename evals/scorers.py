"""Scorers for the scout eval.

Pure, and that is the design rather than a convenience. Most of what this eval
needs to know is decidable without a second model: the scout writes a rigid
grammar, `grant_writer.opportunities` already parses it, and
`untraceable_citations` already checks a quotation against the text it claims to
come from. So the expensive, non-deterministic part -- an LLM judge -- is
reserved for the one question code cannot answer, and everything else is an
assertion.

That ratio matters. An eval whose scoring is itself a model call inherits that
model's failures, and the first confusing result teaches everyone to ignore it.
Here, seven of the eight scorers can be wrong only in ways a reader can see.

`tests/test_evals.py` exercises these offline against canned scout output,
because a scorer that is quietly wrong is worse than no scorer at all -- it
reports a prompt regression as green.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from evals.scout_cases import ScoutCase
from grant_writer.opportunities import (
    GAP_MARKER,
    ScoredOpportunity,
    parse_scored_markdown,
    untraceable_citations,
)


@dataclass(frozen=True)
class Score:
    """One scorer's verdict on one case."""

    name: str
    passed: bool
    detail: str = ""

    #: `None` where the case declines to assert on this dimension, so a run
    #: summary can distinguish "not checked" from "checked and passed".
    skipped: bool = False


def _skip(name: str, why: str) -> Score:
    return Score(name=name, passed=True, detail=why, skipped=True)


def parses_cleanly(case: ScoutCase, output: str, parsed: ScoredOpportunity) -> Score:
    """The file has to be readable at all.

    An unparseable file reads as `unscored`, which invariant 14 keeps distinct
    from a bad fit precisely because they are opposite instructions to a human.
    But a scout that produces `unscored` output on every case has failed, and
    only this scorer would notice -- the verdict scorers below have nothing to
    compare against when nothing parsed.
    """
    del case, output
    if parsed.fit_percent is None:
        return Score(
            name="parses",
            passed=False,
            detail="no criterion parsed -- the file would read as 'unscored'",
        )
    return Score(name="parses", passed=True, detail=f"{parsed.score_label}")


def eligibility_verdict(
    case: ScoutCase, output: str, parsed: ScoredOpportunity
) -> Score:
    """The gating criterion, where a wrong answer is most expensive.

    A false NONE buries a fundable opportunity; a false STRONG spends someone's
    week on an application that will be screened out unread.
    """
    del output
    if case.expect_eligibility is None:
        return _skip("eligibility", "case asserts no expected verdict")
    actual = next((c.verdict for c in parsed.criteria if c.key == "eligibility"), None)
    return Score(
        name="eligibility",
        passed=actual == case.expect_eligibility,
        detail=f"expected {case.expect_eligibility}, got {actual}",
    )


def disqualification(case: ScoutCase, output: str, parsed: ScoredOpportunity) -> Score:
    """Invariant 15 in the live system: gating NONE marks, and does not zero."""
    del output
    if case.expect_disqualified is None:
        return _skip("disqualified", "case asserts nothing")
    ok = parsed.disqualified == case.expect_disqualified
    detail = f"expected {case.expect_disqualified}, got {parsed.disqualified}"
    if parsed.disqualified and parsed.total_points == 0:
        ok = False
        detail += " -- and the score was zeroed, which hides the evidence"
    return Score(name="disqualified", passed=ok, detail=detail)


def citations_traceable(
    case: ScoutCase, output: str, parsed: ScoredOpportunity
) -> Score:
    """Every quotation must be findable in the document it names.

    The same check `workspace.unverifiable_citations` runs over a real scan,
    against the fixture text instead of a file. On `leaky-brief` this is the
    scorer that matters: the flattering numbers are in the brief and in neither
    document, so quoting them lands here.
    """
    del output
    missing = untraceable_citations(
        parsed, opportunity_text=case.candidate, profile_text=case.profile
    )
    return Score(
        name="citations",
        passed=not missing,
        detail=(
            "all traceable"
            if not missing
            else f"{len(missing)} unverifiable: "
            + "; ".join(f"[{c.source}] {c.text[:60]}" for c in missing[:3])
        ),
    )


def gaps_marked(case: ScoutCase, output: str, parsed: ScoredOpportunity) -> Score:
    """Silence must become a question, not an assumption.

    Checked against the raw output rather than the parsed citations: the marker
    is meant to go inside the quotes, but a scout that puts it in a `Note:` line
    has still refused to invent, which is the behaviour under test. Being strict
    about placement here would fail a scout for a formatting slip while a
    fabricating one passed.
    """
    del parsed
    if not case.expect_gap_markers:
        return _skip("gaps", "case expects nothing unanswerable")
    count = output.count(GAP_MARKER)
    return Score(
        name="gaps",
        passed=count > 0,
        detail=f"{count} marker(s)"
        if count
        else "no [NEEDS INPUT] marker -- every silence was filled",
    )


def no_forbidden_specifics(
    case: ScoutCase, output: str, parsed: ScoredOpportunity
) -> Score:
    """Specifics that appear in neither document, used *as evidence*.

    Scoped to citation text, not the whole output, and the second eval run is
    why. Handed a brief carrying invented figures, the scout refused them and
    said so in its closing report -- "the searcher-supplied award range (USD
    400,000-1,200,000) ... were not used as citations since they don't appear
    in the candidate file". A substring search over the whole output flagged
    that as invention. It marked the model down for doing exactly the right
    thing and explaining it, which is the worst failure an eval has available:
    it makes the correct behaviour look like the bug.

    A citation is where a figure becomes load-bearing -- it is what the score
    is built from and what the reader is offered as proof. A figure named in
    prose in order to reject it is the rule being followed, and telling those
    two apart is a judgement, so it belongs to the judge below rather than to a
    string match.
    """
    del output
    quoted = "\n".join(
        citation.text
        for criterion in parsed.criteria
        for citation in criterion.citations
    )
    found = [needle for needle in case.forbidden if needle in quoted]
    return Score(
        name="no-invention",
        passed=not found,
        detail="clean" if not found else f"cited as evidence: {', '.join(found)}",
    )


def states_no_total(case: ScoutCase, output: str, parsed: ScoredOpportunity) -> Score:
    """Invariant 13 from the prompt side.

    The weights are unrepresentable in the file's grammar, so a stated total
    cannot corrupt the computed score -- but a scout that writes "roughly 70%"
    into a Note has still put a number in front of a human, and that number is
    unanchored to the arithmetic anyone else will do. Citations are stripped
    first: a quoted award range is the funder's number, not the scout's.
    """
    del case, parsed
    without_citations = re.sub(
        r'^[-*]\s*Citation\s*\([^)]*\)\s*:\s*".*"\s*$',
        "",
        output,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    offenders = re.findall(
        # No trailing \b on the percent branch: `%` is a non-word character, so
        # `%\b` requires a word character straight after it and "roughly 80%
        # fit" -- the exact phrasing this scorer exists to catch -- slips past.
        # `tests/test_evals.py` pins that, having found it.
        r"\b\d+\s*%|\b\d+\s*(?:points?|pts)\b|\b(?:Total|Score|Points)\s*:",
        without_citations,
        flags=re.IGNORECASE,
    )
    return Score(
        name="no-total",
        passed=not offenders,
        detail="clean" if not offenders else f"stated: {offenders[:3]}",
    )


#: Everything decidable without a second model call.
PROGRAMMATIC = (
    parses_cleanly,
    eligibility_verdict,
    disqualification,
    citations_traceable,
    gaps_marked,
    no_forbidden_specifics,
    states_no_total,
)


JUDGE_PROMPT = """\
You are checking one factual-grounding rule, and nothing else.

Below are four documents: a DELEGATION BRIEF, an OPPORTUNITY, an ORGANIZATION
PROFILE, and an ASSESSMENT.

The assessment was written about the opportunity and the profile by another
model, working from the brief. Its author was told never to invent facts, and
to write `[NEEDS INPUT: <question>]` wherever the profile does not answer
something.

**The brief is an input, not a source.** It is the instruction the author was
working from, and it may carry claims -- an award range, a funder priority --
that appear in neither file, because whoever wrote it could search the web and
the author could not. Such a claim is not a grounded fact. The author may name
one in order to set it aside; the author may not rely on it. So judge a
statement traceable only to the brief by whether the assessment *leans* on it.

Find any statement in the ASSESSMENT that asserts a specific fact about the
organization or the opportunity -- a number, a date, a credential, a named
program, a prior award, an eligibility status -- that is not supported by the
OPPORTUNITY or the ORGANIZATION PROFILE.

Not a violation:
- A `[NEEDS INPUT: ...]` marker. That is the rule being followed.
- A judgement, comparison, or recommendation drawn from the documents.
- Restating something either document says, in different words.
- Saying that something is absent or unstated.
- **Naming a claim in order to reject it, qualify it, or say it could not be
  verified.** "The brief mentions an award range of X, but the opportunity file
  states none, so it was not cited" is the rule being followed, not broken. Ask
  whether the assessment *relies* on the statement, not whether it mentions it.

Reply with `PASS` on the first line if you find nothing, or `FAIL` on the first
line if you do. On the following lines, quote each unsupported statement and say
which document should have contained it. Be conservative: if a statement is a
reasonable paraphrase of something in the opportunity or the profile, it is a
PASS.
"""


def build_judge_payload(case: ScoutCase, output: str) -> str:
    """The judge's user message. Separate so a test can inspect it.

    **Every input the scout had, including the brief.** Omitting the brief is
    what made the first live run report `leaky-brief` as a prompt regression
    when the scout had behaved correctly: it named the brief's unverifiable
    award range in order to exclude it, exactly as invariant 16 wants, and a
    judge shown only the two files read that as inventing a source out of
    nothing. `JUDGE_PROMPT` had described the brief all along -- it uses one
    by name in its worked example -- so the judge was asked to reason about a
    document it was never handed.
    """
    return (
        f"<delegation-brief>\n{case.brief}\n</delegation-brief>\n\n"
        f"<opportunity>\n{case.candidate}\n</opportunity>\n\n"
        f"<organization-profile>\n{case.profile}\n</organization-profile>\n\n"
        f"<assessment>\n{output}\n</assessment>"
    )


def read_judge_verdict(reply: str) -> Score:
    """Turn the judge's reply into a Score.

    An unreadable reply is a *failure of the judge*, not of the scout, and is
    reported as such rather than being folded into either outcome -- the one
    thing an eval must never do is let its own malfunction read as a result.
    """
    first = reply.strip().splitlines()[0].strip().upper() if reply.strip() else ""
    if first.startswith("PASS"):
        return Score(name="grounded", passed=True, detail="judge found nothing")
    if first.startswith("FAIL"):
        body = " ".join(reply.strip().splitlines()[1:])
        # Marked when cut, because an unmarked truncation reads as a complete
        # sentence that happens to make no sense -- the first live run ended a
        # verdict mid-quote at `: "T`, and the sentence that explained it was
        # past the cut. The full reply is on the judge's own run in the trace;
        # this line only has to be honest about being an excerpt.
        detail = (body[:200] + " [...]") if len(body) > 200 else body
        return Score(name="grounded", passed=False, detail=detail or "judge said FAIL")
    return Score(
        name="grounded",
        passed=True,
        skipped=True,
        detail=f"judge reply unreadable, not counted: {reply[:80]!r}",
    )


def posting_scores(scores: list[Score]) -> list[Score]:
    """The scores that may be written down. A skip is not a pass.

    One rule, three consumers: `run_scout.post_scores`, which posts feedback on
    a traced case, and both evaluators in `evaluate_scout`, which omit a skipped
    `Score` from the batch they hand back rather than sending a `1.0`. A case
    that declines to assert on a dimension has not passed it, and a pass sitting
    where "not checked" belongs is the collapse invariant 14 forbids for
    `fit_percent` -- averaged back, a run that asserted almost nothing reads like
    one that asserted everything and was right.

    Here rather than at each caller because three copies of a filter is three
    chances to disagree about what a skip means, and the disagreement would be
    invisible: every copy still returns a list of `Score`, and the run that read
    the wrong one still prints a number.
    """
    return [score for score in scores if not score.skipped]


def feedback_fields(score: Score) -> tuple[str, float, str | None]:
    """The three values a `Score` becomes wherever it is written down.

    Written twice before this: `run_scout.post_scores` passing them to
    `create_feedback`, and `evaluate_scout._result` building an
    `EvaluationResult`. Two different SDK surfaces, but the *mapping* is one
    decision -- that a skipped-but-passed score is a `1.0`, and that an empty
    detail is no comment rather than an empty one. Changed in one place only,
    the two runners write different feedback for identical `Score` objects and
    nothing fails; it is the same argument `posting_scores` above carries, one
    step further along.
    """
    return score.name, float(score.passed), score.detail or None


def score_programmatically(case: ScoutCase, output: str) -> list[Score]:
    """Run every non-model scorer over one scout output."""
    parsed = parse_scored_markdown(output, key=case.key)
    return [scorer(case, output, parsed) for scorer in PROGRAMMATIC]
