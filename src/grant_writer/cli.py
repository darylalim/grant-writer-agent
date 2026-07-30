"""Command line entrypoint."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from langgraph.types import Command

from grant_writer.activity import (
    DELEGATE,
    MESSAGE,
    PLAN,
    TOOL,
    approval_decisions,
    iter_activity,
    pending_action_requests,
)
from grant_writer.agent import build_agent, build_discovery_agent
from grant_writer.config import (
    Settings,
    discovery_thread_id,
    opportunities_dir,
    persistent_settings,
    require_api_keys,
)
from grant_writer.opportunities import rank_opportunities
from grant_writer.prompts import discovery_request, draft_request
from grant_writer.workspace import count_gaps, read_scored_opportunities, scan_files


def _settings_from_args(args: argparse.Namespace) -> Settings:
    """Build Settings from parsed CLI flags."""
    return persistent_settings(
        backend_profile=args.profile,
        approve_final=args.approve,
        enable_search=not args.no_search,
    )


def _print_activity(chunk: dict) -> None:
    """Render a compact trace of what the agent is doing.

    Parsing lives in `activity.iter_activity` so the UI reads the stream the
    same way; this function only decides how a terminal draws it.
    """
    for event in iter_activity(chunk):
        if event.kind == DELEGATE:
            print(f"  -> delegate to {event.label}", flush=True)
        elif event.kind == PLAN:
            print(f"  -> plan ({len(event.todos)} steps)", flush=True)
            for todo in event.todos:
                print(f"     [{todo.mark}] {todo.content}", flush=True)
        elif event.kind == TOOL:
            suffix = f" {event.detail}" if event.detail else ""
            print(f"  -> {event.label}{suffix}", flush=True)
        elif event.kind == MESSAGE:
            print(f"\n{event.detail}\n", flush=True)


def _resolve_interrupt(agent, config: dict) -> dict | None:
    """Prompt for approval on pending writes. Returns a resume Command payload
    with one decision per pending action, or None to stop."""
    requests = pending_action_requests(agent, config)
    n = max(len(requests), 1)
    print(f"\n--- approval required ({n} pending write(s)) ---", flush=True)
    for req in requests:
        req_args = req.get("args", {})
        print(
            f"  {req.get('name', '?')} -> {req_args.get('file_path', '')}", flush=True
        )
    try:
        answer = input("approve / reject / quit? ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    if answer.startswith("a"):
        return {"decisions": approval_decisions(requests, approve=True)}
    if answer.startswith("r"):
        reason = input("reason (sent back to the agent): ").strip()
        return {
            "decisions": approval_decisions(requests, approve=False, message=reason)
        }
    return None


def _run(agent, payload: dict | Command, config: dict) -> None:
    """Stream one turn, resolving interrupts until the agent finishes."""
    while True:
        interrupted = False
        for chunk in agent.stream(payload, config=config, stream_mode="updates"):
            if "__interrupt__" in chunk:
                interrupted = True
                continue
            _print_activity(chunk)
        if not interrupted:
            return
        decision = _resolve_interrupt(agent, config)
        if decision is None:
            print("Stopped. Resume later with the same --app-id.", flush=True)
            return
        payload = Command(resume=decision)


def _draft(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    missing = require_api_keys(needs_search=settings.enable_search)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}", file=sys.stderr)
        print("Copy .env.example to .env and fill it in.", file=sys.stderr)
        return 1

    rfp_path: str | None = None
    if args.rfp:
        rfp = Path(args.rfp).expanduser().resolve()
        if not rfp.is_file():
            print(f"No such RFP file: {rfp}", file=sys.stderr)
            return 1
        rfp_path = str(rfp)

    instruction = draft_request(
        args.app_id,
        rfp_path=rfp_path,
        funder=args.funder,
        notes=args.notes,
    )
    payload: dict = {"messages": [{"role": "user", "content": instruction}]}

    if args.rubric:
        rubric_path = Path(args.rubric).expanduser()
        if not rubric_path.is_file():
            print(f"No such rubric file: {rubric_path}", file=sys.stderr)
            return 1
        # RubricMiddleware activates only when `rubric` is present in state.
        payload["rubric"] = rubric_path.read_text(encoding="utf-8")
        print(f"Grading against rubric: {rubric_path}", flush=True)

    agent = build_agent(settings)
    config = {
        "configurable": {"thread_id": args.app_id},
        "recursion_limit": args.recursion_limit,
    }
    _run(agent, payload, config)
    print(f"\nDone. Output in ./applications/{args.app_id}/", flush=True)
    return 0


def _chat(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    missing = require_api_keys(needs_search=settings.enable_search)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}", file=sys.stderr)
        return 1

    agent = build_agent(settings)
    # Reusing the app id as the thread id, backed by the SQLite checkpoint the
    # CLI persists, lets a later session pick up the same todos and conversation
    # instead of starting cold.
    config = {
        "configurable": {"thread_id": args.app_id},
        "recursion_limit": args.recursion_limit,
    }
    print(f"Session '{args.app_id}'. Ctrl-D or 'exit' to quit.\n", flush=True)
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line in {"exit", "quit"}:
            return 0
        if not line:
            continue
        _run(agent, {"messages": [{"role": "user", "content": line}]}, config)


def _print_shortlist(scan_dir: Path, scan_id: str) -> None:
    """Print the ranked shortlist a scan produced.

    Reads the files rather than the transcript, and computes the ranking here,
    because the agent never stated one -- see `opportunities`. That also makes
    this the same view the UI draws, from the same two functions, rather than a
    second summary that could disagree with it.
    """
    ranked = rank_opportunities(read_scored_opportunities(scan_dir))
    if not ranked:
        print(
            f"\nNo scored candidates in ./opportunities/{scan_id}/scored/.",
            flush=True,
        )
        return

    print(f"\n--- shortlist ({len(ranked)}) ---", flush=True)
    for position, opportunity in enumerate(ranked, start=1):
        flag = " [INELIGIBLE]" if opportunity.disqualified else ""
        print(
            f"  {position:>2}. {opportunity.score_label:>8}  "
            f"{opportunity.display_title}{flag}",
            flush=True,
        )
        for warning in opportunity.warnings:
            print(f"        ! {warning}", flush=True)

    # `count_gaps` over the scan's files, which is what the UI shows beside the
    # same wording. Counting parsed gap *citations* instead was a second way to
    # arrive at one number: it misses a marker written into a `- Note:` line,
    # and the two frontends then printed different totals for the same
    # directory under captions that claimed they meant the same thing.
    gaps = count_gaps(scan_files(scan_dir))
    if gaps:
        print(
            f"\n{gaps} unresolved [NEEDS INPUT] marker(s) — facts the scout "
            "refused to invent.",
            flush=True,
        )


def _discover(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    missing = require_api_keys(needs_search=settings.enable_search)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}", file=sys.stderr)
        print("Copy .env.example to .env and fill it in.", file=sys.stderr)
        return 1

    try:
        # The same boundary the drafting side applies to `--app-id`. A scan id
        # names a directory the UI later joins onto a real path, so it is
        # checked here rather than trusted because it came from a flag.
        scan_dir = opportunities_dir(settings, args.scan_id)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    instruction = discovery_request(
        args.scan_id,
        focus=args.focus,
        agencies=args.agencies,
        notes=args.notes,
    )
    agent = build_discovery_agent(settings)
    config = {
        # Namespaced, so scanning and then drafting under the same name do not
        # resume each other -- see `config.discovery_thread_id`.
        "configurable": {"thread_id": discovery_thread_id(args.scan_id)},
        "recursion_limit": args.recursion_limit,
    }
    _run(agent, {"messages": [{"role": "user", "content": instruction}]}, config)

    _print_shortlist(scan_dir, args.scan_id)
    print(
        f"\nDone. Candidates and scoring in ./opportunities/{args.scan_id}/. "
        f"Draft one with: grant-writer draft --app-id <id> --funder <funder>",
        flush=True,
    )
    return 0


def _add_common_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile",
        choices=("local", "server"),
        default="local",
        help="local writes real files; server keeps drafts in state (default: local)",
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help="require approval before writing submission-bound files",
    )
    parser.add_argument(
        "--no-search", action="store_true", help="run without web search"
    )
    parser.add_argument(
        "--recursion-limit",
        type=int,
        default=150,
        help="max graph steps per turn (default: 150)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="grant-writer",
        description=(
            "Find funding opportunities worth applying to, and draft a "
            "proposal from the solicitation."
        ),
    )
    # Common flags live on a parent parser inherited by each subcommand, so they
    # are accepted AFTER the subcommand (e.g. `grant-writer draft --approve`),
    # which is where users -- and the README -- naturally put them.
    common = argparse.ArgumentParser(add_help=False)
    _add_common_flags(common)

    sub = parser.add_subparsers(dest="command", required=True)

    draft = sub.add_parser(
        "draft", parents=[common], help="run the full drafting pipeline"
    )
    draft.add_argument("--app-id", required=True, help="short id, e.g. nsf-aisl-2026")
    draft.add_argument("--rfp", help="path to the solicitation PDF")
    draft.add_argument("--funder", help="funder name, e.g. 'NSF'")
    draft.add_argument("--notes", help="extra context for this application")
    draft.add_argument(
        "--rubric",
        help="file of review criteria; the agent iterates until it satisfies them",
    )
    draft.set_defaults(func=_draft)

    chat = sub.add_parser(
        "chat", parents=[common], help="interactive session on an application"
    )
    chat.add_argument("--app-id", required=True)
    chat.set_defaults(func=_chat)

    # Inherits `common` like the others, including `--approve`, which is inert
    # here: `backends.discovery_permissions` returns no interrupt rule at any
    # setting, so this graph has nothing to pause on. Accepted rather than split
    # out, so every subcommand takes the same flags and the UI's one sidebar can
    # drive both graphs -- an approve toggle that silently changed meaning
    # between the two would be worse than one that does nothing.
    discover = sub.add_parser(
        "discover",
        parents=[common],
        help="find and fit-score candidate funding opportunities, before drafting",
    )
    discover.add_argument(
        "--scan-id", required=True, help="short id, e.g. rural-health-2026"
    )
    discover.add_argument(
        "--focus", help="what to search for, e.g. 'afterschool STEM in rural districts'"
    )
    discover.add_argument(
        "--agencies", help="pipe-separated grants.gov agency codes, e.g. 'USDA|NSF'"
    )
    discover.add_argument("--notes", help="extra context for this scan")
    discover.set_defaults(func=_discover)

    return parser


def main() -> int:
    args = _build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
