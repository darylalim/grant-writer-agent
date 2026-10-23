"""Draft one section through the real `section-drafter`, and score it.

    uv run python -m evals.run_draft_section                  # every case, once
    uv run python -m evals.run_draft_section --repeat 3       # pass rate per check
    uv run python -m evals.run_draft_section --case tight-limit --no-judge

**This costs money and needs a live `ANTHROPIC_API_KEY`**: each case is one
drafter run (several model calls) plus, for two cases, one judge call. Not
collected by `pytest tests/`, not run in CI -- see `evals/README.md`.

## What it runs

The real drafting graph, built by `build_agent` on a fresh root per case, with
**one** thing replaced: the orchestrator's model is a script that makes a
single `task` call to `section-drafter` carrying the case's brief, then stops.
So the drafter is the production one in every respect -- its prompt, its model
and effort, `measure_text`, `/skills/`, the filesystem and its permission
rules, and the exact message the `task` tool hands it -- and the one variable
is what H3 is about: what the drafter does with a brief.

That is the trade against `run_draft_trajectory`, which runs everything and so
cannot say *which* agent got a section wrong, and against `run_scout`, which
inlines the documents and so cannot see a drafter that never opens the
profile. Here the drafter must read `rfp.md` and the profile itself, write the
file, and measure it, and the eval grades the file it leaves behind.

## Where results go

`evals/out/draft-section/<variant>/` (gitignored): `results.jsonl`, one row per
(case, repeat), written as each finishes; `traces/<case>_rep<k>.json`, the
drafter's whole exchange; `sections/`, the section files; and `errors.jsonl`
for attempts that never produced a scorable result. A rerun skips any (case,
repeat) already in `results.jsonl`, so a crash costs only the case in flight.
The layout is the one `/claude-api`'s eval report builder reads, so
`report.html` can be regenerated from it; `--out` also writes run_scout-style
records.

A refused or truncated turn is recorded with its `status` and kept out of the
pass rates: a classifier declining a biomedical section and a drafter
inventing a salary are different findings, and summing them hides both.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest import mock

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult

from evals.draft_cases import CASES, RFP_PDF, DraftCase
from evals.draft_scorers import (
    DRAFT_JUDGE_PROMPT,
    DraftOutput,
    build_draft_judge_payload,
    markers,
    read_draft_judge_verdict,
    score_programmatically,
)
from evals.run_scout import EVAL_FRONTEND, _text
from evals.scorers import Score, pass_rates, render_rates

REPO = Path(__file__).resolve().parents[1]
FLOW = REPO / "evals" / "out" / "draft-section"

#: One suffix per process, as in `run_refusals`: a batch's threads are its own
#: in LangSmith rather than merged with every earlier batch's `<app>-<repeat>`.
_BATCH = uuid.uuid4().hex[:8]

# Pinned, for run_scout's reason: the ruler must not move with what it measures.
# And not the drafter's model -- Opus 5.5 grading Opus 5.5's prose shares its
# blind spots -- nor a weaker one grading up. Fable 5.1 needs 30-day retention;
# under zero data retention its call is a 400, which lands as a harness error.
JUDGE_MODEL = "anthropic:claude-fable-5-1"
JUDGE_EFFORT = "high"

#: Declared for the report: the first binary metric is its headline.
METRICS = (
    "all-pass",
    "section-written",
    "within-limit",
    "measured",
    "uses-profile",
    "gap-marked",
    "numbers-traceable",
    "no-leaked-figure",
    "uses-confirmed-figure",
    "no-blanks",
    "no-profile-write",
    "grounded",
)

# `build_agent` looks its model builder up at call time, so swapping the
# orchestrator's model means patching a module attribute -- which is global.
# Two threads building at once would each see the other's script.
_BUILD_LOCK = threading.Lock()


class _ScriptedOrchestrator(BaseChatModel):
    """An orchestrator that delegates exactly once and then stops.

    Stateless on purpose: the reply is decided from the conversation, not
    drawn from an iterator. The first version was a `GenericFakeChatModel`
    over `iter([...])`, which passed offline and failed on the first live
    pilot with `StopIteration` on its very first turn. With tracing on,
    deepagents' `SummarizationMiddleware.wrap_model_call` is a `@traceable`,
    and posting its run serialises the model request -- model object included
    -- through langsmith's `_pydantic_json_dump`, which iterates the
    `messages` field and drains it. The suite runs with tracing off, so only a
    live run could see it. Nothing here can be consumed by being looked at.
    """

    brief: str
    #: H4 reuses this script to reach `compliance-checker` as well.
    subagent: str = "section-drafter"

    @property
    def _llm_type(self) -> str:
        return "scripted-orchestrator"

    def bind_tools(self, tools: Any, **kwargs: Any) -> _ScriptedOrchestrator:
        del tools, kwargs
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager, kwargs
        if any(isinstance(m, ToolMessage) for m in messages):
            reply = AIMessage(content="Delegated.")
        else:
            reply = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "description": self.brief,
                            "subagent_type": self.subagent,
                        },
                        "id": "h3-delegate",
                        "type": "tool_call",
                    }
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=reply)])


def _script(case: DraftCase) -> _ScriptedOrchestrator:
    return _ScriptedOrchestrator(brief=case.brief)


def prepare_root(root: Path, case: DraftCase) -> str:
    """Seed `root` the way a real run would have it; return the rfp text.

    `rfp.md` comes from the real `extract_solicitation` and
    `preload_solicitation`, so the drafter reads exactly what a frontend's
    upload would have left.
    Skills are copied, not symlinked: the local backend refuses a path that
    resolves outside its root.
    """
    from grant_writer.config import Settings
    from grant_writer.solicitation import extract_solicitation, preload_solicitation

    (root / "memories" / "org").mkdir(parents=True)
    (root / "memories" / "org" / "AGENTS.md").write_text(case.profile, "utf-8")
    shutil.copytree(REPO / "skills", root / "skills")
    text = extract_solicitation(RFP_PDF.read_bytes(), RFP_PDF.name)
    preload_solicitation(Settings(root=root), case.app_id, text, user_id=None)
    rfp = root / case.rfp_path.lstrip("/")
    if not rfp.is_file():
        raise RuntimeError(f"rfp preload did not land at {case.rfp_path}")
    return rfp.read_text(encoding="utf-8")


def _drafter_messages(
    graph: Any, config: dict[str, Any]
) -> tuple[list[Any], str | None]:
    """The drafter's own messages, from the subgraph stream. Deduplicated.

    Without `subgraphs=True` the drafter's calls run inside `task` and never
    reach the stream -- which is what `run_draft_trajectory` relies on, and
    the opposite of what this needs.
    """
    from langgraph.errors import GraphRecursionError

    seen: dict[str, Any] = {}
    stopped = None
    try:
        for namespace, chunk in graph.stream(
            {"messages": [{"role": "user", "content": "(scripted orchestrator)"}]},
            config=config,
            stream_mode="updates",
            subgraphs=True,
        ):
            if not namespace:
                continue
            for update in chunk.values():
                batch = update.get("messages") if isinstance(update, dict) else None
                if not isinstance(batch, list):
                    continue
                for message in batch:
                    key = getattr(message, "id", None) or f"anon-{len(seen)}"
                    seen.setdefault(key, message)
    except GraphRecursionError:
        stopped = f"recursion limit ({config['recursion_limit']}) reached"
    return list(seen.values()), stopped


def _usage(messages: list[Any]) -> dict[str, int]:
    """Summed usage, with cache writes counted wherever langchain files them.

    When the API splits writes by TTL, langchain-anthropic reports them as
    `ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens` and sets
    `cache_creation` to **0**. Reading only `cache_creation` counted every
    write as uncached input in the first pilot -- a zero that looked like a
    measurement. The 1h share is kept apart because it bills at 2x input, not
    the 1.25x of a 5-minute write.
    """
    total = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_creation_1h_input_tokens": 0,
    }
    for message in messages:
        usage = getattr(message, "usage_metadata", None) or {}
        details = usage.get("input_token_details") or {}
        five = details.get("ephemeral_5m_input_tokens") or 0
        hour = details.get("ephemeral_1h_input_tokens") or 0
        total["input_tokens"] += usage.get("input_tokens", 0)
        total["output_tokens"] += usage.get("output_tokens", 0)
        total["cache_read_input_tokens"] += details.get("cache_read") or 0
        total["cache_creation_input_tokens"] += (
            (details.get("cache_creation") or 0) + five + hour
        )
        total["cache_creation_1h_input_tokens"] += hour
    return total


def _trace(case: DraftCase, messages: list[Any]) -> list[dict[str, Any]]:
    """The exchange in the report's turn format. System prompt first."""
    from grant_writer.prompts import DRAFTER_PROMPT

    return turns(DRAFTER_PROMPT, case.brief, messages)


def turns(system: str, brief: str, messages: list[Any]) -> list[dict[str, Any]]:
    """`_trace` for any subagent: its prompt, its brief, then the exchange."""
    out: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": brief},
    ]
    for message in messages:
        if isinstance(message, AIMessage):
            text = _text(message)
            if text.strip():
                out.append({"role": "assistant", "content": text})
            for call in message.tool_calls:
                out.append(
                    {
                        "role": "tool_call",
                        "name": call["name"],
                        "content": json.dumps(call["args"], indent=2),
                    }
                )
        elif isinstance(message, ToolMessage):
            out.append({"role": "tool_result", "content": _text(message)})
    return out


def _expected_model() -> str:
    from grant_writer.config import DRAFTING_MODEL

    return DRAFTING_MODEL.split(":", 1)[-1]


def _judge(case: DraftCase, rfp: str, section: str) -> tuple[Score, dict[str, Any]]:
    from grant_writer.config import build_model

    reply = build_model(JUDGE_MODEL, effort=JUDGE_EFFORT).invoke(
        [
            SystemMessage(DRAFT_JUDGE_PROMPT),
            HumanMessage(build_draft_judge_payload(case, rfp, section)),
        ],
        config={
            "run_name": "judge",
            "tags": ["eval", "draft-section", "judge", f"case:{case.key}"],
            "metadata": {
                "case": case.key,
                "role": "judge",
                "model": JUDGE_MODEL,
                "effort": JUDGE_EFFORT,
                "frontend": EVAL_FRONTEND,
            },
        },
    )
    meta = {
        "judge_model": reply.response_metadata.get("model_name"),
        "judge_usage": _usage([reply]),
        "judge_reply": _text(reply),
    }
    return read_draft_judge_verdict(_text(reply)), meta


#: Canned sections with a known verdict, for `--calibrate-judge`. The usual
#: null inputs (an empty string, "I don't know") do not fit a grounding judge:
#: they assert nothing, so PASS is right for them, and the code checks are what
#: fail them. What must be shown is that the judge catches an invented fact and
#: does not punish a figure named in order to set it aside.
CALIBRATION: tuple[tuple[str, str, bool], ...] = (
    (
        "genuine-section",
        "Dana Okafor, Executive Director, holds a PhD from Kansas State University "
        "and has secured $2.3 million in USDA awards since 2015.",
        False,
    ),
    (
        "leaky-delegation",
        "The external evaluator is budgeted at $68,000, in line with comparable "
        "SCMP projects, within a total request of $735,000.",
        False,
    ),
    (
        "leaky-delegation",
        "The evaluator has not yet been contracted. [NEEDS INPUT: evaluator cost; "
        "the brief's $68,000 estimate from comparable projects is unverified] "
        "Cost share is drawn from the board's approval of up to $60,000 in cash "
        "plus staff time in kind.",
        True,
    ),
)


def calibrate_judge(rfp: str) -> int:
    """Run the judge on `CALIBRATION`; print agreement. Returns disagreements."""
    by_key = {c.key: c for c in CASES}
    wrong = 0
    for key, section, expect in CALIBRATION:
        verdict, _ = _judge(by_key[key], rfp, section)
        agrees = not verdict.skipped and verdict.passed is expect
        wrong += not agrees
        want = "PASS" if expect else "FAIL"
        got = "unreadable" if verdict.skipped else "PASS" if verdict.passed else "FAIL"
        print(f"  {'✓' if agrees else '✗'} {key:<18} want {want}, got {got}")
    return wrong


def run_case(
    case: DraftCase, *, judge: bool, repeat: int, recursion_limit: int, root: Path
) -> dict[str, Any]:
    """One drafter run on `root`, scored. Raises on a harness failure."""
    from grant_writer import agent as agent_module
    from grant_writer.config import DRAFTING_EFFORT, DRAFTING_MODEL, Settings

    rfp = prepare_root(root, case)
    with (
        _BUILD_LOCK,
        mock.patch.object(
            agent_module, "build_model", lambda spec, *, effort: _script(case)
        ),
    ):
        graph = agent_module.build_agent(Settings(root=root, enable_search=False))

    config: dict[str, Any] = {
        "configurable": {"thread_id": f"{case.app_id}-{repeat}-{_BATCH}"},
        "recursion_limit": recursion_limit,
        "run_name": f"draft-section:{case.key}",
        "tags": ["eval", "draft-section", f"case:{case.key}"],
        "metadata": {
            "frontend": EVAL_FRONTEND,
            "case": case.key,
            "repeat": repeat,
            "drafting_model": DRAFTING_MODEL,
            "drafting_effort": DRAFTING_EFFORT,
        },
    }
    started = time.monotonic()
    messages, stopped = _drafter_messages(graph, config)
    latency = round(time.monotonic() - started, 1)

    replies = [m for m in messages if isinstance(m, AIMessage)]
    if not replies:
        raise RuntimeError("the drafter produced no model reply")
    served = {m.response_metadata.get("model_name") for m in replies}
    expected = _expected_model()
    if not all(isinstance(s, str) and s.startswith(expected) for s in served):
        # A score from the wrong model measures nothing; fail loudly.
        raise RuntimeError(f"served model {sorted(map(str, served))} != {expected}")

    stop_reasons = [m.response_metadata.get("stop_reason") for m in replies]
    if "max_tokens" in stop_reasons:
        status = "truncated"
    elif stop_reasons[-1] == "refusal":
        status = "refused"
    elif stopped:
        status = "stopped"
    else:
        status = "ok"

    file = root / case.section_path.lstrip("/")
    section = file.read_text(encoding="utf-8") if file.is_file() else None
    calls = tuple((c["name"], c["args"]) for m in replies for c in m.tool_calls)
    tool_calls = tuple(name for name, _ in calls)
    out = DraftOutput(
        section=section, tool_calls=tool_calls, calls=calls, report=_text(replies[-1])
    )

    # The documents only, not the brief: the brief is an input but not a
    # source, so a figure it alone carries does not trace (DRAFT_JUDGE_PROMPT
    # draws the same line).
    sources = "\n".join((rfp, case.profile))
    scores = score_programmatically(case, out, sources)
    judge_meta: dict[str, Any] = {}
    if case.judge and judge and status == "ok" and section:
        verdict, judge_meta = _judge(case, rfp, section)
        scores.append(verdict)

    return {
        "case": case.key,
        "why": case.why,
        "repeat": repeat,
        "status": status,
        "stopped": stopped,
        "stop_reason": stop_reasons[-1],
        "model": sorted(map(str, served))[0],
        "usage": _usage(replies),
        "latency_s": latency,
        "tool_calls": len(tool_calls),
        "turns": len(replies),
        "markers": len(markers(section or "")),
        "section": section,
        "report": out.report,
        "scores": [asdict(score) for score in scores],
        "trace": _trace(case, messages),
        **judge_meta,
    }


def attempt(
    case: DraftCase,
    *,
    judge: bool,
    repeat: int,
    recursion_limit: int = 60,
) -> dict[str, Any]:
    """`run_case` on a fresh root, with a raised call kept as a harness error.

    Kept apart for `run_scout.attempt`'s reason: a record with an `error` key
    has no scores and never reaches a pass rate. A rate limit is not a
    regression.
    """
    root = Path(tempfile.mkdtemp(prefix=f"h3-{case.key}-"))
    try:
        return run_case(
            case,
            judge=judge,
            repeat=repeat,
            recursion_limit=recursion_limit,
            root=root,
        )
    except Exception as exc:  # noqa: BLE001 - a broken eval is not a verdict
        return {
            "case": case.key,
            "repeat": repeat,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def grade(record: dict[str, Any]) -> dict[str, int]:
    """The report's `grade`: 1/0 per real check, plus `all-pass` first."""
    real = {s["name"]: int(s["passed"]) for s in record["scores"] if not s["skipped"]}
    return {"all-pass": int(all(real.values())), **real}


def write_result(variant: Path, case: DraftCase, record: dict[str, Any]) -> None:
    """Append one finished attempt to `variant`, in the report's layout."""
    rep = record["repeat"]
    if "error" in record:
        with open(variant / "errors.jsonl", "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"prompt_id": case.key, "rep": rep, **record}) + "\n"
            )
        return
    (variant / "traces").mkdir(exist_ok=True)
    (variant / "traces" / f"{case.key}_rep{rep}.json").write_text(
        json.dumps(record["trace"], indent=2), encoding="utf-8"
    )
    if record["section"] is not None:
        (variant / "sections").mkdir(exist_ok=True)
        (variant / "sections" / f"{case.key}_rep{rep}.md").write_text(
            record["section"], encoding="utf-8"
        )
    row = {
        "prompt_id": case.key,
        "rep": rep,
        "prompt": case.brief,
        "tags": list(case.tags),
        "status": record["status"],
        "stop_reason": record["stop_reason"],
        "grade": grade(record),
        "explanation": {s["name"]: s["detail"] for s in record["scores"]},
        "model": record["model"],
        "usage": record["usage"],
        "latency_s": record["latency_s"],
        "tool_calls": record["tool_calls"],
        "meta": {"turns": record["turns"], "markers": record["markers"]},
    }
    for key in ("judge_model", "judge_usage"):
        if key in record:
            row[key] = record[key]
    with open(variant / "results.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def done(variant: Path) -> set[tuple[str, int]]:
    """(case, repeat) pairs already scored in `variant`, for resume."""
    path = variant / "results.jsonl"
    if not path.is_file():
        return set()
    rows = [json.loads(line) for line in path.read_text("utf-8").splitlines() if line]
    return {(row["prompt_id"], row["rep"]) for row in rows}


def rescore(variant: Path) -> list[dict[str, Any]]:
    """Re-apply the code checks to `variant`'s saved sections. No model calls.

    A grader fix should not need a paid rerun to be seen: the section files and
    traces are on disk, and every check but the judge is a pure function of
    them. The judge's verdict is carried over as recorded. Rewrites
    `results.jsonl` in place; returns minimal records for `outcomes`.
    """
    by_key = {c.key: c for c in CASES}
    path = variant / "results.jsonl"
    rows = [json.loads(line) for line in path.read_text("utf-8").splitlines() if line]
    with tempfile.TemporaryDirectory(prefix="h3-rescore-") as tmp:
        rfp = prepare_root(Path(tmp), CASES[0])
    records = []
    for row in rows:
        case = by_key[row["prompt_id"]]
        stem = f"{case.key}_rep{row['rep']}"
        section_file = variant / "sections" / f"{stem}.md"
        trace = json.loads((variant / "traces" / f"{stem}.json").read_text("utf-8"))
        out = DraftOutput(
            section=section_file.read_text("utf-8") if section_file.is_file() else None,
            tool_calls=tuple(t["name"] for t in trace if t["role"] == "tool_call"),
            calls=tuple(
                (t["name"], json.loads(t["content"]))
                for t in trace
                if t["role"] == "tool_call"
            ),
        )
        scores = score_programmatically(case, out, "\n".join((rfp, case.profile)))
        if "grounded" in row["grade"]:
            judged = row.get("explanation", {}).get("grounded", "")
            scores.append(Score("grounded", bool(row["grade"]["grounded"]), judged))
        record = {
            "case": case.key,
            "repeat": row["rep"],
            "status": row["status"],
            "scores": [asdict(score) for score in scores],
        }
        row["grade"] = grade(record)
        row["explanation"] = {s.name: s.detail for s in scores}
        records.append(record)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), "utf-8")
    return records


def outcomes(records: list[dict[str, Any]]) -> list[tuple[str, str, bool]]:
    """Real verdicts only: no skips, no harness errors, no refused/truncated turns."""
    return [
        (record["case"], score["name"], bool(score["passed"]))
        for record in records
        if "error" not in record and record["status"] == "ok"
        for score in record["scores"]
        if not score["skipped"]
    ]


def _render(records: list[dict[str, Any]]) -> None:
    for record in records:
        title = f"{record['case']}  (repeat {record['repeat'] + 1})"
        print(f"\n{'=' * 78}\n{title}\n{'-' * 78}")
        if "error" in record:
            print(f"  ! harness error: {record['error']}")
            continue
        usage = record["usage"]
        print(
            f"  status={record['status']}  {record['latency_s']}s  "
            f"turns={record['turns']}  tools={record['tool_calls']}  "
            f"markers={record['markers']}  in={usage['input_tokens']} "
            f"out={usage['output_tokens']} "
            f"cache_read={usage['cache_read_input_tokens']}"
        )
        for score in record["scores"]:
            mark = "  ·" if score["skipped"] else "  ✓" if score["passed"] else "  ✗"
            print(f"{mark} {score['name']:<22} {score['detail']}")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="evals.run_draft_section",
        description="Evaluate section-drafter on the fixture solicitation.",
    )
    parser.add_argument("--case", help="run only this case key")
    parser.add_argument("--no-judge", action="store_true", help="skip the judge")
    parser.add_argument("--repeat", type=int, default=1, help="runs per case")
    parser.add_argument("--jobs", type=int, default=3, help="cases run at once")
    parser.add_argument("--variant", default="baseline", help="baseline or vN")
    parser.add_argument(
        "--flow",
        default=str(FLOW),
        help="results directory (a pilot goes in its own, never in baseline)",
    )
    parser.add_argument("--recursion-limit", type=int, default=60)
    parser.add_argument("--out", help="also write run_scout-style records here")
    parser.add_argument(
        "--rescore",
        action="store_true",
        help="re-apply the code checks to a variant's saved sections; no model calls",
    )
    parser.add_argument(
        "--calibrate-judge",
        action="store_true",
        help="run the judge on three canned sections with known verdicts, then exit",
    )
    args = parser.parse_args()
    if args.repeat < 1 or args.jobs < 1:
        parser.error("--repeat and --jobs must be at least 1")

    if not args.rescore and not os.getenv("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY is not set; this eval calls a real model.",
            file=sys.stderr,
        )
        return 1
    if args.rescore:
        records = rescore(Path(args.flow) / args.variant)
        for record in records:
            print(f"\n{record['case']}  (repeat {record['repeat'] + 1})")
            for score in record["scores"]:
                mark = "·" if score["skipped"] else "✓" if score["passed"] else "✗"
                print(f"  {mark} {score['name']:<22} {score['detail'][:100]}")
        checked = outcomes(records)
        passed = sum(1 for _, _, ok in checked if ok)
        print(f"\n{passed}/{len(checked)} checks passed (rescored).")
        return 0

    if args.calibrate_judge:
        with tempfile.TemporaryDirectory(prefix="h3-calibrate-") as tmp:
            rfp = prepare_root(Path(tmp), CASES[0])
        wrong = calibrate_judge(rfp)
        print(f"\n{len(CALIBRATION) - wrong}/{len(CALIBRATION)} agree with the label.")
        return 0

    cases = [c for c in CASES if args.case in (None, c.key)]
    if not cases:
        known = ", ".join(c.key for c in CASES)
        print(f"No case named {args.case!r}. Known: {known}", file=sys.stderr)
        return 1

    flow = Path(args.flow)
    variant = flow / args.variant
    variant.mkdir(parents=True, exist_ok=True)
    state = flow / "_state.json"
    if not state.exists():
        metrics = [{"id": m, "kind": "binary"} for m in METRICS]
        state.write_text(json.dumps({"metrics": metrics}, indent=2), "utf-8")

    skip = done(variant)
    todo = [(c, n) for c in cases for n in range(args.repeat) if (c.key, n) not in skip]
    if skip:
        print(f"Resuming: {len(skip)} attempt(s) already in {variant}/results.jsonl")

    def one(item: tuple[DraftCase, int]) -> dict[str, Any]:
        case, n = item
        record = attempt(
            case,
            judge=not args.no_judge,
            repeat=n,
            recursion_limit=args.recursion_limit,
        )
        write_result(variant, case, record)
        return record

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        records = list(pool.map(one, todo))

    _render(records)
    if args.repeat > 1:
        render_rates(pass_rates(outcomes(records)))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(records, handle, indent=2)
        print(f"\nFull records written to {args.out}")

    checked = outcomes(records)
    failures = sum(1 for _, _, passed in checked if not passed)
    broken = sum(1 for r in records if "error" in r)
    other = sum(1 for r in records if "error" not in r and r["status"] != "ok")
    summary = f"{len(checked) - failures}/{len(checked)} checks passed."
    if other:
        summary += f" {other} refused/truncated/stopped turn(s), not scored."
    if broken:
        summary += f" {broken} harness error(s): the eval broke, not the drafter."
    print(f"\n{'=' * 78}\n{summary}\nResults: {variant}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
