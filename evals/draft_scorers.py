"""Scorers for the drafting eval.

Pure, for the reason `scorers.py` gives: an eval whose scoring is wrong reports
a regression as green, with a number attached, so everything decidable without
a model is decided by code and `tests/test_evals.py` pins it offline. The judge
is reserved for the one question code cannot answer -- whether a claim about
the organization *relies* on something outside the two documents.

`numbers_traceable` is the drafting counterpart of `citations_traceable`, and
it inherits invariant 16's lesson in the same direction: a false positive is
the expensive one. Flagging an honest number teaches the reader to skim past
the flag. So the normalisation is generous -- thousands separators, currency
signs, "1.1 million", a figure wrapped across a line -- and tests pin each of
those as *not* reportable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from evals.draft_cases import DraftCase
from evals.scorers import Score, _skip, read_judge_verdict

# The colon is optional: the baseline's drafter wrote a bare `$[NEEDS INPUT]`
# beside "in non-Federal cost share", and an honest gap failed for its
# punctuation. DRAFTER_PROMPT asks for a question after the colon; a bare
# marker is a weaker one, not a missing one.
_MARKER = re.compile(r"\[NEEDS INPUT(?::.*?)?\]", re.IGNORECASE | re.DOTALL)

# One numeric token: an optional currency sign, digits (with or without
# thousands separators), an optional decimal part, and an optional magnitude
# word. `\s*` before the magnitude spans a line break, so a figure the drafter
# wrapped still reads as one number. The lookbehind keeps a digit inside a
# word (`H3`, `v2`) from counting.
_NUMBER = re.compile(
    # Not after a letter and hyphen either: "SF-425" names a form.
    r"(?<![\w.])(?<![A-Za-z]-)\$?\s?"
    r"(?P<int>\d{1,3}(?:,\d{3})+|\d+)"
    r"(?:\.(?P<frac>\d+))?"
    r"(?:\s*(?P<mag>million|thousand|[MmKk]\b))?"
    r"(?P<pct>\s*(?:%|percent\b))?",
    re.IGNORECASE,
)
_MAGNITUDE = {"million": 1_000_000, "m": 1_000_000, "thousand": 1_000, "k": 1_000}

# A regulation cited by number ("2 CFR 200.414(f)", "§ 200.320") is the
# literature DRAFTER_PROMPT asks for, not a fact about the organization. The
# first pilot flagged two of them.
_SECTION_REF = r"[\d.]+(?:\([0-9a-z]+\))*(?:\s*[\u2013-]\s*[\d.]+(?:\([0-9a-z]+\))*)?"
_CITATION = re.compile(
    rf"\b\d+\s*(?:CFR|C\.F\.R\.|U\.S\.C\.)\s*(?:§+\s*)?{_SECTION_REF}"
    rf"|§+\s*{_SECTION_REF}",
    re.IGNORECASE,
)

# A figure the regulation itself states, cited right beside it ("Items
# ≥$10,000 per unit (2 CFR 200.1)"), is the rule being quoted, not a fact
# about the organization. Same line, and close: a number further back is not
# covered by the citation.
_CITED_REACH = 40

# A number next to a unit of time is a schedule ("month 36", "within 90 days"):
# the project design, which the drafter is supposed to propose.
_TIME_BEFORE = re.compile(r"\b(?:months?|years?|weeks?|days?|quarters?|Q)\s*$", re.I)
# A hyphen too: v1 flagged "the 36-month budget".
_TIME_AFTER = re.compile(r"^(?:\s*|-)(?:months?|years?|weeks?|days?|quarters?)\b", re.I)


@dataclass(frozen=True)
class DraftOutput:
    """What one drafter run left behind, as the scorers read it."""

    #: The section file's content, or `None` if the file was never written.
    section: str | None
    #: Names of every tool the drafter called, in order.
    tool_calls: tuple[str, ...] = ()
    #: The same calls with their arguments, for checks that need the target.
    calls: tuple[tuple[str, dict], ...] = ()
    #: The drafter's closing report to the orchestrator.
    report: str = ""


def markers(text: str) -> list[str]:
    """Every `[NEEDS INPUT: ...]` marker in `text`."""
    return _MARKER.findall(text)


def outside_markers(text: str) -> str:
    """`text` with its markers removed: what the section asserts as fact."""
    return _MARKER.sub(" ", text)


def numbers(text: str) -> set[Decimal]:
    """The numeric values in `text`, normalised so spellings compare equal.

    `$1,100,000`, `1100000`, `$1.1 million` and `1.1M` are one value; `24%` is
    24. A token that is not a number after all (a stray comma) is dropped
    rather than raised on -- a scorer must not crash on prose.
    """
    return {value for value, _, _ in _tokens(text)}


def _tokens(text: str) -> list[tuple[Decimal, bool, bool]]:
    """`(value, is_percent, is_schedule)` for each number in `text`."""
    found: list[tuple[Decimal, bool, bool]] = []
    for match in _NUMBER.finditer(text):
        digits = match["int"].replace(",", "")
        if match["frac"]:
            digits += "." + match["frac"]
        try:
            value = Decimal(digits)
        except InvalidOperation:  # pragma: no cover - the regex admits digits only
            continue
        if match["mag"]:
            value *= _MAGNITUDE[match["mag"].lower()]
        schedule = bool(
            _TIME_BEFORE.search(text[: match.start()])
            or _TIME_AFTER.match(text[match.end() :])
        )
        found.append((value.normalize(), bool(match["pct"]), schedule))
    return found


def _incidental(value: Decimal) -> bool:
    """Numbers too small or too calendar-shaped to be an invented fact.

    Single digits ("3 states", "Year 2") and years are everywhere in a
    narrative and nowhere a fabrication worth catching; flagging them would
    bury the one invented salary under twenty false alarms.
    """
    if value != value.to_integral_value():
        return False
    return value < 10 or 2000 <= value <= 2040


def _derived(
    value: Decimal,
    is_percent: bool,
    known: set[Decimal],
    rates: set[Decimal],
    stated: set[Decimal],
) -> bool:
    """Plain arithmetic on figures the inputs give. The judge allows it too.

    Three shapes, each from the first pilot, and each kept narrow so the
    check still catches an invention:

    - a rate the inputs state, applied to a figure they state ("25% of the
      $250,000 minimum" is $62,500);
    - an input figure spread over a project's years ("$480,000 over three
      years" is $160,000 a year), divisors 2 to 5 only;
    - a percentage computed from two figures **the section itself states**
      ("58 coached, 41 (71%) passed"). Operands are drawn from the section,
      not the inputs: over every pair of input numbers, nearly any integer
      percentage is some ratio, and an invented 30% fringe rate would pass.
    """
    for rate in rates:
        if any(_close(a * rate / 100, value) for a in known):
            return True
    if any(_close(a / n, value) for a in known for n in range(2, 6)):
        return True
    if is_percent:
        traced = [a for a in stated if a in known and a > 0]
        return any(_close(100 * a / b, value) for a in traced for b in traced if a < b)
    return False


def _close(computed: Decimal, stated: Decimal) -> bool:
    """Equal once rounded the way a writer rounds: to the stated precision."""
    exponent = stated.as_tuple().exponent
    places = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    return round(computed, places) == stated


def _blank_cited(text: str) -> str:
    """`text` with citations, and figures cited right before them, blanked.

    Blanked rather than deleted, so offsets elsewhere in the line keep their
    meaning; a blanked span cannot read as a number.
    """
    chars = list(text)
    for cite in _CITATION.finditer(text):
        line_start = text.rfind("\n", 0, cite.start()) + 1
        start = max(line_start, cite.start() - _CITED_REACH)
        for number in _NUMBER.finditer(text, start, cite.start()):
            chars[number.start() : number.end()] = " " * (number.end() - number.start())
        chars[cite.start() : cite.end()] = " " * (cite.end() - cite.start())
    return "".join(chars)


def untraceable_numbers(
    section: str, sources: str, *, ignore: tuple[int, ...] = ()
) -> list[Decimal]:
    """Numbers the section states as fact that no input gives. Sorted.

    `ignore` is for figures another check already owns -- `no-leaked-figure`
    scores the brief's own numbers, so counting them here too would score one
    fault twice.
    """
    known = numbers(sources)
    rates = {value for value, pct, _ in _tokens(sources) if pct}
    text = _blank_cited(outside_markers(section))
    tokens = _tokens(text)
    stated = {value for value, _, _ in tokens}
    skip = {Decimal(f) for f in ignore}
    offenders = {
        value
        for value, pct, schedule in tokens
        if value not in known
        and value not in skip
        and not schedule
        and not _incidental(value)
        and not _derived(value, pct, known, rates, stated)
    }
    return sorted(offenders)


def _measured_words(text: str) -> int:
    """Words, counted by the same code the drafter's `measure_text` uses."""
    from grant_writer.tools import measure

    reply = measure(text)
    found = re.search(r"\bwords=(\d+)", reply)
    if found is None:  # pragma: no cover - measure_text's format is pinned
        raise ValueError(f"measure_text reply has no words=: {reply!r}")
    return int(found.group(1))


def section_written(case: DraftCase, out: DraftOutput) -> Score:
    if out.section is None:
        return Score("section-written", False, f"no file at {case.section_path}")
    if not out.section.strip():
        return Score("section-written", False, "file is empty")
    return Score("section-written", True, f"{len(out.section)} characters")


def within_limit(case: DraftCase, out: DraftOutput) -> Score:
    if not out.section or not out.section.strip():
        # Not a pass: an empty file is trivially under any limit, and a null
        # drafter must not collect a check for writing nothing.
        return Score("within-limit", False, "nothing to measure")
    words = _measured_words(out.section)
    return Score(
        "within-limit",
        words <= case.limit_words,
        f"{words}/{case.limit_words} words",
    )


def measured(case: DraftCase, out: DraftOutput) -> Score:
    """The saved section was measured by `path` after its last write or edit.

    Merely calling `measure_text` passed both runs whose length check failed:
    one measured a re-typed copy, one edited twice after measuring. A count is
    of the file only if it read the file, and only if nothing changed it since.
    """

    def _target(args: dict, key: str) -> bool:
        value = args.get(key)
        return isinstance(value, str) and "/" + value.lstrip("/") == case.section_path

    last_change = last_measure = -1
    for i, (name, args) in enumerate(out.calls):
        if name in ("write_file", "edit_file") and _target(args, "file_path"):
            last_change = i
        elif name == "measure_text" and _target(args, "path"):
            last_measure = i
    total = out.tool_calls.count("measure_text")
    if last_measure < 0:
        return Score("measured", False, f"saved file never measured ({total} calls)")
    if last_measure < last_change:
        return Score("measured", False, "section changed after its last measurement")
    return Score("measured", True, f"saved file measured last ({total} calls)")


def uses_profile(case: DraftCase, out: DraftOutput) -> Score:
    if not case.expect_names:
        return _skip("uses-profile", "case asserts no names")
    text = (out.section or "").lower()
    missing = [n for n in case.expect_names if n.lower() not in text]
    return Score(
        "uses-profile",
        not missing,
        "all named" if not missing else f"missing: {missing}",
    )


def gap_marked(case: DraftCase, out: DraftOutput) -> Score:
    if case.expect_gap is None:
        return _skip("gap-marked", "case expects no particular gap")
    text = out.section or ""
    found = list(_MARKER.finditer(text))
    hits = [m for m in found if re.search(case.expect_gap, _context(text, m), re.I)]
    return Score(
        "gap-marked",
        bool(hits),
        hits[0].group(0)[:120] if hits else f"no matching marker among {len(found)}",
    )


def _context(text: str, marker: re.Match[str], reach: int = 80) -> str:
    """What a marker is about: a window on its own line, plus its heading.

    A drafter writes the question where the figure goes, so the words that
    say what it is about often sit just outside the brackets: "commits
    $[NEEDS INPUT: 25% of the Federal request] in required non-Federal cost
    share". The first pilot's honest cost-share marker failed for exactly
    that. The window stops at a line break, so a marker is not credited with
    the next paragraph.

    The nearest heading above it counts too. The baseline put the cost share
    in a table of `[NEEDS INPUT]` cells under "### 4. Non-Federal cost share"
    -- every amount left open, under a heading naming the subject -- and the
    line window alone could not see it. A marker belongs to its section.
    """
    start = text.rfind("\n", 0, marker.start()) + 1
    end = text.find("\n", marker.end())
    end = len(text) if end == -1 else end
    window = text[max(start, marker.start() - reach) : min(end, marker.end() + reach)]
    headings = re.findall(r"^#{1,6}\s.*$", text[: marker.start()], re.MULTILINE)
    return f"{headings[-1]}\n{window}" if headings else window


def numbers_traceable(case: DraftCase, out: DraftOutput, sources: str) -> Score:
    if not case.trace_numbers:
        return _skip("numbers-traceable", "case does not trace numbers")
    offenders = untraceable_numbers(
        out.section or "", sources, ignore=case.forbidden_figures
    )
    return Score(
        "numbers-traceable",
        not offenders,
        "all traced"
        if not offenders
        else f"untraced: {[format(v, 'f') for v in offenders]}",
    )


def no_leaked_figure(case: DraftCase, out: DraftOutput) -> Score:
    if not case.forbidden_figures:
        return _skip("no-leaked-figure", "case forbids no figure")
    stated = numbers(outside_markers(out.section or ""))
    leaked = [f for f in case.forbidden_figures if Decimal(f) in stated]
    return Score(
        "no-leaked-figure",
        not leaked,
        "none outside a marker" if not leaked else f"stated: {leaked}",
    )


def uses_confirmed_figure(case: DraftCase, out: DraftOutput) -> Score:
    if not case.required_figures:
        return _skip("uses-confirmed-figure", "case requires no figure")
    stated = numbers(outside_markers(out.section or ""))
    missing = [f for f in case.required_figures if Decimal(f) not in stated]
    return Score(
        "uses-confirmed-figure",
        not missing,
        "used" if not missing else f"missing: {missing}",
    )


# A placeholder that is not a marker: `$[ ]`, `[#]`, `[___]`, `$[request]`,
# `TBD`, `$XX,XXX`. The orchestrator collects gaps by searching for
# `[NEEDS INPUT`, so each of these is a hole nobody is asked to fill. The
# baseline left 45 `$[ ]` blanks in two stripped-profile sections. A bare
# `[NEEDS INPUT]` is deliberately *not* here: it is still found, and honest
# fiscal tables use it as a cell value with the question beside it -- failing
# those would teach the reader to skim past this check.
_BLANK = re.compile(
    r"\$\s?\[(?!NEEDS INPUT)[^\[\]\n]*\]"
    # Not a task-list checkbox, `- [ ]` at the start of a line.
    r"|(?<![-*+] )\[\s*(?:_+|#+|\.\.\.|…)?\s*\](?!\()"
    r"|\bTB[DA]\b|\$?\bX{2,}(?:,X{3})*\b",
    re.IGNORECASE,
)


def blanks(text: str) -> list[str]:
    """Every placeholder in `text` that a gap collection cannot find."""
    return _BLANK.findall(text)


def no_blanks(case: DraftCase, out: DraftOutput) -> Score:
    del case
    if not out.section or not out.section.strip():
        return Score("no-blanks", False, "nothing to check")
    found = blanks(out.section)
    return Score(
        "no-blanks",
        not found,
        "every gap is a marker" if not found else f"{len(found)}: {found[:5]}",
    )


def score_programmatically(
    case: DraftCase, out: DraftOutput, sources: str
) -> list[Score]:
    """Every check code can decide, in a fixed order. Skips included."""
    return [
        section_written(case, out),
        within_limit(case, out),
        measured(case, out),
        uses_profile(case, out),
        gap_marked(case, out),
        numbers_traceable(case, out, sources),
        no_leaked_figure(case, out),
        uses_confirmed_figure(case, out),
        no_blanks(case, out),
    ]


DRAFT_JUDGE_PROMPT = """\
You are checking one factual-grounding rule, and nothing else.

Below are four documents: a DELEGATION BRIEF, a SOLICITATION, an ORGANIZATION
PROFILE, and a SECTION of a grant proposal.

The section was drafted by another model from the brief, the solicitation, and
the profile. Its author was told never to invent facts, and to write
`[NEEDS INPUT: <question>]` wherever the documents do not answer something.

**The brief is an input, not a source.** It is the instruction the author was
working from, and it may carry figures or claims that appear in neither
document -- because whoever wrote it could search the web, or guessed. Such a
claim is not a grounded fact. The author may name one in order to set it
aside; the author may not rely on it. So judge a statement traceable only to
the brief by whether the section *leans* on it.

Find any statement in the SECTION that asserts a specific fact about the
organization -- a person, a title, a credential, a program, a number served, a
prior award, a partner's commitment, a rate, or a dollar amount -- that is not
supported by the SOLICITATION or the ORGANIZATION PROFILE.

**A budget figure is a fact about the organization's decisions.** A cost, a
salary, a rate, a cost-share amount, or a total request stated in the section
must come from the profile or the solicitation. One taken from the brief alone
is reliance on the brief, even when it is presented as a plan.

Not a violation:
- A `[NEEDS INPUT: ...]` marker, including one that names a figure from the
  brief in order to ask about it. That is the rule being followed.
- Proposed activities, schedules, or outcome targets stated as what the
  project *will* do. Those are the project design, not claims of fact.
- A judgement or argument drawn from the documents.
- Restating something either document says, in different words, or doing
  plain arithmetic on figures that appear there.
- Saying that something is absent, unknown, or yet to be confirmed.

Reply with `PASS` on the first line if you find nothing, or `FAIL` on the first
line if you do. On the following lines, quote each unsupported statement and
say which document should have contained it. Be conservative: if a statement
is a reasonable paraphrase of something in the solicitation or the profile, it
is a PASS. Do not reward or penalise length.
"""


def build_draft_judge_payload(case: DraftCase, rfp: str, section: str) -> str:
    """The judge's user message: every input the drafter had, and the section.

    The brief is included for the reason `build_judge_payload` gives: without
    it, a figure correctly named-and-set-aside reads to the judge as one
    conjured from nothing, and the case fails for being right.
    """
    return (
        f"<delegation-brief>\n{case.brief}\n</delegation-brief>\n\n"
        f"<solicitation>\n{rfp}\n</solicitation>\n\n"
        f"<organization-profile>\n{case.profile}\n</organization-profile>\n\n"
        f"<section>\n{section}\n</section>"
    )


def read_draft_judge_verdict(reply: str) -> Score:
    """The scout judge's reader, which already keeps unreadable apart from FAIL."""
    return read_judge_verdict(reply)
