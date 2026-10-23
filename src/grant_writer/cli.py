"""Command line entrypoint."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from langgraph.types import Command

from grant_writer.activity import (
    DECLINED,
    DELEGATE,
    MESSAGE,
    PLAN,
    TOOL,
    approval_decisions,
    iter_activity,
    pending_action_requests,
)
from grant_writer.agent import (
    build_agent,
    build_checkpointer,
    build_discovery_agent,
    build_run_ledger,
    check_database,
    setup_database,
)
from grant_writer.config import (
    LOG_LEVEL,
    PARKED_ALERT_DAYS,
    Settings,
    discovery_thread_id,
    opportunities_dir,
    persistent_settings,
    require_api_keys,
    trace_config,
    tracing_mode,
    user_thread_id,
)
from grant_writer.opportunities import rank_opportunities
from grant_writer.ops import (
    configure_logging,
    configure_tracing,
    log_event,
    parked_threads,
)
from grant_writer.prompts import discovery_request, draft_request
from grant_writer.runs import approvals_for
from grant_writer.solicitation import extract_solicitation, preload_solicitation
from grant_writer.workspace import (
    count_gaps,
    read_scored_opportunities,
    scan_files,
    unverifiable_citations,
)

logger = logging.getLogger(__name__)

# The CLI is single-user (decision 9): its thread ids and directories are the
# unprefixed ones, so `--app-id X` is thread `X` and `applications/X/` exactly as
# before per-user ids existed, and a run started in the local UI -- which passes
# the same None until sign-in supplies a user -- still resumes here. Named so the
# call sites say which tree they chose rather than passing a bare None.
CLI_USER: str | None = None


def _settings_from_args(args: argparse.Namespace) -> Settings:
    """Build Settings from parsed CLI flags."""
    return persistent_settings(
        backend_profile=args.profile,
        approve_final=args.approve,
        enable_search=not args.no_search,
    )


def _refuses_shared_database(settings: Settings, args: argparse.Namespace) -> bool:
    """Whether to refuse a turn against the deployment's database (decision 18).

    The CLI has no sign-in and unprefixed thread ids, which is right on one
    laptop. With `DATABASE_URL` set it would reach the *shared* checkpoint
    instead, where `--app-id` names any thread at all -- including a raw
    per-user one, `user:<someone>:<app>`, which `--app-id` cannot spell but
    `chat` would happily resume if it could. So it refuses unless an operator
    says otherwise, and that override is logged, never silent.

    `db-setup` does not come through here: it is the deploy step that needs
    the URL, and it reads and writes no thread.
    """
    if settings.database_url is None:
        return False
    if not args.operator:
        print(
            "DATABASE_URL is set, so this would run against the shared deployment "
            "database with no sign-in. Unset it to work locally, or pass "
            "--operator if you are operating that deployment.",
            file=sys.stderr,
        )
        return True
    logger.warning(
        "operator override: grant-writer %s against the shared database (%s)",
        args.command,
        getattr(args, "app_id", None) or getattr(args, "scan_id", None),
    )
    return False


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
        elif event.kind == DECLINED:
            who = event.label or "the agent"
            print(
                f"  !! declined by the safety classifier ({event.category}) "
                f"in {who}; its output is missing. Rephrase, or retry later.",
                flush=True,
            )
            if event.detail:
                print(f"     {event.detail}", flush=True)


def _resolve_interrupt(
    agent, config: dict, settings: Settings | None = None
) -> dict | None:
    """Prompt for approval on pending writes. Returns a resume Command payload
    with one decision per pending action, or None to stop.

    Against the shared database (an `--operator` run, decision 18) each
    decision is recorded before it is sent, as the UI records a signed-in
    one (invariant 27). On a laptop there is no one else to answer to, and no
    record is written.
    """
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
        decisions = approval_decisions(requests, approve=True)
    elif answer.startswith("r"):
        reason = input("reason (sent back to the agent): ").strip()
        decisions = approval_decisions(requests, approve=False, message=reason)
    else:
        return None
    if settings is not None and settings.database_url is not None:
        thread_id = config["configurable"]["thread_id"]
        try:
            build_run_ledger(settings).record_approvals(
                approvals_for(decisions, requests),
                user_id=CLI_USER,
                thread_id=thread_id,
                run_id=None,
                frontend="cli",
            )
        except Exception as exc:  # noqa: BLE001 - an unrecorded approval is not sent
            log_event(
                logger,
                "approval.unrecorded",
                level=logging.ERROR,
                exc=exc,
                thread_id=thread_id,
                frontend="cli",
            )
            if answer.startswith("a"):
                # An approval nobody can account for is not sent; the thread
                # stays parked and the same decision can be made again.
                print(
                    f"Not approved: the approval could not be recorded ({exc}).",
                    file=sys.stderr,
                )
                return None
    return {"decisions": decisions}


def _run(
    agent, payload: dict | Command, config: dict, settings: Settings | None = None
) -> None:
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
        decision = _resolve_interrupt(agent, config, settings)
        if decision is None:
            print("Stopped. Resume later with the same --app-id.", flush=True)
            return
        payload = Command(resume=decision)


def _draft(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    if _refuses_shared_database(settings, args):
        return 1
    missing = require_api_keys(needs_search=settings.enable_search)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}", file=sys.stderr)
        print("Copy .env.example to .env and fill it in.", file=sys.stderr)
        return 1

    # Extracted here, before the graph exists, through the same bounded parse
    # the UI uses (invariant 24): the agent has no tool that opens a PDF.
    preload: dict = {}
    if args.rfp:
        rfp = Path(args.rfp).expanduser().resolve()
        if not rfp.is_file():
            print(f"No such RFP file: {rfp}", file=sys.stderr)
            return 1
        print(f"Extracting {rfp.name}...", flush=True)
        try:
            text = extract_solicitation(rfp.read_bytes(), rfp.name)
            # The CLI is single-user (decision 9), so no user id.
            preload = preload_solicitation(settings, args.app_id, text, user_id=None)
        except (ValueError, OSError) as exc:
            print(f"Could not use {rfp.name}: {exc}", file=sys.stderr)
            return 1

    instruction = draft_request(
        args.app_id,
        solicitation=bool(args.rfp),
        funder=args.funder,
        notes=args.notes,
    )
    payload: dict = {
        "messages": [{"role": "user", "content": instruction}],
        **preload,
    }

    rubric_name: str | None = None
    if args.rubric:
        rubric_path = Path(args.rubric).expanduser()
        if not rubric_path.is_file():
            print(f"No such rubric file: {rubric_path}", file=sys.stderr)
            return 1
        # RubricMiddleware activates only when `rubric` is present in state.
        payload["rubric"] = rubric_path.read_text(encoding="utf-8")
        print(f"Grading against rubric: {rubric_path}", flush=True)
        # The name, never the path: a trace is shared, and an absolute path
        # under $HOME carries the operator's username with it. Which rubric
        # was applied is the filterable fact; where it sat is not.
        rubric_name = rubric_path.name

    agent = build_agent(settings)
    config = trace_config(
        settings,
        command="draft",
        frontend="cli",
        ref=args.app_id,
        thread_id=user_thread_id(CLI_USER, args.app_id),
        recursion_limit=args.recursion_limit,
        details={
            "app_id": args.app_id,
            "funder": args.funder,
            "rubric": rubric_name,
        },
    )
    _run(agent, payload, config, settings)
    print(f"\nDone. Output in ./applications/{args.app_id}/", flush=True)
    return 0


def _chat(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    if _refuses_shared_database(settings, args):
        return 1
    missing = require_api_keys(needs_search=settings.enable_search)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}", file=sys.stderr)
        return 1

    agent = build_agent(settings)
    # Reusing the app id as the thread id, backed by the SQLite checkpoint the
    # CLI persists, lets a later session pick up the same todos and conversation
    # instead of starting cold.
    #
    # `command="chat"` is what separates these turns from the `draft` run that
    # opened the same thread. They share `thread_id` by design, so before this
    # the two were one undifferentiated stream of identically-named root runs
    # and telling the opening brief from a follow-up meant opening each.
    config = trace_config(
        settings,
        command="chat",
        frontend="cli",
        ref=args.app_id,
        thread_id=user_thread_id(CLI_USER, args.app_id),
        recursion_limit=args.recursion_limit,
        details={"app_id": args.app_id},
    )
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
        _run(agent, {"messages": [{"role": "user", "content": line}]}, config, settings)


def _print_shortlist(scan_dir: Path, scan_id: str, settings: Settings) -> None:
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
    # Every citation checked against the file it names. The scout has no tools,
    # so anything it quotes that is not in the archive came from the
    # orchestrator's delegation -- true, most likely, and unverifiable by
    # anyone reading the scan afterwards.
    unverifiable = unverifiable_citations(scan_dir, ranked, settings.memory_path)
    if unverifiable:
        count = sum(len(items) for items in unverifiable.values())
        print(
            f"\n{count} citation(s) could not be found in the source they cite:",
            flush=True,
        )
        for key, items in unverifiable.items():
            for citation in items:
                print(f"  {key} [{citation.source}]: {citation.text[:70]}…", flush=True)

    gaps = count_gaps(scan_files(scan_dir))
    if gaps:
        print(
            f"\n{gaps} unresolved [NEEDS INPUT] marker(s) — facts the scout "
            "refused to invent.",
            flush=True,
        )


def _db_setup(args: argparse.Namespace) -> int:
    """Create or migrate the Postgres tables named by ``DATABASE_URL``.

    A subcommand rather than a side effect of building a graph: migrations are
    a deploy step, and every replica of a web server running them on startup
    would race each other. Needs no model key and bills nothing.
    """
    settings = persistent_settings()
    if settings.database_url is None:
        print(
            "DATABASE_URL is not set; there is no database to set up.", file=sys.stderr
        )
        return 1
    setup_database(settings)
    print("Checkpoint, Store and run tables are up to date.")
    return 0


def _healthcheck(args: argparse.Namespace) -> int:
    """Readiness: exit 0 when the database this process would use answers.

    For a host's exec probe (E5); liveness is Streamlit's own
    `/_stcore/health`. Without `DATABASE_URL` there is no database to wait
    on and the check passes, so the same probe works on a laptop.
    """
    settings = persistent_settings()
    try:
        check_database(settings)
    except Exception as exc:  # noqa: BLE001 - any failure is "not ready"
        log_event(logger, "health.unready", level=logging.ERROR, exc=exc)
        print(f"not ready: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("ready")
    return 0


def _ops_check(args: argparse.Namespace) -> int:
    """Report what needs an operator's attention; exit 1 if anything does.

    For a host's scheduled job (E7 alerts). Reads only counts and ids: the
    run table grouped by outcome and error type, and the checkpoint's parked
    threads. Limit hits and the rest arrive as log events, which a host
    alerts on from its log collector rather than from this.
    """
    settings = persistent_settings()
    now = time.time()
    since = now - args.hours * 3600
    counts = build_run_ledger(settings).summary(since=since)
    total = sum(n for _outcome, _error, n in counts)

    def share(outcome: str) -> float:
        hits = sum(n for o, _error, n in counts if o == outcome)
        return hits / total if total else 0.0

    by_error: dict[str, int] = {}
    for _outcome, error_type, n in counts:
        if error_type:
            by_error[error_type] = by_error.get(error_type, 0) + n
    parked = parked_threads(
        build_checkpointer(settings).list(None),
        older_than_s=args.parked_days * 86400,
        now=now,
    )
    alerts = []
    if total >= args.min_runs and share("failed") > args.max_failed_share:
        alerts.append(f"failed runs: {share('failed'):.0%} of {total}")
    if total >= args.min_runs and share("refused") > args.max_refused_share:
        alerts.append(f"declined runs: {share('refused'):.0%} of {total}")
    for error_type in ("RateLimitError", "PoolTimeout"):
        if by_error.get(error_type):
            alerts.append(f"{error_type}: {by_error[error_type]} run(s)")
    alerts.extend(
        f"parked on an approval since "
        f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(p.since))} UTC: {p.thread_id}"
        for p in parked
    )
    print(
        json.dumps(
            {
                "hours": args.hours,
                "runs": total,
                "outcomes": {
                    o or "running": sum(n for oo, _e, n in counts if oo == o)
                    for o in {o for o, _e, _n in counts}
                },
                "errors": by_error,
                "parked": [p.thread_id for p in parked],
                "alerts": alerts,
            },
            indent=2,
        )
    )
    return 1 if alerts else 0


def _discover(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    if _refuses_shared_database(settings, args):
        return 1
    missing = require_api_keys(needs_search=settings.enable_search)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}", file=sys.stderr)
        print("Copy .env.example to .env and fill it in.", file=sys.stderr)
        return 1

    try:
        # The same boundary the drafting side applies to `--app-id`. A scan id
        # names a directory the UI later joins onto a real path, so it is
        # checked here rather than trusted because it came from a flag.
        scan_dir = opportunities_dir(settings, args.scan_id, user_id=CLI_USER)
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
    config = trace_config(
        settings,
        command="discover",
        frontend="cli",
        # The scan id names the run; the namespaced form is the thread, so
        # scanning and then drafting under the same name do not resume each
        # other -- see `config.discovery_thread_id` and invariant 12.
        ref=args.scan_id,
        thread_id=user_thread_id(CLI_USER, discovery_thread_id(args.scan_id)),
        recursion_limit=args.recursion_limit,
        details={
            "scan_id": args.scan_id,
            "focus": args.focus,
            "agencies": args.agencies,
        },
    )
    _run(
        agent,
        {"messages": [{"role": "user", "content": instruction}]},
        config,
        settings,
    )

    _print_shortlist(scan_dir, args.scan_id, settings)
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
    parser.add_argument(
        "--operator",
        action="store_true",
        help="allow a run against DATABASE_URL's shared database (logged)",
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

    # No `common` parent: none of those flags means anything to a migration,
    # and accepting `--approve` here would suggest it did.
    db_setup = sub.add_parser(
        "db-setup", help="create or migrate the Postgres tables (deployments only)"
    )
    db_setup.set_defaults(func=_db_setup)

    # Operator commands, exempt from `--operator` like `db-setup`: they read
    # counts and thread ids, never a thread's content, and start no run.
    health = sub.add_parser(
        "healthcheck", help="exit 0 when the database answers (readiness probe)"
    )
    health.set_defaults(func=_healthcheck)

    ops_check = sub.add_parser(
        "ops-check", help="report failed, declined and long-parked runs (alerts)"
    )
    ops_check.add_argument(
        "--hours", type=float, default=24, help="window to count runs over"
    )
    ops_check.add_argument(
        "--parked-days",
        type=float,
        default=PARKED_ALERT_DAYS,
        help=f"report threads parked this long (default: {PARKED_ALERT_DAYS:g})",
    )
    ops_check.add_argument(
        "--max-failed-share", type=float, default=0.1, help="default: 0.1"
    )
    ops_check.add_argument(
        "--max-refused-share", type=float, default=0.1, help="default: 0.1"
    )
    ops_check.add_argument(
        "--min-runs",
        type=int,
        default=5,
        help="runs needed before a share alerts (default: 5)",
    )
    ops_check.set_defaults(func=_ops_check)

    return parser


def main() -> int:
    args = _build_parser().parse_args()
    configure_logging(LOG_LEVEL)
    # Shared means the deployment's database; the CLI has no sign-in.
    configure_tracing(
        tracing_mode(shared=persistent_settings().database_url is not None)
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
