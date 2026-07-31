"""Run the scout prompt eval.

    uv run python -m evals.run_scout               # all cases, with the judge
    uv run python -m evals.run_scout --no-judge    # programmatic scorers only
    uv run python -m evals.run_scout --case leaky-brief
    uv run python -m evals.run_scout --out results.json

**This costs money and needs a live `ANTHROPIC_API_KEY`.** It is not collected
by `pytest tests/` and is not run in CI, both on purpose -- see `evals/README.md`.

## What it measures, and what it does not

It evaluates `SCOUT_PROMPT` in isolation: the model is handed the two documents
inline and asked for the scoring file directly, rather than being run as a
subagent that calls `read_file` twice and `write_file` once.

That is a real limitation and worth stating plainly. It does not exercise the
scout's tool use, its permissions, or the orchestrator's delegation. What it
does exercise is the part nothing else covers at all -- whether the *judgement*
rules in the prompt hold under pressure: does silence become a question rather
than an assumption, does an ineligible candidate get an honest NONE, does a
flattering claim smuggled in through the brief get quoted as though it were in
the source.

Isolating the prompt is also what makes a failure readable. Run through the full
graph, a bad verdict could come from the prompt, a missed `read_file`, a
truncated context, or the orchestrator's own summary -- and the eval would tell
you only that something was wrong. Running the real graph is the natural second
step, once this one is passing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict

from langchain_core.messages import HumanMessage, SystemMessage

from evals.scorers import (
    JUDGE_PROMPT,
    Score,
    build_judge_payload,
    read_judge_verdict,
    score_programmatically,
)
from evals.scout_cases import CASES, ScoutCase
from grant_writer.config import COMPLIANCE_MODEL, DISCOVERY_MODEL, build_model
from grant_writer.prompts import SCOUT_PROMPT


def _text(reply: object) -> str:
    """Message content as a string, whether it arrives as text or as blocks."""
    content = getattr(reply, "content", reply)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content)


def run_case(case: ScoutCase, *, judge: bool) -> dict:
    """Score one case. Returns a JSON-serialisable record."""
    scout = build_model(DISCOVERY_MODEL)
    payload = (
        f"{case.brief}\n\n"
        f"<opportunity-file>\n{case.candidate}\n</opportunity-file>\n\n"
        f"<org-profile-file>\n{case.profile}\n</org-profile-file>"
    )
    output = _text(scout.invoke([SystemMessage(SCOUT_PROMPT), HumanMessage(payload)]))

    scores: list[Score] = score_programmatically(case, output)

    if judge:
        grader = build_model(COMPLIANCE_MODEL)
        verdict = _text(
            grader.invoke(
                [
                    SystemMessage(JUDGE_PROMPT),
                    HumanMessage(build_judge_payload(case, output)),
                ]
            )
        )
        scores.append(read_judge_verdict(verdict))

    return {
        "case": case.key,
        "why": case.why,
        "output": output,
        "scores": [asdict(score) for score in scores],
    }


def _render(records: list[dict]) -> int:
    """Print a per-case table. Returns the number of real failures."""
    failures = 0
    for record in records:
        print(f"\n{'=' * 78}\n{record['case']}\n{'-' * 78}")
        for score in record["scores"]:
            if score["skipped"]:
                mark = "  ·"
            elif score["passed"]:
                mark = "  ✓"
            else:
                mark = "  ✗"
                failures += 1
            print(f"{mark} {score['name']:<14} {score['detail']}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="evals.run_scout", description="Evaluate SCOUT_PROMPT against fixtures."
    )
    parser.add_argument("--case", help="run only this case key")
    parser.add_argument(
        "--no-judge",
        action="store_true",
        help="skip the LLM grounding judge (one fewer model call per case)",
    )
    parser.add_argument("--out", help="write the full records, including raw output")
    args = parser.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY is not set; this eval calls a real model.",
            file=sys.stderr,
        )
        return 1

    cases = [c for c in CASES if args.case in (None, c.key)]
    if not cases:
        print(
            f"No case named {args.case!r}. Known: {', '.join(c.key for c in CASES)}",
            file=sys.stderr,
        )
        return 1

    records = [run_case(case, judge=not args.no_judge) for case in cases]
    failures = _render(records)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(records, handle, indent=2)
        print(f"\nFull records written to {args.out}")

    checked = sum(1 for r in records for s in r["scores"] if not s["skipped"])
    print(f"\n{'=' * 78}\n{checked - failures}/{checked} checks passed.")
    # Zero either way: this is a measurement, not a gate. A failing scorer here
    # is a finding to read, and a non-zero exit invites someone to wire it into
    # a pipeline that then blocks on a model's mood.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
