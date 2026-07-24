"""Command line entrypoint."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from langgraph.types import Command

from grant_writer.agent import build_agent
from grant_writer.config import Settings, require_api_keys


def _settings_from_args(args: argparse.Namespace) -> Settings:
    """Build Settings from parsed CLI flags.

    The CLI always persists its checkpoint to disk so `chat --app-id X` after
    `draft --app-id X` resumes the same conversation and todos, not just the
    files. Tests build Settings without a checkpoint_db and stay in-memory.
    """
    base = Settings(
        backend_profile=args.profile,
        approve_final=args.approve,
        enable_search=not args.no_search,
    )
    return replace(base, checkpoint_db=base.default_checkpoint_db)


def _print_activity(chunk: dict) -> None:
    """Render a compact trace of what the agent is doing."""
    for node, update in chunk.items():
        if not isinstance(update, dict):
            continue
        for message in update.get("messages", []) or []:
            for call in getattr(message, "tool_calls", None) or []:
                name = call.get("name", "?")
                args = call.get("args", {}) or {}
                if name == "task":
                    detail = args.get("subagent_type") or args.get("agent") or ""
                    print(f"  -> delegate to {detail}", flush=True)
                elif name == "write_todos":
                    todos = args.get("todos", []) or []
                    print(f"  -> plan ({len(todos)} steps)", flush=True)
                    for todo in todos:
                        mark = {"completed": "x", "in_progress": ">"}.get(
                            todo.get("status", ""), " "
                        )
                        print(f"     [{mark}] {todo.get('content', '')}", flush=True)
                else:
                    hint = (
                        args.get("file_path")
                        or args.get("path")
                        or args.get("query")
                        or ""
                    )
                    suffix = f" {hint}" if hint else ""
                    print(f"  -> {name}{suffix}", flush=True)
            text = getattr(message, "text", None)
            if callable(text):
                text = text()
            if text and node == "model" and not getattr(message, "tool_calls", None):
                print(f"\n{text}\n", flush=True)


def _pending_action_requests(agent, config: dict) -> list[dict]:
    """All tool calls awaiting approval, flattened across pending interrupts.

    HumanInTheLoopMiddleware bundles every interrupted tool call from one turn
    into a single interrupt as `action_requests`, and on resume it requires
    exactly one decision per request. So we count requests, not interrupts.
    """
    requests: list[dict] = []
    state = agent.get_state(config)
    for task in state.tasks:
        for interrupt in getattr(task, "interrupts", []) or []:
            value = interrupt.value
            if isinstance(value, dict):
                requests.extend(value.get("action_requests", []) or [])
    return requests


def _resolve_interrupt(agent, config: dict) -> dict | None:
    """Prompt for approval on pending writes. Returns a resume Command payload
    with one decision per pending action, or None to stop."""
    requests = _pending_action_requests(agent, config)
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
        return {"decisions": [{"type": "approve"} for _ in range(n)]}
    if answer.startswith("r"):
        reason = input("reason (sent back to the agent): ").strip()
        decision = {"type": "reject", "message": reason or "Rejected."}
        return {"decisions": [dict(decision) for _ in range(n)]}
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

    instruction = [
        f"Prepare a proposal in /applications/{args.app_id}/.",
    ]
    if args.rfp:
        rfp = Path(args.rfp).expanduser().resolve()
        if not rfp.is_file():
            print(f"No such RFP file: {rfp}", file=sys.stderr)
            return 1
        instruction.append(
            f"The solicitation is the PDF at {rfp}. Extract it with "
            f"extract_pdf_text and save it to /applications/{args.app_id}/rfp.md."
        )
    if args.funder:
        instruction.append(f"The funder is {args.funder}.")
    if args.notes:
        instruction.append(f"Additional context from the applicant: {args.notes}")
    instruction.append(
        "Work through the full process: requirements checklist, funder research, "
        "plan, section drafts, compliance review, then assemble the final draft."
    )

    payload: dict = {"messages": [{"role": "user", "content": " ".join(instruction)}]}

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
        description="Draft a grant proposal from a solicitation.",
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

    return parser


def main() -> int:
    args = _build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
