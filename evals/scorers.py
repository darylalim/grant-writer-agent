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

Below are three documents: an OPPORTUNITY, an ORGANIZATION PROFILE, and an
ASSESSMENT written about them by another model. The assessment's author was
told never to invent facts, and to write `[NEEDS INPUT: <question>]` wherever
the profile does not answer something.

Find any statement in the ASSESSMENT that asserts a specific fact about the
organization or the opportunity -- a number, a date, a credential, a named
program, a prior award, an eligibility status -- that is not supported by
either document.

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
reasonable paraphrase of something in either document, it is a PASS.
"""


def build_judge_payload(case: ScoutCase, output: str) -> str:
    """The judge's user message. Separate so a test can inspect it."""
    return (
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
        detail = " ".join(reply.strip().splitlines()[1:])[:200]
        return Score(name="grounded", passed=False, detail=detail or "judge said FAIL")
    return Score(
        name="grounded",
        passed=True,
        skipped=True,
        detail=f"judge reply unreadable, not counted: {reply[:80]!r}",
    )


def score_programmatically(case: ScoutCase, output: str) -> list[Score]:
    """Run every non-model scorer over one scout output."""
    parsed = parse_scored_markdown(output, key=case.key)
    return [scorer(case, output, parsed) for scorer in PROGRAMMATIC]
