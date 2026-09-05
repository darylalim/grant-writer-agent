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
import uuid
from dataclasses import asdict
from typing import TYPE_CHECKING

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langsmith.run_helpers import trace
from langsmith.run_trees import RunTree
from langsmith.utils import tracing_is_enabled

from evals.scorers import (
    JUDGE_PROMPT,
    Score,
    build_judge_payload,
    feedback_fields,
    posting_scores,
    read_judge_verdict,
    score_programmatically,
)
from evals.scout_cases import CASES, ScoutCase
from grant_writer.config import COMPLIANCE_MODEL, DISCOVERY_MODEL, build_model
from grant_writer.prompts import SCOUT_PROMPT

if TYPE_CHECKING:  # the client now arrives on the run; see `post_scores`
    from langsmith import Client


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


def ask_scout(
    *,
    brief: str,
    candidate: str,
    profile: str,
    config: RunnableConfig | None = None,
) -> str:
    """One scout call, and the only place its payload is spelled out.

    Keyword-only, and named for `push_dataset._INPUT_FIELDS` rather than for a
    `ScoutCase`, because there are now two callers and only one of them holds a
    case: `evaluate_scout.scout_target` is handed a dataset row as a plain dict
    and splats it straight in. A second copy of this f-string is the drift this
    repo keeps writing tests against -- two runs would report confidently on
    different prompts, and nothing would say which one you were reading.

    The brief carries the harness override that makes this work at all: the
    scout is told in `SCOUT_PROMPT` to write the file and report on it, and
    `scout_cases._PLAIN_BRIEF` countermands that with "reply with the file
    content itself". Passing the brief through rather than reconstructing it is
    what keeps the dataset able to change that instruction.
    """
    scout = build_model(DISCOVERY_MODEL)
    payload = (
        f"{brief}\n\n"
        f"<opportunity-file>\n{candidate}\n</opportunity-file>\n\n"
        f"<org-profile-file>\n{profile}\n</org-profile-file>"
    )
    return _text(
        scout.invoke(
            [SystemMessage(SCOUT_PROMPT), HumanMessage(payload)], config=config
        )
    )


def ask_judge(
    case: ScoutCase, output: str, *, config: RunnableConfig | None = None
) -> Score:
    """One grounding-judge call, returned already read.

    `read_judge_verdict` is applied here rather than by the caller so that the
    judge's own malfunction cannot reach a caller as a scout result: an
    unreadable reply comes back `skipped`, and `posting_scores` drops it. Split
    across two call sites, one of them would eventually read the reply itself
    and the skip would quietly become a pass.
    """
    grader = build_model(COMPLIANCE_MODEL)
    verdict = _text(
        grader.invoke(
            [
                SystemMessage(JUDGE_PROMPT),
                HumanMessage(build_judge_payload(case, output)),
            ],
            config=config,
        )
    )
    return read_judge_verdict(verdict)


def call_config(role: str, case: ScoutCase, model_spec: str) -> RunnableConfig:
    """The labels one model call carries into LangSmith.

    A function rather than a literal at each call site so it can be checked
    without a credential. `tests/test_evals.py` asserts this shape offline,
    which is the only cover this module gets: running it needs a real key by
    design, so nothing else here is reachable from the suite.

    Typed `RunnableConfig` rather than `dict` because that is what
    `BaseChatModel.invoke` accepts, and it is a `TypedDict` -- so a key it does
    not know is rejected here rather than accepted and dropped in silence,
    which is the same hazard `config.trace_config` centralises for the graphs.
    """
    return {
        "run_name": role,
        "tags": ["eval", "scout-prompt", role, f"case:{case.key}"],
        "metadata": {"case": case.key, "role": role, "model": model_spec},
    }


_SESSION_IDS: dict[str, uuid.UUID | None] = {}


def session_id(client: Client, project_name: str | None) -> uuid.UUID | None:
    """The project uuid `create_feedback` wants, which a `RunTree` lacks.

    Posting feedback against a `run_id` alone is deprecated and warns on every
    call that it "will stop working in a future release" -- but the run object
    `trace()` yields carries only `session_name`, so the uuid has to be looked
    up. Cached per name for the life of the process: a project does not get a
    new id under a running eval, and four cases would otherwise be four extra
    round trips to learn the same answer.
    """
    if not project_name:
        return None
    if project_name not in _SESSION_IDS:
        _SESSION_IDS[project_name] = client.read_project(project_name=project_name).id
    return _SESSION_IDS[project_name]


def _unrecorded(run: RunTree, names: list[str], exc: Exception) -> None:
    """Say on stderr which case's scores were lost, and which they were.

    Named by `run.name`, which is already `scout:<case key>`, so the line is
    attributable without `post_scores` having to take the case.
    """
    print(
        f"  ({run.name}: feedback not recorded for "
        f"{', '.join(repr(name) for name in names)}: "
        f"{type(exc).__name__}: {exc})",
        file=sys.stderr,
    )


def post_scores(run: RunTree | None, scores: list[Score]) -> None:
    """Attach each scorer's verdict to the case's run as LangSmith feedback.

    Gated on `tracing_is_enabled()`, which is the switch invariant 19 forces
    off across all four of its spellings -- so the one write this directory
    makes to a LangSmith workspace is disarmed by the same guard that keeps
    the suite offline, rather than by a second rule that could disagree with
    it. `trace()` hands back a real `RunTree` even with tracing off, so the
    run being non-None is not on its own permission to post.

    A failure to post is printed and swallowed: the eval is a measurement and
    never a gate, and the model output already bought is not worth losing to
    a dropped connection on the way to recording a score.

    **A skipped scorer posts nothing, rather than posting a pass.** A case
    that declines to assert on a dimension has not passed it, and a `1.0`
    sitting where "not checked" belongs is the collapse invariant 14 forbids
    for `fit_percent`: averaged back, a run that asserted almost nothing reads
    like one that asserted everything and was right.
    """
    if run is None or not tracing_is_enabled():
        return

    posting = posting_scores(scores)
    if not posting:
        return

    try:
        # The run's own client, never a fresh one. `Client()` re-resolves
        # endpoint and key from the environment at construction, so a per-case
        # client can target a different workspace than the one that created the
        # run it attaches to -- and the disagreement is silent, the feedback
        # simply absent from the trace someone opens. A lazy property, so it is
        # inside the try for the same reason everything else is.
        client = run.client
    except Exception as exc:  # noqa: BLE001 - a measurement, never a gate
        _unrecorded(run, [score.name for score in posting], exc)
        return

    try:
        session = session_id(client, run.session_name)
    except Exception as exc:  # noqa: BLE001 - a measurement, never a gate
        # Outside the write loop and survivable on its own: omitting the label
        # is deprecated, not broken, so a lookup that fails costs the label and
        # not the scores. Deliberately not worded "feedback not recorded" --
        # nothing was lost, and a reader grepping stderr for dropped scores
        # must not land here.
        session = None
        print(
            f"  ({run.name}: project id unresolved, posting without it: "
            f"{type(exc).__name__}: {exc})",
            file=sys.stderr,
        )

    for score in posting:
        # One try per score. Around the loop, a transient failure on score 2 of
        # 7 dropped the remaining 5 -- and a run reporting four scores where
        # seven were computed reads exactly like a run of four scorers, the
        # collapse the skipped-scorer rule above exists to prevent, arriving by
        # another route.
        key, value, comment = feedback_fields(score)
        try:
            client.create_feedback(
                run.id,
                key=key,
                score=value,
                comment=comment,
                # Both supplied on purpose. `session_id` is what the run_id-only
                # form was deprecated in favour of; `trace_id` lets the write be
                # routed without a lookup on the far side.
                trace_id=run.trace_id,
                session_id=session,
            )
        except Exception as exc:  # noqa: BLE001 - a measurement, never a gate
            _unrecorded(run, [score.name], exc)


def run_case(case: ScoutCase, *, judge: bool) -> dict:
    """Score one case. Returns a JSON-serialisable record.

    The whole case is one traced run named after the fixture, with the scout
    call and the judge call as its children. Before this a case was two
    anonymous `ChatAnthropic` roots tied neither to each other nor to the
    fixture they came from, so a project full of eval runs read as a pile --
    which is most of why a traced run went unread.

    An explicit `trace()` rather than a `@traceable` decorator, because the
    run name has to carry `case.key` and a decorator's name is fixed at import.
    The alternative is passing `langsmith_extra` at the call site, which works
    but cannot be typed: the decorator's `ParamSpec` describes the wrapped
    signature, so the extra keyword reads to a type checker as a wrong
    argument. A context manager says the same thing with the name computed
    where the case is in scope.
    """
    with trace(
        name=f"scout:{case.key}",
        run_type="chain",
        tags=["eval", "scout-prompt", f"case:{case.key}"],
        metadata={"case": case.key, "why": case.why},
        inputs={"case": case.key, "why": case.why},
    ) as run:
        output = ask_scout(
            brief=case.brief,
            candidate=case.candidate,
            profile=case.profile,
            config=call_config("scout", case, DISCOVERY_MODEL),
        )

        scores: list[Score] = score_programmatically(case, output)

        if judge:
            scores.append(
                ask_judge(
                    case, output, config=call_config("judge", case, COMPLIANCE_MODEL)
                )
            )

        post_scores(run, scores)

        record = {
            "case": case.key,
            "why": case.why,
            "output": output,
            "scores": [asdict(score) for score in scores],
        }
        # The scores, not the output: a trace already carries the scout's text
        # as the child call's own output, and repeating it here doubles the
        # payload of every case for nothing.
        run.end(outputs={"scores": record["scores"]})
        return record


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
