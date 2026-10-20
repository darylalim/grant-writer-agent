"""Check that the drafting graph delegates, by running the real graph.

    uv run python -m evals.run_draft_trajectory              # one run
    uv run python -m evals.run_draft_trajectory --repeat 3   # pass rate per check
    uv run python -m evals.run_draft_trajectory --out evals/out/trajectory.json

**This costs money and needs a live `ANTHROPIC_API_KEY`**: each run is one full
`draft` on the fixture solicitation. Not collected by `pytest tests/`, not run
in CI -- see `evals/README.md`.

## Why this exists

`run_scout` tests a prompt with the graph taken away, which is what makes its
failures readable. Delegation is the one thing that cannot be seen that way.
Since deepagents 0.7 the authored guidance on *when to hand work to a subagent*
is gone from the base prompt (CLAUDE.md, the `prompts.py` note), and the
orchestrator is supposed to `task` each section out to `section-drafter` rather
than draft in its own context. If it stops doing that, every test still passes
and the proposal still arrives -- written by the wrong agent, with the drafter's
skills never loaded.

## What it checks, and how

The real drafting graph, built by `build_agent` on a fresh root holding the
fixture profile and the six skills, runs once on `evals/fixtures/`. Its stream
is read with `activity.iter_activity`, the same parser both frontends use, so
this sees exactly what a user's activity feed would show.

That parser only ever sees the orchestrator. Without `subgraphs=True`, a
subagent's own tool calls happen inside the `task` tool and never reach the
top-level stream -- so a write under `sections/` that does reach it is the
orchestrator's own. That one fact is what makes the check programmatic rather
than a judgement.

Four checks, all code, and the counts beside them rather than only a verdict:

- `delegates-sections` -- at least one `section-drafter` call per section file
  on disk. The prompt says one section per call, so fewer calls than files
  means some section was written another way.
- `no-inline-drafting` -- the orchestrator `write_file`d no section it had not
  already read or edited. Revising a drafter's section is reported as a count
  beside the verdict and does not fail it.
- `compliance-checked` -- `compliance-checker` was called at least once.
- `plans` -- `write_todos` was used. The UI's Plan panel depends on it.

## Why revising does not count (H2b)

The first version failed any orchestrator `write_file` or `edit_file` under
`sections/`, and scored 0/3 on the first live runs. Almost all of that was
revision -- 34 to 50 `edit_file` calls per run after the drafters' pass, which
`ORCHESTRATOR_PROMPT` step 5 asks for ("Do all revision and tightening in
`sections/`"). Counting it measured the step budget (D0), not delegation.

So this was narrowed *after* seeing those results, and the narrowing is a
definition, not a discovery. A section counts as drafted inline when the
orchestrator `write_file`s a path it had not touched earlier in the run: it
wrote content it never saw from anyone else. `edit_file` is always a revision,
since it replaces text that must already be there. A `write_file` over a path
it had read or edited is a whole-file revision, counted with the edits.

The stream cannot tell a create from an overwrite any other way: the
filesystem tools overwrite silently and answer "Updated file" either way, and
the drafter's own writes never reach this stream. Read-before-write is the
proxy for "this section already existed", and it errs towards failing: an
orchestrator that overwrites a section without looking at it is drafting.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langgraph.errors import GraphRecursionError

from evals.scorers import Score, pass_rates, render_rates

if TYPE_CHECKING:
    from collections.abc import Iterable

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "evals" / "fixtures"
RFP = FIXTURES / "scmp-2026-nofo-excerpt.pdf"
PROFILE = FIXTURES / "profile-fictional.md"
FUNDER = "USDA AMS"
APP_ID = "trajectory"
#: The fixture's name in pass-rate tables. One fixture today; a second is a
#: second key here, not a second runner.
CASE = "scmp-2026"
OUT = REPO / "evals" / "out" / "trajectory"

_SECTIONS = re.compile(r"^/applications/[^/]+/sections/")
_TOUCH_TOOLS = frozenset({"read_file", "write_file", "edit_file"})


@dataclass
class Trajectory:
    """What one run's top-level stream showed, counted."""

    delegations: Counter[str] = field(default_factory=Counter)
    tools: Counter[str] = field(default_factory=Counter)
    inline_section_writes: list[str] = field(default_factory=list)
    section_revisions: list[str] = field(default_factory=list)
    plans: int = 0
    sections_on_disk: list[str] = field(default_factory=list)


def read_trajectory(
    chunks: Iterable[dict[str, Any]], sections_on_disk: list[str]
) -> Trajectory:
    """Count delegations, plans and inline writes in a stream of updates.

    Pure: chunks in, counts out, so `tests/test_evals.py` can feed it recorded
    chunks with no model. `sections_on_disk` comes from the caller because the
    files are the one fact the stream does not carry.
    """
    from grant_writer.activity import DELEGATE, PLAN, TOOL, iter_activity

    seen = Trajectory(sections_on_disk=sorted(sections_on_disk))
    touched: set[str] = set()
    for chunk in chunks:
        for event in iter_activity(chunk):
            if event.kind == DELEGATE:
                seen.delegations[event.label] += 1
            elif event.kind == PLAN:
                seen.plans += 1
            elif event.kind == TOOL:
                seen.tools[event.label] += 1
                path = event.detail
                if event.label not in _TOUCH_TOOLS or not _SECTIONS.match(path):
                    continue
                if event.label == "write_file" and path not in touched:
                    seen.inline_section_writes.append(path)
                elif event.label != "read_file":
                    seen.section_revisions.append(path)
                touched.add(path)
    return seen


def check_trajectory(seen: Trajectory) -> list[Score]:
    """The four verdicts, each carrying the counts it was decided on."""
    drafter = seen.delegations["section-drafter"]
    checker = seen.delegations["compliance-checker"]
    sections = len(seen.sections_on_disk)
    revised = f"; {len(seen.section_revisions)} revision(s) of sections/"
    return [
        Score(
            "delegates-sections",
            passed=drafter >= max(sections, 1),
            detail=f"{drafter} section-drafter call(s) for {sections} section file(s)",
        ),
        Score(
            "no-inline-drafting",
            passed=not seen.inline_section_writes,
            detail=(
                "orchestrator wrote " + ", ".join(seen.inline_section_writes)
                if seen.inline_section_writes
                else "orchestrator wrote no section itself"
            )
            + revised,
        ),
        Score(
            "compliance-checked",
            passed=checker >= 1,
            detail=f"{checker} compliance-checker call(s)",
        ),
        Score(
            "plans",
            passed=seen.plans >= 1,
            detail=f"{seen.plans} write_todos call(s)",
        ),
    ]


def _prepare_root(root: Path) -> None:
    """A root holding the fixture profile and the six skills, and nothing else.

    Copied, not symlinked: the local backend refuses any path that resolves
    outside its root, so a symlinked `skills/` stops the run before it starts.
    """
    (root / "memories" / "org").mkdir(parents=True)
    shutil.copy(PROFILE, root / "memories" / "org" / "AGENTS.md")
    shutil.copytree(REPO / "skills", root / "skills")


def run_once(root: Path, *, repeat: int, recursion_limit: int) -> dict[str, Any]:
    """One full drafting run on `root`. Returns a JSON-serialisable record.

    The graph is built on `root`, and that alone keeps the run inside it: the
    real-disk tools take their root from `Settings.root`.
    """
    from grant_writer.agent import build_agent
    from grant_writer.config import Settings
    from grant_writer.prompts import draft_request

    _prepare_root(root)

    chunks: list[dict[str, Any]] = []
    started = time.monotonic()
    error = None
    stopped = None
    try:
        agent = build_agent(Settings(root=root, enable_search=False))
        brief = draft_request(APP_ID, rfp_path=str(RFP), funder=FUNDER)
        run_config: Any = {
            "configurable": {"thread_id": f"{APP_ID}-{repeat}"},
            "recursion_limit": recursion_limit,
            "run_name": f"trajectory:{CASE}",
            "tags": ["eval", "draft-trajectory", f"case:{CASE}"],
            "metadata": {"frontend": "eval", "case": CASE, "repeat": repeat},
        }
        for chunk in agent.stream(
            {"messages": [{"role": "user", "content": brief}]},
            config=run_config,
            stream_mode="updates",
        ):
            chunks.append(chunk)
    except GraphRecursionError:
        # The product stopping, not the harness breaking: a full draft runs
        # close to the CLI's default step limit, and every call up to that
        # point is real delegation evidence. Scored, and said to have stopped.
        stopped = f"recursion limit ({recursion_limit}) reached"
    except Exception as exc:  # noqa: BLE001 - a broken run is not a verdict
        error = f"{type(exc).__name__}: {exc}"

    sections_dir = root / "applications" / APP_ID / "sections"
    sections = (
        sorted(p.name for p in sections_dir.iterdir() if p.is_file())
        if sections_dir.is_dir()
        else []
    )
    record: dict[str, Any] = {
        "case": CASE,
        "repeat": repeat,
        "root": str(root),
        "wall_s": round(time.monotonic() - started, 1),
        "stopped": stopped,
    }
    if error is not None:
        # Kept apart from the verdicts, exactly as `run_scout.attempt` does: a
        # run that raised taught nothing about delegation.
        record["error"] = error
        return record

    seen = read_trajectory(chunks, sections)
    record["counts"] = {
        "delegations": dict(seen.delegations),
        "tools": dict(seen.tools),
        "plans": seen.plans,
        "sections_on_disk": seen.sections_on_disk,
        "inline_section_writes": seen.inline_section_writes,
        "section_revisions": seen.section_revisions,
    }
    record["scores"] = [asdict(score) for score in check_trajectory(seen)]
    return record


def _render(records: list[dict[str, Any]]) -> tuple[int, int, int]:
    """Print each run. Returns (checked, failed, harness errors)."""
    checked = failed = broken = 0
    for record in records:
        print(f"\n{'=' * 78}\n{record['case']}  (repeat {record['repeat'] + 1})")
        print(f"{record['root']}  {record['wall_s']}s\n{'-' * 78}")
        if "error" in record:
            broken += 1
            print(f"  ! harness error: {record['error']}")
            continue
        for score in record["scores"]:
            checked += 1
            failed += not score["passed"]
            mark = "  ✓" if score["passed"] else "  ✗"
            print(f"{mark} {score['name']:<20} {score['detail']}")
        counts = record["counts"]
        print(f"    delegations: {counts['delegations']}")
        if record.get("stopped"):
            print(f"    stopped early: {record['stopped']}")
    return checked, failed, broken


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="evals.run_draft_trajectory",
        description="Check the drafting graph delegates, on a live run.",
    )
    parser.add_argument(
        "--repeat", type=int, default=1, help="runs (N >= 3 to compare)"
    )
    parser.add_argument("--recursion-limit", type=int, default=150)
    parser.add_argument("--out", help="write the full records as JSON")
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set; this runs a real graph.", file=sys.stderr)
        return 1

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    repo_app = REPO / "applications" / APP_ID
    existed = repo_app.exists()
    records = [
        run_once(OUT / f"{stamp}-{n}", repeat=n, recursion_limit=args.recursion_limit)
        for n in range(args.repeat)
    ]
    if not existed and repo_app.exists():
        # A real-disk tool wrote into this repository instead of the run's
        # root -- the bug building the tools from `Settings.root` fixed, back.
        print(f"WARNING: a run wrote into {repo_app}", file=sys.stderr)

    checked, failed, broken = _render(records)
    if args.repeat > 1:
        render_rates(
            pass_rates(
                [
                    (r["case"], s["name"], bool(s["passed"]))
                    for r in records
                    if "error" not in r
                    for s in r["scores"]
                ]
            )
        )
    if args.out:
        Path(args.out).write_text(json.dumps(records, indent=2), encoding="utf-8")
        print(f"\nFull records written to {args.out}")

    summary = f"{checked - failed}/{checked} checks passed."
    if broken:
        summary += f" {broken} harness error(s): the run broke, not the delegation."
    print(f"\n{'=' * 78}\n{summary}")
    # Zero either way, as `run_scout.main` returns: a measurement, not a gate.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
