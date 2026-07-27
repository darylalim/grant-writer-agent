"""Streamlit front end for the grant writer agent.

Runs the same graph as `grant-writer draft`, against the same SQLite
checkpoint, so a run started here continues from `grant-writer chat --app-id X`
and back again. The app id is the LangGraph thread id in both.

Deliberately outside `src/grant_writer/`: the package must never import
streamlit, so installing the console script pulls in no web dependencies. The
flip side is that this file is not in the wheel either, which is why streamlit
is a dev dependency rather than an optional extra -- an extra would have been
installable by someone who then had no app to run.

    uv sync
    uv run streamlit run streamlit_app.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import streamlit as st
from langgraph.types import Command

from grant_writer.activity import (
    DELEGATE,
    MESSAGE,
    PLAN,
    TOOL,
    Event,
    approval_decisions,
    iter_activity,
    pending_action_requests,
)
from grant_writer.agent import build_agent
from grant_writer.config import (
    BackendProfile,
    application_dir,
    persistent_settings,
    require_api_keys,
)
from grant_writer.prompts import draft_request
from grant_writer.workspace import (
    application_files,
    compliance_verdict,
    count_gaps,
    read_bytes,
    read_text,
)

st.set_page_config(
    page_title="Grant writer",
    page_icon=":material/history_edu:",
    layout="wide",
)

# One turn's phases. The CLI runs this as a `while True` loop that blocks on
# input() when an interrupt arrives (see cli._run); Streamlit reruns the script
# top to bottom and can never block, so the same loop becomes a state machine
# driven by reruns, with the next payload parked in session state between them.
# This is only safe because the pending interrupt lives in the checkpointer,
# not in the agent object -- the graph can be rebuilt on any rerun and
# Command(resume=...) still lands on the right call.
IDLE = "idle"
RUNNING = "running"
AWAITING = "awaiting"
DONE = "done"
FAILED = "failed"
STOPPED = "stopped"

SUBAGENT_ICONS = {
    "funder-researcher": ":material/travel_explore:",
    "section-drafter": ":material/edit_note:",
    "compliance-checker": ":material/fact_check:",
}

TOOL_ICONS = {
    "write_file": ":material/draw:",
    "edit_file": ":material/draw:",
    "read_file": ":material/description:",
    "ls": ":material/folder_open:",
    "glob": ":material/search:",
    "grep": ":material/search:",
    "extract_pdf_text": ":material/picture_as_pdf:",
    "measure_text": ":material/straighten:",
    "tavily_search": ":material/travel_explore:",
    "write_todos": ":material/checklist:",
}

TODO_ICONS = {
    "completed": ":green[:material/check_circle:]",
    "in_progress": ":orange[:material/pending:]",
}
PENDING_TODO_ICON = ":gray[:material/radio_button_unchecked:]"

# How each non-markdown text file is shown in the browser, as the
# syntax-highlighting language or None for plain monospace. Only markdown goes
# to st.markdown: it *transforms* what it is given and these formats do not
# survive the trip -- a YAML comment becomes an <h1>, an indented block becomes
# a code fence, and a CSV collapses into one paragraph. A viewer that quietly
# shows something other than the file is worse than one that refuses to.
# Anything listed in neither is offered as a download, so a stray binary cannot
# be pushed through a text element.
CODE_LANGUAGES: dict[str, str | None] = {
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".csv": None,
    ".txt": None,
}

# A long run can emit thousands of events, and every rerun -- each entry in the
# reason box, each file click -- replays the whole feed. Keep all of them in
# state, draw only the tail.
MAX_RENDERED_EVENTS = 250

st.session_state.setdefault("activity", [])
st.session_state.setdefault("phase", IDLE)
st.session_state.setdefault("payload", None)
st.session_state.setdefault("active_app_id", "")
st.session_state.setdefault("error", "")
# Bumped on every resume, and mixed into the approval widgets' keys so each
# interrupt gets its own. See `resume_with`.
st.session_state.setdefault("approval_round", 0)

# A script run that is stopped mid-stream -- the toolbar's Stop button, or any
# widget interaction, which aborts the current pass at the next element call --
# leaves `phase` at RUNNING with its payload already consumed. Streamlit raises
# those as BaseException subclasses (RerunException, StopException) precisely so
# `except Exception` cannot swallow them, so the run block's handler never sees
# it. Without this the app is wedged: no turn can start because the payload is
# gone, and the submit button stays disabled because the phase still says busy.
if st.session_state.phase == RUNNING and st.session_state.payload is None:
    st.session_state.phase = STOPPED

# Read once, before anything interactive is drawn. Every widget on the page is
# gated on this: touching any of them mid-stream aborts the pass at the next
# element call and drops a turn that may be minutes of model calls into
# STOPPED. Disabling the submit button alone would just move which control
# throws the run away.
busy = st.session_state.phase == RUNNING


@st.cache_resource(show_spinner=False)
def get_agent(profile: BackendProfile, approve: bool, search: bool) -> Any:
    """Build the graph once per settings combination.

    `cache_resource` is shared across sessions, which is what we want: the
    SQLite checkpointer opens one connection for the process (with
    `check_same_thread=False`, see agent._build_checkpointer) instead of one
    per rerun. Toggling a sidebar setting rebuilds the graph, but the thread's
    history and any pending approval live in the checkpoint file, so nothing
    in flight is lost.

    The cache holds one agent per (profile, approve, search) combination and
    never evicts, so it tops out at eight SQLite connections held open for the
    process -- bounded, but the reason this is a single-user local tool rather
    than something to put behind a shared URL. Two viewers also share one graph
    object, so a sidebar toggle in one tab changes which agent the other tab's
    pending approval resumes through.
    """
    return build_agent(
        persistent_settings(
            backend_profile=profile, approve_final=approve, enable_search=search
        )
    )


def render_event(event: Event) -> None:
    """Draw one activity event into the current container."""
    if event.kind == PLAN:
        with st.container(border=True):
            st.markdown(f"**Plan** · {len(event.todos)} steps")
            for todo in event.todos:
                icon = TODO_ICONS.get(todo.status, PENDING_TODO_ICON)
                st.markdown(f"{icon} {todo.content}")
    elif event.kind == DELEGATE:
        icon = SUBAGENT_ICONS.get(event.label, ":material/hub:")
        st.markdown(f"{icon} Delegated to **{event.label}**")
    elif event.kind == TOOL:
        icon = TOOL_ICONS.get(event.label, ":material/build:")
        detail = f" `{event.detail}`" if event.detail else ""
        st.markdown(f":gray[{icon} {event.label}]{detail}")
    elif event.kind == MESSAGE:
        with st.container(border=True):
            st.markdown(event.detail)


def stream_turn(agent: Any, payload: Any, config: dict) -> bool:
    """Stream one turn into the current container.

    Returns True if the turn stopped on an approval interrupt. Events are
    appended to session state as they arrive so a later rerun can replay the
    whole feed -- Streamlit keeps no memory of what a previous run drew.
    """
    interrupted = False
    for chunk in agent.stream(payload, config=config, stream_mode="updates"):
        if "__interrupt__" in chunk:
            interrupted = True
            continue
        for event in iter_activity(chunk):
            st.session_state.activity.append(event)
            render_event(event)
    return interrupted


def resume_with(decisions: list[dict]) -> None:
    """Queue a resume for the next rerun and trigger it.

    Called from inside `approval_panel`, which is a fragment. `st.rerun()`
    defaults to `scope="app"` even there, and that default is load-bearing: the
    block that consumes the payload is in the main script body, so a
    fragment-scoped rerun would redraw the approve/reject buttons while the
    graph stayed parked on its interrupt -- a dead button, no error. Leave the
    scope alone.
    """
    st.session_state.payload = Command(resume={"decisions": decisions})
    st.session_state.phase = RUNNING
    # Retires this round's approval widgets. Their keys carry the counter, so
    # bumping it hands the next interrupt a blank reason box and a fresh set of
    # view toggles rather than the previous file's leftovers. Safe here and not
    # in the panel: `approval_round` is plain state, not a widget key.
    st.session_state.approval_round += 1
    st.rerun()


def render_feed(events: list[Event]) -> None:
    """Draw the tail of the activity log into the current container."""
    elided = len(events) - MAX_RENDERED_EVENTS
    if elided > 0:
        st.caption(f"{elided} earlier event(s) hidden.")
    for event in events[-MAX_RENDERED_EVENTS:]:
        render_event(event)


@st.fragment
def approval_panel(requests: list[dict]) -> None:
    """Draw the approval prompt for one interrupt's pending writes.

    A fragment because of the rejection-reason box. `st.text_input` commits on
    Enter or blur rather than per keystroke, so this is one full-app rerun per
    entry, not one per character -- but that rerun replays up to
    MAX_RENDERED_EVENTS activity events through st.markdown and re-walks the
    application directory to recount gaps and re-read the verdict, all to
    redraw a text box. Scoped here it redraws this container and nothing else.

    `requests` is passed in rather than fetched here so that a fragment rerun
    reuses the last arguments instead of re-reading the checkpoint on every
    keystroke. They cannot go stale under it: only a resume clears the
    interrupt, and `resume_with` reruns the whole app.
    """
    with st.container(border=True):
        st.subheader("Approval required", anchor=False)
        # An empty list does not mean nothing is pending. The graph is parked on
        # an interrupt either way -- that is why the phase is AWAITING -- and
        # `pending_action_requests` returns [] just as readily for a renamed
        # `deepagents` payload key or a non-dict interrupt value, both of which
        # it skips without raising. Left to the normal path this drew a
        # confident "1 write(s) ... read each one before approving" above no
        # expanders at all, and Approve then resumed a submission-bound write
        # that no human had seen. So say what happened, and make approving
        # blind take a second, deliberate action.
        blind = not requests
        if blind:
            st.error(
                "This run is waiting on an approval, but no pending write could "
                "be read from it — most likely a `deepagents` upgrade that "
                "renamed the interrupt payload. Approving would sign off on a "
                "submission-bound file without seeing it. Rejecting is safe: it "
                "releases the graph without writing.",
                icon=":material/error:",
            )
        else:
            # `approval_decisions` owns the resume count, shared with the CLI
            # prompt: one decision per action request, not per interrupt.
            st.caption(
                f"{len(requests)} write(s) to `final/`. These are the "
                "submission-bound files — read each one before approving."
            )
        for index, request in enumerate(requests):
            args = request.get("args") or {}
            path = args.get("file_path") or "(unknown path)"
            with st.expander(f"{request.get('name', '?')} → {path}"):
                content = args.get("content")
                if not content:
                    st.json(args)
                    continue
                # Source is the default, and the reason is not stylistic.
                # st.markdown *transforms* what it is given: a citation whose
                # link text and URL disagree shows only the text, heading
                # levels and whitespace normalise away, and `:red[...]` or
                # `$...$` are read as directives. This is the one moment a
                # human vets a submission-bound file, and approving a rendered
                # view approves something other than the bytes that get
                # written. Deselecting the control returns None, which lands
                # on source too.
                view = st.segmented_control(
                    "View",
                    ("Source", "Rendered"),
                    default="Source",
                    key=f"view_{st.session_state.approval_round}_{index}",
                    label_visibility="collapsed",
                )
                if view == "Rendered":
                    st.markdown(content)
                else:
                    st.code(content, language="markdown", wrap_lines=True)
        reason = st.text_input(
            "Reason",
            # Keyed per resume, not a fixed "reject_reason". A turn can
            # interrupt again immediately -- one interrupt per file under
            # final/ -- and a widget whose key is stable across that keeps its
            # value, so rejecting narrative.md with "budget is wrong" left that
            # text sitting in the box for budget.md. Clicking Reject again sent
            # the previous file's complaint, and the agent then fixed the wrong
            # thing. Session state cannot be cleared in `resume_with` instead:
            # the widget is already instantiated by then, and Streamlit refuses
            # to modify a live widget's key.
            key=f"reject_reason_{st.session_state.approval_round}",
            placeholder="Sent back to the agent if you reject.",
        )
        # Not a nag: it is the only thing separating "read it and approved it"
        # from "clicked the primary button on a panel showing nothing".
        acknowledged = not blind or st.checkbox(
            "Approve without seeing what will be written",
            key=f"blind_approve_{st.session_state.approval_round}",
        )
        with st.container(horizontal=True):
            if st.button(
                "Approve",
                type="primary",
                icon=":material/check:",
                disabled=not acknowledged,
            ):
                resume_with(approval_decisions(requests, approve=True))
            if st.button("Reject", icon=":material/close:"):
                resume_with(approval_decisions(requests, approve=False, message=reason))


def render_file(path: Path) -> None:
    """Draw one application file into the current container.

    Markdown gets the same Source/Rendered control as the approval panel, and
    files under `final/` default to source for the reason spelled out there:
    st.markdown shows a citation's link text and not its URL, and those are the
    files that go to a funder. That argument does not stop applying once the
    approval is clicked -- this is where the same file gets re-read. Everything
    under `sections/`, `research/`, and `review/` is a working draft read for
    its content, so it defaults to rendered.

    Other text formats are never markdown; see CODE_LANGUAGES.
    """
    if not path.is_file():
        # The listing is a snapshot and the agent keeps writing, so a file can
        # be gone by the time it is picked. Say so, rather than drawing the
        # empty string `read_text` returns -- which reads as an empty file.
        st.caption("That file is no longer on disk.")
        return

    if path.suffix == ".md":
        view = st.segmented_control(
            "View",
            ("Source", "Rendered"),
            default="Source" if path.parent.name == "final" else "Rendered",
            # Keyed per file, so picking another one lands on that file's own
            # default instead of inheriting the last one's view. No explicit
            # reset is needed: a keyed widget's value is dropped once it stops
            # being rendered, and only one file is rendered at a time.
            key=f"browse_view_{path}",
            label_visibility="collapsed",
        )
        # Deselecting the control returns None, which lands on source -- the
        # same fallback the approval panel takes, and the safe one either way.
        if view == "Rendered":
            st.markdown(read_text(path))
        else:
            st.code(read_text(path), language="markdown", wrap_lines=True)
    elif path.suffix in CODE_LANGUAGES:
        st.code(read_text(path), language=CODE_LANGUAGES[path.suffix], wrap_lines=True)
    elif (blob := read_bytes(path)) is None:
        # Same message, a narrower window: the file survived the check above
        # and vanished before the read.
        st.caption("That file is no longer on disk.")
    elif path.suffix == ".pdf":
        st.pdf(blob, height=430)
    else:
        st.download_button(
            "Download", blob, file_name=path.name, icon=":material/download:"
        )


@st.fragment
def file_browser(app_dir: Path, files: list[Path], gaps: int, verdict: str) -> None:
    """Draw the application-directory browser for one listing.

    A fragment for the same reason `approval_panel` is one. Picking a file
    changes only which text the right-hand pane shows, but as a plain part of
    the script that click reran the whole app: replaying up to
    MAX_RENDERED_EVENTS events through st.markdown, re-walking the application
    directory, and re-reading every draft to recount gaps and re-read the
    verdict -- all to swap one pane.

    Everything derived from the listing is passed in rather than computed here,
    and that is what makes the fragment worth having: a fragment rerun reuses
    the last arguments, so a file click touches disk once, for the file it is
    about to show. Recomputing `gaps` and `verdict` in this body would re-read
    every draft on every click and leave only the feed replay saved.

    Neither can go stale under a fragment rerun. Nothing writes into the
    directory except a turn, and a turn ends in a full-app rerun -- see the
    `st.rerun` closing the run block -- which recomputes both.
    """
    with st.container(horizontal=True):
        st.metric("Files", len(files), border=True)
        st.metric(
            "Needs input",
            gaps,
            border=True,
            help="Unresolved `[NEEDS INPUT]` markers — facts the agent refused "
            "to invent and a human must supply.",
        )
        st.metric(
            "Compliance",
            verdict,
            border=True,
            help="The compliance reviewer's latest verdict, from `review/`.",
        )

    names = [str(path.relative_to(app_dir)) for path in files]
    picker_col, content_col = st.columns([1, 2], vertical_alignment="top")
    with picker_col, st.container(height=480, border=True):
        selected = st.radio(
            "File", names, key="file_pick", label_visibility="collapsed"
        )
    with content_col, st.container(height=480, border=True):
        render_file(app_dir / selected)


# --- Sidebar: run settings ---------------------------------------------------

with st.sidebar:
    st.subheader("Run settings", anchor=False)
    # Every control here takes an explicit key. Without one a widget's identity
    # is derived from its parameters, `disabled` among them, so gating these on
    # `busy` would change their identity the moment a run starts and hand back
    # defaults -- silently swapping the profile or switching search on for the
    # pass that actually builds the graph. With a key, identity is the key.
    profile = st.selectbox(
        "Backend profile",
        ("local", "server"),
        key="profile_select",
        disabled=busy,
        help=(
            "`local` writes real files under `applications/`. `server` keeps "
            "drafts in ephemeral graph state, so the file browser stays empty."
        ),
    )
    # The CLI defaults --approve off; a UI where approving is one click has no
    # reason to. Every write under final/ is submission-bound, and the whole
    # point of the narrow rule is that you actually read the ones you get.
    approve = st.toggle(
        "Approve writes to final/",
        value=True,
        key="approve_toggle",
        disabled=busy,
        help="Pause before each submission-bound file and show it for review.",
    )
    search = st.toggle(
        "Web search",
        value=True,
        key="search_toggle",
        disabled=busy,
        help="Funder research needs TAVILY_API_KEY. Turn off to run without it.",
    )
    recursion_limit = st.number_input(
        "Recursion limit",
        min_value=20,
        max_value=500,
        value=150,
        step=10,
        key="recursion_limit_input",
        disabled=busy,
        help="Maximum graph steps per turn.",
    )
    st.divider()
    st.caption(
        "Drafts land in `applications/<app-id>/`. State persists to "
        "`.grant_writer/checkpoints.sqlite`, shared with the CLI."
    )

# Value-equal to the Settings inside `get_agent` -- same function, same args --
# but built here without constructing any models, so paths are available before
# an API key is needed.
settings = persistent_settings(
    backend_profile=profile, approve_final=approve, enable_search=search
)

# --- Header and run form -----------------------------------------------------

st.title("Grant writer", anchor=False)
st.caption(
    "Turns a solicitation into a compliant proposal draft. It drafts; it does "
    "not submit. Every output needs human review before it goes to a funder."
)

missing = require_api_keys(needs_search=search)
if missing:
    st.error(
        f"Missing environment variables: {', '.join(missing)}. "
        "Copy `.env.example` to `.env` and fill it in, then restart the app.",
        icon=":material/key_off:",
    )

with st.form("draft", border=True):
    with st.container(horizontal=True):
        app_id = st.text_input(
            "Application id",
            key="app_id_input",
            placeholder="nsf-aisl-2026",
            help="Also the LangGraph thread id. Reuse it to continue a run.",
        )
        funder = st.text_input("Funder", placeholder="NSF")
    rfp = st.file_uploader("Solicitation PDF", type="pdf")
    notes = st.text_area(
        "Additional context",
        placeholder="Anything the agent should know that is not in the solicitation.",
        height=80,
    )
    rubric = st.file_uploader(
        "Review criteria (optional)",
        type=["md", "txt"],
        help="The funder's published criteria. The agent grades itself against "
        "them and iterates, up to 3 times.",
    )
    submitted = st.form_submit_button(
        "Draft proposal",
        type="primary",
        icon=":material/play_arrow:",
        disabled=bool(missing) or busy,
    )

# --- Reserve output slots before any slow work -------------------------------

activity_slot = st.container()
status_slot = st.container()
results_slot = st.container()

if submitted:
    app_id = app_id.strip()
    try:
        if not app_id:
            msg = "An application id is required."
            raise ValueError(msg)

        # `application_dir` is the boundary. Joining the raw id would escape
        # `applications/` outright: pathlib discards the left operand for an
        # absolute right one, so an id of "/Users/me/.ssh" writes there.
        app_dir = application_dir(settings, app_id)

        rfp_path = None
        if rfp is not None:
            # Keep the source PDF beside the drafts rather than in a temp file,
            # so the same app id works again tomorrow. Written by the app, not
            # the agent, so FilesystemPermission does not cover it -- which is
            # exactly why the id and the upload name are both bounded here.
            # `rfp.name` is client-supplied; take its last component only.
            filename = Path(rfp.name).name or "solicitation.pdf"
            target = app_dir / filename
            app_dir.mkdir(parents=True, exist_ok=True)
            target.write_bytes(rfp.getvalue())
            rfp_path = str(target)

        payload: dict[str, Any] = {
            "messages": [
                {
                    "role": "user",
                    "content": draft_request(
                        app_id,
                        rfp_path=rfp_path,
                        funder=funder.strip() or None,
                        notes=notes.strip() or None,
                    ),
                }
            ]
        }
        if rubric is not None:
            # RubricMiddleware is dormant until this key is in invocation state.
            payload["rubric"] = rubric.getvalue().decode("utf-8")
    except (ValueError, OSError, UnicodeDecodeError) as exc:
        # Everything above touches user input or the disk. Route failures
        # through the app's own error banner rather than a raw traceback,
        # which would also leave phase and payload half-set.
        st.session_state.error = str(exc)
        st.session_state.phase = FAILED
    else:
        st.session_state.activity = []
        st.session_state.error = ""
        st.session_state.active_app_id = app_id
        st.session_state.payload = payload
        st.session_state.phase = RUNNING
        # Rerun before streaming rather than falling through to the run block.
        # `busy` is read further up, *before* this handler sets the phase, so
        # the form on this pass was already drawn enabled -- and this pass is
        # the one that then streams the agent for minutes. Without the rerun
        # the submit button sits there clickable for the whole run, inviting a
        # second click that aborts the turn in flight. One extra pass costs a
        # redraw and no model calls; the next one renders the form disabled and
        # runs the turn.
        st.rerun()

# --- Activity feed -----------------------------------------------------------

with activity_slot:
    st.subheader("Activity", anchor=False)
    feed = st.container(height=440, border=True, autoscroll=True)
    with feed:
        render_feed(st.session_state.activity)
        if not st.session_state.activity and st.session_state.phase != RUNNING:
            st.caption("Nothing yet. Submit a solicitation above to start a run.")
    st.caption(
        "Long runs can be stopped from Streamlit's toolbar and picked up later "
        "with the same application id — progress is checkpointed each step."
    )

# --- Run one turn ------------------------------------------------------------

if st.session_state.phase == RUNNING and st.session_state.payload is not None:
    turn_payload = st.session_state.payload
    # Consume before running: a crash must not leave a payload that replays.
    st.session_state.payload = None
    agent = get_agent(profile, approve, search)
    config = {
        "configurable": {"thread_id": st.session_state.active_app_id},
        "recursion_limit": int(recursion_limit),
    }
    with feed, st.spinner("Agent is working…", show_time=True):
        try:
            interrupted = stream_turn(agent, turn_payload, config)
        except Exception as exc:  # noqa: BLE001 - surface anything to the user
            st.session_state.error = f"{type(exc).__name__}: {exc}"
            st.session_state.phase = FAILED
        else:
            st.session_state.phase = AWAITING if interrupted else DONE
    # Redraw now that the phase is terminal. The form above was drawn from
    # `busy` at the top of this pass -- disabled, correctly, since the turn was
    # about to stream -- and nothing else would rerun to re-enable it. Skipping
    # this leaves a greyed-out submit button sitting under the "Run finished"
    # banner until the user happens to touch some other widget: the same bug as
    # leaving it live during the run, at the other end of the turn.
    st.rerun()

# --- Approval and status -----------------------------------------------------

with status_slot:
    if st.session_state.error:
        st.error(st.session_state.error, icon=":material/error:")

    if st.session_state.phase == AWAITING:
        agent = get_agent(profile, approve, search)
        config = {"configurable": {"thread_id": st.session_state.active_app_id}}
        # Read the checkpoint here, in the full app run, and hand the result to
        # the fragment -- see approval_panel on why it does not fetch its own.
        approval_panel(pending_action_requests(agent, config))

    elif st.session_state.phase == STOPPED:
        st.warning(
            f"Run stopped before it finished. Progress for "
            f"`{st.session_state.active_app_id}` is checkpointed — submit the "
            "same application id to carry on from where it left off.",
            icon=":material/pause_circle:",
        )

    elif st.session_state.phase == DONE:
        st.success(
            f"Run finished for `{st.session_state.active_app_id}`. Review every "
            "number, citation, and `[NEEDS INPUT]` marker before submitting.",
            icon=":material/task_alt:",
        )

# --- Application files -------------------------------------------------------

browse_id = (st.session_state.get("app_id_input") or "").strip()
browse_id = browse_id or st.session_state.active_app_id

with results_slot:
    st.subheader("Application files", anchor=False)

    if profile == "server":
        st.info(
            "The `server` profile keeps drafts in ephemeral graph state, not on "
            "disk. Switch to `local` to browse files.",
            icon=":material/cloud_off:",
        )
    elif not browse_id:
        st.caption("Enter an application id to browse its files.")
    else:
        try:
            # Same boundary as the write side, and it matters more here: without
            # it an id of ".." lists the repo root, and the download branch
            # below would hand `.env` -- live API keys -- to the browser.
            app_dir = application_dir(settings, browse_id)
        except ValueError as exc:
            st.warning(str(exc), icon=":material/warning:")
        else:
            files = application_files(app_dir)
            if not files:
                st.caption(f"Nothing in `applications/{browse_id}/` yet.")
            else:
                # Walk the directory here, in the full app run, and hand the
                # results to the fragment -- see `file_browser` on why it does
                # not read its own.
                file_browser(
                    app_dir, files, count_gaps(files), compliance_verdict(files)
                )
