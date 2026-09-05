"""Run the scout eval as a LangSmith experiment over the pushed dataset.

    uv run python -m evals.evaluate_scout                 # every row, with the judge
    uv run python -m evals.evaluate_scout --no-judge      # programmatic scorers only
    uv run python -m evals.evaluate_scout --prefix opus-scout
    uv run python -m evals.evaluate_scout --dataset scratch

**This needs both credentials and spends both kinds of budget.** It calls a real
model per row (`ANTHROPIC_API_KEY`, money) and reads a dataset and writes an
experiment to a real workspace (`LANGSMITH_API_KEY`). Like everything else in
`evals/` it is not collected by `pytest tests/` and is not run in CI. The pure
half is, in `tests/test_evals.py`.

## What this adds over `run_scout`

The same model call, on the same prompt, scored by the same scorers -- imported
from `run_scout` and `scorers` rather than re-spelled, so the two runners cannot
drift into measuring different things. What changes is the shape of the answer.

`run_scout` iterates `scout_cases.CASES` in this process and posts each verdict
as *feedback* on a trace it opened. That is readable one run at a time and
comparable across runs only by eye. This runs the same work as an **experiment**
over the dataset `push_dataset` mirrors, so two runs are a table: sonnet against
opus, or `SCOUT_PROMPT` before and after an edit, on rows that are pinned to be
identical because only one end holds a pen.

The dataset is therefore the case list here, and that is a real difference in
behaviour rather than a detail: editing `scout_cases.py` changes what
`run_scout` measures immediately and changes what this measures only after a
push. Nothing warns you. `push_dataset --dry-run` is how you ask.

## Why the evaluators are local callables

LangSmith can host an evaluator and run it automatically on every experiment
against a dataset. Not these. `evals/scorers.py` imports `parse_scored_markdown`
and `untraceable_citations` from `grant_writer.opportunities`, and an uploaded
evaluator runs in a sandbox with no such package -- so hosting them means
reimplementing the parser beside the copy the product uses, which is the two-
copies drift the scorers exist to avoid. The whole argument for scoring this
eval with code is that the code is already written and already tested.

## What does *not* disarm this module

Invariant 19's tracing switch does not. `evaluate()` opens its own
`tracing_context(enabled=True)` whenever `upload_results` is on, so unlike
`run_scout.post_scores` -- which gates its writes on `tracing_is_enabled()` --
there is no environment variable here that turns the writing off. What keeps
this inert under `pytest` is the credential `tests/conftest.py` blanks and the
guard in `main` that reads it, and nothing else. A `tracing_is_enabled()` check
would read like a second safety and be none.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from typing import TYPE_CHECKING, Any

from langsmith import evaluate
from langsmith.evaluation import EvaluationResult, EvaluationResults

from evals.push_dataset import _INPUT_FIELDS, DATASET_NAME, case_from_example
from evals.run_scout import ask_judge, ask_scout
from evals.scorers import JUDGE_PROMPT, Score, posting_scores, score_programmatically
from grant_writer.config import COMPLIANCE_MODEL, DISCOVERY_MODEL
from grant_writer.prompts import SCOUT_PROMPT

if TYPE_CHECKING:
    from collections.abc import Iterable

    from langsmith.schemas import Example, Run

    from evals.scout_cases import ScoutCase


def scout_target(inputs: dict[str, Any]) -> dict[str, Any]:
    """The thing under test: one dataset row in, the scoring file out.

    `**inputs` rather than three named arguments, so `push_dataset._INPUT_FIELDS`
    is the single contract on both ends -- the mirror writes those three keys and
    `ask_scout` takes exactly those three keywords.

    The splat is checked rather than trusted, and one name is the reason.
    `ask_scout` also takes `config`, which `run_scout` uses to label its calls,
    so a row carrying a `config` key would bind to it and be handed to
    `.invoke()` as a runtime configuration -- a hand-edited dataset row
    steering the model call it is supposed to be an input to. Every other
    stray field raises `TypeError` on its own; that one would not, and the
    check is what makes the whole rule statable in one sentence.
    """
    stray = sorted(set(inputs) - set(_INPUT_FIELDS))
    if stray:
        raise TypeError(f"dataset row carries non-input field(s): {stray}")
    return {"output": ask_scout(**inputs)}


def _result(score: Score) -> EvaluationResult:
    """One `Score` as the feedback row it becomes."""
    return EvaluationResult(
        key=score.name, score=float(score.passed), comment=score.detail or None
    )


def _harness_error(where: str, exc: Exception) -> EvaluationResult:
    """Say that the eval broke, in a key no scorer answers to.

    An evaluator that raises is not silent, but it reports badly. LangSmith
    emits one error result per feedback key it infers from the function's
    source; it infers them from literal `{"key": "..."}` dicts, and finding
    none -- both evaluators here build their results in a comprehension -- it
    falls back to the function's own name. So an uncaught exception posts a
    score-less row keyed `programmatic_scores`, on a table whose other columns
    are named for scorers, with nothing on it saying which row broke or how.

    Catching it buys three things that row does not have: a key that is
    obviously not a verdict, the exception type, and which half of the harness
    raised. The rule underneath is `read_judge_verdict`'s -- an eval's own
    malfunction must not arrive looking like a measurement -- and the failure
    here is subtler than silence, which is why it is worth the four lines.
    """
    return EvaluationResult(
        key="harness-error",
        score=0.0,
        comment=f"{where}: {type(exc).__name__}: {exc}",
    )


def _scorable(run: Run, example: Example | None) -> tuple[ScoutCase, str] | None:
    """The row as the scorers want it, or `None` when there is nothing to score.

    `evaluate` catches every exception the target raises and logs it, so a rate
    limit or a dropped connection does not fail the row -- it arrives here as
    `run.error` set and no output. Scored anyway, that posts `parses: 0.0` and a
    row of zeros beside it: a dead API rendered as a prompt regression, with a
    number attached. The caller turns `None` into "post nothing", which is the
    same answer a skipped scorer gets and for the same reason.

    `example` is optional because the SDK's evaluator contract is: an evaluator
    attached to a *project* rather than to a dataset is run on live traces and
    handed no reference row. These two cannot work that way -- the case they
    score against is rebuilt from the row -- so the absence is answered here
    rather than reaching `case_from_example` as an attribute error on whoever
    attaches one to a project and waits for the first result.

    Both `Example` documents are `dict | None` on the schema, so they are
    narrowed here rather than at `case_from_example`, which indexes them by name
    and would otherwise have to grow an opinion about missing rows. The spelling
    matches `push_dataset.fetch`, so the mirror and the evaluator read a row the
    same way.
    """
    output = (run.outputs or {}).get("output")
    if example is None or run.error:
        return None
    if not isinstance(output, str) or not output.strip():
        return None
    case = case_from_example(
        dict(example.inputs or {}),
        dict(example.outputs or {}),
        dict(example.metadata or {}),
    )
    return case, output


def programmatic_scores(run: Run, example: Example | None) -> EvaluationResults:
    """Every non-model scorer, as one batch of feedback rows.

    One evaluator rather than seven, because `score_programmatically` is the
    scoring path `tests/test_evals.py` covers and seven wrappers would be seven
    more places to disagree with it. Each `Score` still carries its own key, so
    the experiment table still has a column per scorer.

    Skipped scorers are omitted rather than posted as passes -- `posting_scores`
    holds that rule for this file and for `run_scout` both. `{"results": []}` is
    the only way to say "nothing to post": `None` and `{}` are rejected by the
    SDK as malformed, so an empty batch has to be spelled, not defaulted into.
    """
    try:
        scorable = _scorable(run, example)
        if scorable is None:
            return {"results": []}
        case, output = scorable
        scores = posting_scores(score_programmatically(case, output))
    except Exception as exc:  # noqa: BLE001 - a broken eval is not a verdict
        return {"results": [_harness_error("programmatic", exc)]}
    return {"results": [_result(score) for score in scores]}


def grounding_judge(run: Run, example: Example | None) -> EvaluationResults:
    """The one question code cannot answer: is every claim in the two documents.

    Costs a second model call per row, which is why `--no-judge` exists. An
    unreadable reply comes back from `ask_judge` already marked skipped, so it
    leaves through the same filter a declining scorer does and never reaches the
    table as a pass.
    """
    try:
        scorable = _scorable(run, example)
        if scorable is None:
            return {"results": []}
        case, output = scorable
        scores = posting_scores([ask_judge(case, output)])
    except Exception as exc:  # noqa: BLE001 - a broken eval is not a verdict
        return {"results": [_harness_error("judge", exc)]}
    return {"results": [_result(score) for score in scores]}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def experiment_metadata(*, dataset: str, judge: bool) -> dict[str, Any]:
    """What the experiment has to record about itself to be worth comparing to.

    The prompt digests are the load-bearing entries. The dataset deliberately
    holds no copy of `SCOUT_PROMPT` -- it holds the fixtures, and the prompt is
    the thing under test -- so the experiment is the only place the identity of
    what was measured can live. Without it, a row of scores that moved between
    two runs cannot be attributed to the prompt edit that moved them, and the
    second experiment has nothing to be a regression *from*.

    Truncated to twelve hex characters because this is a label a human compares
    at a glance, not a checksum defending against anybody.

    `dataset` is threaded in rather than read from `DATASET_NAME`, because
    `--dataset` exists: a constant here labels every scratch run as though it
    had measured the mirror. The key is `dataset` and deliberately not
    `mirror_source`, which `push_dataset` already writes on every example with
    a different meaning -- the file the fixtures come from, and the marker
    `assert_ours` refuses a foreign dataset by.
    """
    return {
        "eval": "scout-prompt",
        "dataset": dataset,
        "scout_model": DISCOVERY_MODEL,
        "scout_prompt_sha": _sha(SCOUT_PROMPT),
        "judge": judge,
        "judge_model": COMPLIANCE_MODEL if judge else None,
        "judge_prompt_sha": _sha(JUDGE_PROMPT) if judge else None,
    }


def render(rows: Iterable[Any], *, out: Any = None) -> int:
    """Print a per-row table. Returns the number of real failures.

    Deliberately shaped like `run_scout._render`: the same eval read two ways
    should not need the reader to learn two layouts. A row with no results is
    printed as such rather than skipped -- that is either a target that never
    answered or a case that asserted nothing, and both are worth seeing.

    `out=None` rather than `out=sys.stdout`, because a default argument is
    bound once at import and would then ignore every later redirect -- the one
    `capsys` installs included. `push_dataset._out` carries the same note, and
    the bug it describes is one this repo has already shipped once.
    """
    stream = sys.stdout if out is None else out
    failures = 0
    for row in rows:
        example = row["example"]
        key = (example.metadata or {}).get("key", str(example.id))
        print(f"\n{'=' * 78}\n{key}\n{'-' * 78}", file=stream)
        results = row["evaluation_results"]["results"]
        if not results:
            print("  · nothing posted", file=stream)
            continue
        for result in results:
            passed = bool(result.score)
            if not passed:
                failures += 1
            print(
                f"  {'✓' if passed else '✗'} {result.key:<14} {result.comment or ''}",
                file=stream,
            )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="evals.evaluate_scout",
        description="Evaluate SCOUT_PROMPT as a LangSmith experiment.",
    )
    parser.add_argument(
        "--dataset", default=DATASET_NAME, help="dataset to run against"
    )
    parser.add_argument(
        "--no-judge",
        action="store_true",
        help="skip the LLM grounding judge (one fewer model call per row)",
    )
    parser.add_argument(
        "--prefix", default="scout-prompt", help="experiment name prefix"
    )
    args = parser.parse_args()

    # Truthiness, not `is None`: `tests/conftest.py` blanks these rather than
    # popping them, and LangSmith itself skips a value that strips to nothing
    # and falls through to the next namespace -- so the `LANGCHAIN_` spelling
    # has to be asked about too, or a developer with only that one set is told
    # a key is missing on a machine where it is not.
    if not os.getenv("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY is not set; this eval calls a real model.",
            file=sys.stderr,
        )
        return 1
    if not (os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY")):
        print(
            "LANGSMITH_API_KEY is not set; this reads a dataset and writes an "
            "experiment to a real workspace.",
            file=sys.stderr,
        )
        return 1

    evaluators = [programmatic_scores]
    if not args.no_judge:
        evaluators.append(grounding_judge)

    results = evaluate(
        scout_target,
        data=args.dataset,
        evaluators=evaluators,
        experiment_prefix=args.prefix,
        metadata=experiment_metadata(dataset=args.dataset, judge=not args.no_judge),
        # 0 is the SDK's default too, so this pins the value rather than
        # choosing a new one -- and pinning is the point: a flip upstream would
        # otherwise parallelise a billed run with no edit here. It means serial
        # across rows. A row's own two calls are sequential either way, the
        # judge needing the scout's answer.
        max_concurrency=0,
    )
    failures = render(results)
    print(f"\n{'=' * 78}\n{failures} failing check(s). {results.experiment_name}")
    # Zero either way, exactly as `run_scout.main` returns zero: this is a
    # measurement, and a non-zero exit invites wiring it into a pipeline that
    # then blocks on a model's mood.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
