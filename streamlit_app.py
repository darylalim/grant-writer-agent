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
    is_parked,
    iter_activity,
    pending_action_requests,
)
from grant_writer.agent import build_agent, build_discovery_agent
from grant_writer.config import (
    BackendProfile,
    application_dir,
    application_ids,
    discovery_thread_id,
    opportunities_dir,
    opportunity_scan_ids,
    persistent_settings,
    require_api_keys,
)
from grant_writer.opportunities import (
    MAX_TOTAL_POINTS,
    ScoredOpportunity,
    rank_opportunities,
)
from grant_writer.prompts import discovery_request, draft_request
from grant_writer.workspace import (
    application_files,
    compliance_verdict,
    count_gaps,
    read_bytes,
    read_scored_opportunities,
    scan_files,
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

# A follow-up the human typed, echoed into the feed so a turn does not open
# with a reply to a question no longer on screen. Deliberately *not* in
# `activity.py` alongside the kinds imported above: that module's job is reading
# the agent's stream, and nothing in the stream ever produces this. It rides on
# `Event` because the feed is a list of Events and `kind` is an open string.
PROMPT = "prompt"

# Which of the two graphs a turn runs on. Not a phase: a run is RUNNING or
# AWAITING or DONE regardless of which graph it is, and folding the two axes
# into one enum would double every phase comparison on the page.
DRAFT = "draft"
DISCOVER = "discover"

SUBAGENT_ICONS = {
    "funder-researcher": ":material/travel_explore:",
    "section-drafter": ":material/edit_note:",
    "compliance-checker": ":material/fact_check:",
    "opportunity-scout": ":material/plagiarism:",
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
    "search_grants_gov": ":material/manage_search:",
    "fetch_grants_gov_opportunity": ":material/download_for_offline:",
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
# The scan the discovery graph is running on. A *separate* key from
# `active_app_id` rather than a reuse of it, because the two name directories
# in different trees and the file browser at the foot of the page reads
# `active_app_id` as a fallback -- parking a scan id there would send it
# looking for `applications/<scan-id>/`, find nothing, and report an empty
# application rather than a scan it was never asked about.
st.session_state.setdefault("active_scan_id", "")
# Which graph the current turn belongs to: DRAFT or DISCOVER. One phase
# machine drives both, so this is what tells the run block which agent to
# build and which thread id to run it on. See `_active_thread_id`.
st.session_state.setdefault("active_kind", DRAFT)
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

# Everything that can start a turn is gated on this rather than on `busy`.
# RUNNING is not the only phase where starting one is wrong: on AWAITING the
# graph is parked on an interrupt, and a fresh brief or a plain message there
# resumes nothing -- it abandons the submission-bound write the approval panel
# is asking a human to vet. Spelled once rather than at each site, so a phase
# added here reaches every control instead of most of them, and named for the
# rule rather than the first control it gated: it now covers the sidebar, the
# id box, the browse picker, the run form, and the follow-up input.
#
# This is only the session's own guess, though. `phase` is *this* browser
# session's memory of the run, and a reload, a second tab, or `grant-writer
# chat` in another terminal all reach a still-parked thread with it back at
# IDLE -- while STOPPED and FAILED can both be reached with an interrupt
# already committed, since either is inferred from a pass that stopped rather
# than from the graph. So this gates the controls and the submit handler asks
# the checkpoint; see `parked` there. Disabled widgets are the courtesy, that
# check is the guarantee.
turn_locked = busy or st.session_state.phase == AWAITING


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


@st.cache_resource(show_spinner=False)
def get_discovery_agent(profile: BackendProfile, approve: bool, search: bool) -> Any:
    """Build the discovery graph once per settings combination.

    Its own function, not a flag on `get_agent`, so the two graphs cannot share
    a cache entry: `cache_resource` keys on the function *and* its arguments,
    and `(local, True, True)` means a different graph here than it does there.

    Takes `approve` even though nothing in a scan can interrupt -- the sidebar
    toggle drives both graphs, and dropping the argument here would give the
    two builders different cache keys for the same visible settings, so
    flipping the toggle would rebuild one and hand back a cached other.
    """
    return build_discovery_agent(
        persistent_settings(
            backend_profile=profile, approve_final=approve, enable_search=search
        )
    )


def _active_thread_id() -> str:
    """The LangGraph thread id for whichever graph the current turn belongs to.

    One expression, read by the run block and by the approval panel, so those
    two can never disagree about which thread they are addressing. A scan runs
    under a namespaced id (see `config.discovery_thread_id`) precisely so it
    cannot land on the checkpoint row an identically-named application uses.
    """
    if st.session_state.active_kind == DISCOVER:
        return discovery_thread_id(st.session_state.active_scan_id)
    return st.session_state.active_app_id


def parked_state(
    app_id: str, profile: BackendProfile, approve: bool, search: bool
) -> tuple[bool | None, str]:
    """Ask the checkpoint whether `app_id`'s thread is parked on an interrupt.

    Returns `(parked, cause)`, where `parked` is None when the read itself
    failed and `cause` names why. None rather than False, and the distinction is
    the entire point: this is the check that stops a new turn abandoning a
    submission-bound write a human was asked to vet (see both call sites), so a
    caller that read a failed check as "not parked" would fail open in exactly
    the case the check exists for -- the same trap `activity.is_parked` exists
    to keep callers out of. Refusing the turn is the safe direction, because a
    read that raised cannot rule a pending write out.

    Catching `Exception` rather than a tuple is deliberate. The checkpoint is
    the SQLite file `grant-writer chat` also opens, so `database is locked`
    arrives as `sqlite3.OperationalError`, which is neither an `OSError` nor a
    `ValueError` -- the submit handler's own tuple did not cover it, and the
    read reached that handler as a raw traceback over a half-set phase. The
    breadth is bounded by how little is inside the try: two calls, neither of
    which reruns, so nothing here can swallow control flow.

    `get_agent` is inside the try, not above it: it builds models and opens the
    SQLite connection, so a read-only `.grant_writer/` or a dropped API key
    raises there rather than in the read below. That is the same line the
    approval block at the foot of this file keeps inside its own guard, for the
    same reason.
    """
    try:
        agent = get_agent(profile, approve, search)
        return is_parked(agent, {"configurable": {"thread_id": app_id}}), ""
    except Exception as exc:  # noqa: BLE001 - the caller refuses the turn
        return None, f"{type(exc).__name__}: {exc}"


def render_event(event: Event) -> None:
    """Draw one activity event into the current container."""
    if event.kind == PLAN:
        # One element per todo, so the container's default 1rem gap is paid
        # between every line of the checklist -- an eight-step plan spends more
        # of the 440px feed on blank space than on text. `xxsmall` (0.25rem)
        # still parts the heading from the first todo without spacing the list
        # out like separate paragraphs.
        with st.container(border=True, gap="xxsmall"):
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
    elif event.kind == PROMPT:
        # The one thing in the feed that came from the human, so it gets the
        # native chat bubble rather than another bordered container -- the
        # agent's own prose is already drawn as one, and a turn that opens with
        # a follow-up needs the two told apart at a glance.
        with st.chat_message("user"):
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


def _pick_application() -> None:
    """Copy the browse picker's choice into the application id box.

    The picker is an input method for that box, not a second source of truth.
    Two controls both deciding which application is on screen disagree the
    moment a run starts on an id the picker is not showing, and whichever one
    the code reads first is then wrong half the time. Funnelling through the id
    box keeps `browse_id` a single expression.

    That makes the *browsed* application single-valued. It does not make it the
    same value as the run: `active_app_id` is assigned only by the submit
    handler, so picking here after a run points the file browser somewhere the
    graph is not. That is deliberate -- reading an old draft must not cost a
    billed turn -- and the results block below says so on screen rather than
    leaving the two to disagree quietly.

    Legal only because widget callbacks run *before* the script body: the id
    box is not instantiated yet, so assigning its key here is a default rather
    than the modification of a live widget, which Streamlit refuses.

    Clearing the picker afterwards is what makes it behave like an action
    rather than a selection. Left set, picking `alpha`, typing `beta`, then
    picking `alpha` again fires no change event -- same value -- and the box
    would stay on `beta` with the picker showing `alpha`.
    """
    if picked := st.session_state.browse_pick:
        st.session_state.app_id_input = picked
        st.session_state.browse_pick = None


def _pick_scan() -> None:
    """Copy the scan picker's choice into the scan id box.

    The same shape as `_pick_application`, for the same reasons and with the
    same legality argument: widget callbacks run before the script body, so
    assigning the box's key here is setting a default rather than mutating a
    live widget. Clearing the picker afterwards is what makes it behave like an
    action rather than a selection -- see that function for the re-picking case
    that motivates it.
    """
    if picked := st.session_state.scan_pick:
        st.session_state.scan_id_input = picked
        st.session_state.scan_pick = None


def _verdict_markup(criterion: Any) -> str:
    """One rubric row, coloured by verdict.

    Uses the theme's semantic colour names rather than hex, so the row stays
    legible on both surfaces -- `.streamlit/config.toml` derives a per-mode
    value from each, and hardcoding one picks a side.
    """
    colours = {"STRONG": "green", "MODERATE": "orange", "WEAK": "red", "NONE": "red"}
    if criterion.verdict is None:
        # Not the same as a bad verdict, and must not read like one: nothing
        # was scored here, so there is no judgement to colour.
        return f":gray[{criterion.label} — not scored]"
    colour = colours.get(criterion.verdict, "gray")
    return (
        f":{colour}[**{criterion.verdict}**] {criterion.label} "
        f":gray[({criterion.points}/{criterion.max_points})]"
    )


@st.fragment
def opportunity_browser(
    scan_dir: Path, ranked: list[ScoredOpportunity], gaps: int
) -> None:
    """Draw the ranked shortlist for one scan.

    A fragment for the reason `file_browser` is one: expanding a candidate to
    read its citations must not replay the whole feed and re-parse every other
    candidate. Everything derived from disk is computed by the caller and
    passed in, so a fragment rerun re-reads nothing -- and cannot go stale,
    because only a turn writes here and a turn ends in a full-app rerun.
    """
    top = ranked[0].fit_percent if ranked else None
    with st.container(horizontal=True):
        st.metric("Candidates", len(ranked), border=True)
        st.metric(
            "Best fit",
            "—" if top is None else f"{top:.0f}%",
            border=True,
            help=f"Weighted across the {MAX_TOTAL_POINTS}-point fit rubric. "
            "Computed from the scout's verdicts — it never states a score.",
        )
        st.metric(
            "Needs input",
            gaps,
            border=True,
            help="Unresolved `[NEEDS INPUT]` markers — facts the scout refused "
            "to invent and a human must supply.",
        )

    if not ranked:
        st.caption("No scored candidates in this scan yet.")
        return

    for position, opportunity in enumerate(ranked, start=1):
        # `score_label` and `display_title` come from `opportunities`, shared
        # with the CLI: "unscored" and "0%" are opposite instructions to a
        # reader, and that decision made twice is how one frontend starts
        # printing the other's answer.
        flag = " · INELIGIBLE" if opportunity.disqualified else ""
        with st.expander(
            f"{position}. {opportunity.display_title} — {opportunity.score_label}{flag}"
        ):
            if opportunity.fields:
                st.caption(
                    " · ".join(
                        f"{label}: {value}"
                        for label, value in opportunity.fields.items()
                    )
                )
            for criterion in opportunity.criteria:
                st.markdown(_verdict_markup(criterion))
                for citation in criterion.citations:
                    # A gap marker is not a quote and must not be italicised
                    # like one -- it is the scout saying it had nothing to
                    # quote, which is the opposite claim.
                    quoted = citation.text if citation.is_gap else f"*{citation.text}*"
                    st.markdown(f"  :gray[↳ {citation.source}:] {quoted}")
                if criterion.note:
                    st.caption(f"↳ {criterion.note}")
            for warning in opportunity.warnings:
                st.warning(warning, icon=":material/report:")
            # The raw file, under the reading of it. The scoring is the
            # product, but a citation is only worth what its source says, and
            # this is where someone checks that.
            candidate = scan_dir / "candidates" / f"{opportunity.key}.md"
            if candidate.is_file():
                with st.popover("Read the source", icon=":material/description:"):
                    render_file(candidate)


def render_feed(events: list[Event]) -> None:
    """Draw the tail of the activity log into the current container."""
    elided = len(events) - MAX_RENDERED_EVENTS
    if elided > 0:
        st.caption(f"{elided} earlier event(s) hidden.")
    for event in events[-MAX_RENDERED_EVENTS:]:
        render_event(event)


@st.fragment
def approval_panel(requests: list[dict], unreadable: str = "") -> None:
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

    `unreadable` is the *cause* when the caller already knows why the list is
    empty, not a replacement message. An empty list has two very different
    reasons -- a payload this app could not parse, or a checkpoint read that
    raised -- and reporting a locked database as a `deepagents` rename sends the
    reader after the wrong thing. Only one of the two is worth retrying, which
    is the other thing this argument decides.
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
            # One safety sentence, composed here rather than written out at each
            # call site. Only the cause differs between them, and a copy of the
            # warning kept beside each cause is a copy that goes stale silently:
            # the wording that tells a human not to approve blind is the last
            # thing that should drift between two branches of the same panel.
            cause = unreadable or (
                "most likely a `deepagents` upgrade that renamed the interrupt payload"
            )
            st.error(
                f"This run is waiting on an approval, but no pending write "
                f"could be read from it — {cause}. Approving would sign off on "
                "a submission-bound file without seeing it. Rejecting is safe: "
                "it releases the graph without writing.",
                icon=":material/error:",
            )
            # A read that raised may simply have lost a race for the SQLite file
            # the CLI shares. That case wants another look, not a decision --
            # without it the only offers on screen are throwing away a good
            # draft or approving one nobody has seen. Withheld when the payload
            # merely did not parse: rereading returns the same bytes, and a
            # retry that cannot help reads as one that was not tried hard enough.
            if unreadable and st.button("Try again", icon=":material/refresh:"):
                st.rerun()
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
                # written. `required` keeps the pair exhaustive: without it a
                # click on the lit segment deselects, and the control then
                # shows nothing lit above a pane that is still displaying one
                # of the two views.
                view = st.segmented_control(
                    "View",
                    ("Source", "Rendered"),
                    default="Source",
                    required=True,
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
        # empty string a failed read returns -- which reads as an empty file.
        st.caption("That file is no longer on disk.")
        return

    # Lowercased before every comparison below. The upload is saved under the
    # name the browser sent (see the submit handler), which keeps its case, so
    # `SOLICITATION.PDF` missed the `.pdf` branch and was offered as a download
    # instead of being shown in the viewer. CODE_LANGUAGES keys are already
    # lowercase, so only the lookup had to change.
    suffix = path.suffix.lower()

    # One read, before the dispatch, and bytes rather than text. `read_text`
    # collapses a vanished file and an undecodable one into the same "", and
    # the text branches drew that as an empty pane -- a file that exists,
    # rendered as though it were empty. That is the failure the comment on
    # CODE_LANGUAGES exists to prevent ("a viewer that quietly shows something
    # other than the file is worse than one that refuses to"), reached through
    # the encoding rather than the format. Decoding explicitly below tells the
    # two apart.
    if (blob := read_bytes(path)) is None:
        # Same message, a narrower window: the file survived the check above
        # and vanished before the read.
        st.caption("That file is no longer on disk.")
        return

    if suffix == ".pdf":
        st.pdf(blob, height=430)
        return

    if suffix != ".md" and suffix not in CODE_LANGUAGES:
        st.download_button(
            "Download", blob, file_name=path.name, icon=":material/download:"
        )
        return

    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        # Listed as text but not decodable as text -- a CSV exported as
        # latin-1, or a binary that borrowed the suffix. Say which of the two
        # failures this is, and hand over the bytes, rather than pushing a
        # replacement-character soup through st.code or drawing nothing.
        st.caption("That file is not valid UTF-8 text.")
        st.download_button(
            "Download", blob, file_name=path.name, icon=":material/download:"
        )
        return

    if suffix != ".md":
        st.code(text, language=CODE_LANGUAGES[suffix], wrap_lines=True)
        return

    view = st.segmented_control(
        "View",
        ("Source", "Rendered"),
        default="Source" if path.parent.name == "final" else "Rendered",
        # Not deselectable. Without this, clicking the lit segment returned
        # None and the pane fell back to source while no segment was lit, so
        # the control described neither view -- worst on a working draft, which
        # starts on Rendered and so is one click from that state.
        required=True,
        # Keyed per file, so picking another one lands on that file's own
        # default instead of inheriting the last one's view. No explicit
        # reset is needed: a keyed widget's value is dropped once it stops
        # being rendered, and only one file is rendered at a time.
        key=f"browse_view_{path}",
        label_visibility="collapsed",
    )
    if view == "Rendered":
        st.markdown(text)
    else:
        st.code(text, language="markdown", wrap_lines=True)


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
    # is derived from its value-shaping parameters -- `label`, `value`,
    # `options`, `help`, `width` -- and a widget whose identity changes remounts
    # and hands back its default, silently swapping the profile or switching
    # search on for the pass that actually builds the graph. A key narrows what
    # identity depends on, via `key_as_main_identity` in `elements/lib/utils.py`
    # -- to nothing else at all for the toggles and the number input, and to
    # `accept_new_options` alone for the selectbox, which is constant here.
    #
    # `disabled` was never among those parameters, so gating these on
    # `turn_locked` would not by itself have reset them; the widget docstrings
    # list it with `on_change` and `label_visibility` as excluded. The keys are
    # what makes the whole set safe to gate rather than a fact about 1.60 that
    # a later parameter change could take back.
    profile = st.selectbox(
        "Backend profile",
        ("local", "server"),
        key="profile_select",
        disabled=turn_locked,
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
        disabled=turn_locked,
        help="Pause before each submission-bound file and show it for review.",
    )
    search = st.toggle(
        "Web search",
        value=True,
        key="search_toggle",
        disabled=turn_locked,
        help="Funder research needs TAVILY_API_KEY. Turn off to run without it.",
    )
    recursion_limit = st.number_input(
        "Recursion limit",
        min_value=20,
        max_value=500,
        value=150,
        step=10,
        key="recursion_limit_input",
        disabled=turn_locked,
        help="Maximum graph steps per turn.",
    )
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

# Read once per pass, before the id box that the picker fills. `application_ids`
# runs every candidate through `application_dir` itself, so nothing offered here
# can be picked and then refused by the boundary below.
#
# Gated on the profile, and not just to save the walk. `server` keeps drafts in
# ephemeral graph state and the results pane below says so; a picker there would
# list whatever an earlier `local` run happened to leave on disk, promise a read
# it structurally cannot perform, and still retarget the thread id on the way to
# refusing. Disk is not the source of truth in that profile, so it is not
# offered as one.
existing_ids = application_ids(settings) if profile == "local" else []
# The scan-side counterpart, gated on the profile for the same reason: under
# `server` a scan's files live in ephemeral graph state, so a picker over disk
# would offer reads it structurally cannot perform.
existing_scan_ids = opportunity_scan_ids(settings) if profile == "local" else []

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

# --- Discovery: find opportunities worth drafting -----------------------------
#
# Above the drafting form because that is the order the work happens in: you
# find an opportunity, then you write to it. Both sections start a turn, both
# are gated on the same `turn_locked`, and both feed the same activity feed
# below -- what differs is which graph runs and which tree it writes into.

st.subheader("Find opportunities", anchor=False)
st.caption(
    "Searches grants.gov and the web, then scores each candidate against your "
    "organization profile. Scoring is a rubric of verdicts with quoted "
    "citations; the percentage is computed from those, never stated by the agent."
)

# Outside the form, and for the reason spelled out at the application id box
# below: the shortlist at the foot of the page reads this back to decide which
# scan to show, so inside a form it could only ever show a scan already
# submitted this session -- making re-reading last week's scan cost a fresh
# billed run.
with st.container(horizontal=True):
    scan_id = st.text_input(
        "Scan id",
        key="scan_id_input",
        placeholder="rural-health-2026",
        disabled=turn_locked,
        help="Names the scan directory, and the discovery thread. Reuse it to "
        "add to a scan, or to read one below.",
    )
    if existing_scan_ids:
        st.selectbox(
            "Browse an existing scan",
            existing_scan_ids,
            index=None,
            key="scan_pick",
            on_change=_pick_scan,
            placeholder="Pick one to read…",
            disabled=turn_locked,
            help="Fills the id box on the left. Reading a scan does not start a run.",
        )

with st.form("discover", border=True):
    focus = st.text_input(
        "What to look for",
        placeholder="afterschool STEM programs in rural districts",
        help="Your programs and populations, in your own words. The scout "
        "builds its search from this and the organization profile.",
    )
    agencies = st.text_input(
        "Agencies (optional)",
        placeholder="USDA|NSF",
        help="Pipe-separated grants.gov agency codes. Leave empty to search "
        "every agency — the results tell you which codes are worth filtering on.",
    )
    scan_notes = st.text_area(
        "Additional context",
        placeholder="Constraints, deadlines you cannot meet, funders to avoid.",
        height=80,
    )
    discover_submitted = st.form_submit_button(
        "Find opportunities",
        icon=":material/search:",
        # `turn_locked`, not `busy`, exactly as the drafting button is. A scan
        # runs on its own thread so it cannot abandon a pending `final/` write
        # the way a second draft brief would -- but it does clear the activity
        # feed, which is the only on-screen record that one is pending, and on
        # AWAITING the panel below is asking about that write.
        disabled=bool(missing) or turn_locked,
        help="Resolve the pending approval below before starting a new run."
        if turn_locked and not busy
        else None,
    )

st.divider()
st.subheader("Draft a proposal", anchor=False)

# Outside the form, deliberately. `st.form` batches its widgets and sends them
# only when Submit is pressed -- "the values of the widgets inside it are never
# sent to your app" otherwise. This id is not just a run input: the file browser
# at the bottom reads it back to decide which application to show. Inside the
# form that read could only ever see an id already submitted this session, so
# opening yesterday's drafts meant clicking Draft proposal and starting a fresh
# billed run on them, and the caption below promising otherwise was unreachable.
# Out here, committing the box reruns the script and the browser follows.
#
# The lock below is load-bearing *because* of the move, not decoration copied
# from the sidebar. In the form this input could not trigger a rerun, so it was
# harmless mid-stream; out here typing into it during a turn would abort the
# streaming pass at the next element call and drop minutes of model calls into
# STOPPED, exactly as an ungated sidebar widget would. The key keeps its
# identity stable across that toggle -- see the sidebar block on why.
#
# The picker beside it is an input method for this box, not a second source of
# truth -- see `_pick_application`. So this stays the one value both the run and
# the browser read, and a text input rather than a selectbox with
# `accept_new_options`: AppTest serialises a selectbox by *index*
# (`Selectbox.index` does `options.index(value)`), so a typed id that is not
# already an option raises ValueError in the harness. Every case that names a
# new application -- including the ones pinning that `../evil` is refused --
# would become untestable, and these AppTest cases are the only coverage this
# file has.
# `turn_locked` (defined at the top) covers this box for a reason of its own,
# beyond the shared "no turn may start on a parked thread": on AWAITING the
# approval panel below is asking a human to vet a `final/` write for
# `active_app_id`, and retargeting the box there leaves that panel and the files
# pane under it describing two different applications, at the one moment the
# surrounding context has to be right.

with st.container(horizontal=True):
    app_id = st.text_input(
        "Application id",
        key="app_id_input",
        placeholder="nsf-aisl-2026",
        disabled=turn_locked,
        help="Also the LangGraph thread id. Reuse it to continue a run, or to "
        "browse an earlier one below.",
    )
    # Only worth drawing once there is something to pick, and it never gates the
    # id box: typing an id that does not exist yet is how a new run is named.
    if existing_ids:
        st.selectbox(
            "Browse an existing application",
            existing_ids,
            index=None,
            key="browse_pick",
            on_change=_pick_application,
            placeholder="Pick one to read…",
            disabled=turn_locked,
            help="Fills the id box on the left. Reading an application does not "
            "start a run.",
        )

with st.form("draft", border=True):
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
        # `turn_locked`, not `busy` -- this is a door into the parked state the
        # id box and the follow-up input are already gated against. Submitting
        # on AWAITING does not resume the interrupt: it sends a fresh
        # `draft_request` on the same thread, abandoning the pending
        # submission-bound write, and clears `activity` on the way so the feed
        # keeps no trace that one was ever pending. Locking the id box does not
        # cover it -- the brief is rebuilt from the id that box still holds.
        #
        # Only the phases this session knows about, though; the handler's
        # `parked` check is what actually holds. Left enabled on STOPPED and
        # FAILED on purpose: their banners tell you to re-submit the same id,
        # which is right whenever no interrupt survived, and the handler
        # refuses the case where one did.
        disabled=bool(missing) or turn_locked,
        help="Resolve the pending approval below before starting a new run."
        if turn_locked and not busy
        else None,
    )

# --- Reserve output slots before any slow work -------------------------------

activity_slot = st.container()
status_slot = st.container()
discovery_slot = st.container()
results_slot = st.container()

if discover_submitted:
    scan_id = scan_id.strip()
    try:
        if not scan_id:
            msg = "A scan id is required."
            raise ValueError(msg)

        # The boundary, for the same reason the drafting handler applies it to
        # an application id: this id names a directory the shortlist below
        # joins onto a real path, and pathlib discards the base for an absolute
        # right operand.
        opportunities_dir(settings, scan_id)
    except ValueError as exc:
        st.session_state.error = str(exc)
        st.session_state.phase = FAILED
    else:
        # No `parked_state` check here, and its absence is deliberate rather
        # than an omission of invariant 11's rule. That check exists because a
        # fresh brief on a parked thread discards the pending write; a scan
        # runs on `discovery_thread_id(scan_id)`, a thread nothing can be
        # pending on, because `backends.discovery_permissions` returns no
        # interrupt rule at any setting.
        #
        # That guarantee is a property of the rules, not of the roster. Arguing
        # it from "this graph has no drafter" was wrong and briefly true only by
        # luck: the orchestrator has `write_file` itself, and the rules it used
        # to share interrupt on `/applications/*/final/**`. It is enforced in
        # one place now and pinned by
        # `test_a_scan_can_never_park_on_an_approval`, which is what this
        # missing read is entitled to rely on.
        st.session_state.activity = []
        st.session_state.error = ""
        st.session_state.active_scan_id = scan_id
        st.session_state.active_kind = DISCOVER
        st.session_state.payload = {
            "messages": [
                {
                    "role": "user",
                    "content": discovery_request(
                        scan_id,
                        focus=focus.strip() or None,
                        agencies=agencies.strip() or None,
                        notes=scan_notes.strip() or None,
                    ),
                }
            ]
        }
        st.session_state.phase = RUNNING
        # Rerun before streaming, for the reason the drafting handler does:
        # every control on screen was drawn from a `busy` read at the top of
        # this pass, so they are all still live while this pass streams.
        st.rerun()

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

        # Ask the graph, not the phase. `turn_locked` greys this button out
        # whenever *this* session remembers parking on an interrupt, but that
        # memory is the weakest thing in the room: a page reload starts a fresh
        # session at IDLE, a second tab has its own, `grant-writer chat` has
        # none, and STOPPED and FAILED are both inferred from a pass that
        # stopped rather than from the graph -- so either can be reached with an
        # interrupt already committed. In every one of those the button is
        # enabled and the thread is still parked, and submitting would send a
        # fresh brief that discards a submission-bound write a human was asked
        # to vet, with nothing on screen recording that it existed.
        #
        # The checkpoint knows, and one read answers for all of them. Route it
        # back into AWAITING rather than refusing outright: the panel that
        # renders there is the thing the user actually needs, and this is the
        # only path that puts it back on screen after a reload.
        #
        # `is_parked`, not `pending_action_requests(...)` being non-empty. That
        # returns [] for a parked thread whose payload could not be read as
        # readily as for one with nothing pending, so asking it here fails open
        # in exactly the case the blind panel exists for -- see invariant 5.
        parked, unreadable = parked_state(app_id, profile, approve, search)
        if unreadable:
            # The read failed, so whether a write is pending is unknown -- and
            # unknown has to be treated as pending. Raised rather than handled
            # inline so it lands in the banner below with the rest of this
            # block's failures; the phase stays out of AWAITING because nothing
            # here confirmed an interrupt to approve.
            msg = (
                f"Could not read the checkpoint for `{app_id}` ({unreadable}). "
                "Not starting a turn: a run may be waiting on an approval, and "
                "starting one would abandon that pending write rather than "
                "resolve it. Try again — if it repeats, the cause is not a "
                "transient lock on the file the CLI shares."
            )
            raise ValueError(msg)

        if parked:
            # The feed belongs to whichever thread this session last ran. Kept
            # when that is the thread being resumed, cleared when it is not:
            # otherwise A's plan and file writes sit under an approval panel
            # asking about a `final/` write for B, with the results caption
            # silent because `browse_id` and `active_app_id` now agree.
            if app_id != st.session_state.active_app_id:
                st.session_state.activity = []
            st.session_state.active_app_id = app_id
            # The interrupt being recovered belongs to a drafting thread -- it
            # is a `final/` write -- so the panel must read that thread and not
            # whichever scan this session last ran. See `_active_thread_id`.
            st.session_state.active_kind = DRAFT
            st.session_state.error = ""
            st.session_state.phase = AWAITING
            st.rerun()

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
        st.session_state.active_kind = DRAFT
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

# What the last turn was about, for prose. `active_ref` is the id a human
# typed, never a thread id -- `_active_thread_id` owns that, and the two differ
# for a scan on purpose. `active_noun` exists because the banners below are the
# only place on the page where a scan and an application would otherwise be
# indistinguishable, and "re-submit the same id" is advice that points at a
# different box depending on which one it was.
active_discovering = st.session_state.active_kind == DISCOVER
active_ref = (
    st.session_state.active_scan_id
    if active_discovering
    else st.session_state.active_app_id
)
active_noun = "scan" if active_discovering else "application"

with activity_slot:
    st.subheader("Activity", anchor=False)
    feed = st.container(height=440, border=True, autoscroll=True)
    with feed:
        render_feed(st.session_state.activity)
        # AWAITING is excluded, not just RUNNING. The reload-recovery path lands
        # there with an empty feed -- a fresh session has no events -- and this
        # would then invite the reader to submit, above a disabled button, under
        # an approval panel that is the one thing they can actually act on.
        if not st.session_state.activity and st.session_state.phase not in (
            RUNNING,
            AWAITING,
        ):
            st.caption("Nothing yet. Submit a solicitation above to start a run.")
    # Nested inside a container on purpose. Called from the main body,
    # `st.chat_input` pins itself to the bottom of the viewport and would float
    # over the file browser -- which is the surface you actually read after a
    # run. This is not a chat app; the follow-up belongs to the feed it
    # continues, so it renders inline under it.
    followup = st.chat_input(
        # Names whichever graph is being continued. A follow-up goes to the
        # thread that last ran, so on a scan this box refines the search rather
        # than the drafts -- saying which avoids a message meant for one
        # landing on the other.
        (
            f"Ask for another sweep of {active_ref}…"
            if active_discovering
            else f"Ask for a revision to {active_ref}…"
        )
        if active_ref
        else "Start a run above, then refine it here…",
        key="followup_input",
        # `turn_locked` carries the AWAITING half of this: the graph is parked
        # on an interrupt there, and a fresh message starts a new turn rather
        # than answering it -- abandoning the pending write instead of approving
        # or rejecting it. Resolve the approval first; its panel is directly
        # below. Shared rather than spelled out here, so a phase added to that
        # expression reaches this input too; the extra clause is this control's
        # own precondition, that there is a thread to continue at all.
        disabled=(turn_locked or bool(missing) or not active_ref),
        # Closes the window inside the submitting pass itself, before the rerun
        # below lands and `busy` takes over: `disabled` is computed from a phase
        # read at the top of this pass, so without this a second submission here
        # aborts the pass at the next element call and drops the turn to STOPPED.
        submit_mode="disable",
    )
    st.caption(
        "Long runs can be stopped from Streamlit's toolbar and picked up later "
        "with the same application id — progress is checkpointed each step."
    )

# `st.chat_input` returns exactly what was typed, unstripped, so a stray space
# submitted as truthy and started a whole billed turn on a message with no
# content in it -- and echoed the blank into the feed on the way. Normalised
# before the guard so everything below sees the same text the agent would.
followup = (followup or "").strip()

if followup:
    # The same question the submit handler asks, for the same reason, because
    # this is the other way to start a turn. `turn_locked` disables this input
    # on AWAITING, but AWAITING is only what *this* session remembers: STOPPED
    # and FAILED are both inferred from a pass that stopped rather than from the
    # graph, so either can be reached with an interrupt already committed -- and
    # the STOPPED banner points the user straight at this box. Sending there
    # abandons the pending write exactly as a fresh brief would.
    # Only a drafting thread can be parked, so only a drafting follow-up has
    # anything to abandon: `backends.discovery_permissions` returns no
    # interrupt rule at any setting, which is enforced there and pinned by
    # `test_a_scan_can_never_park_on_an_approval`. Asking anyway would build
    # the wrong graph to read the checkpoint with and return False every time.
    parked, unreadable = (
        (False, "")
        if active_discovering
        else parked_state(st.session_state.active_app_id, profile, approve, search)
    )
    if unreadable:
        # Same refusal as the submit handler, for the same reason: an
        # unreadable checkpoint cannot rule out a pending write, so the message
        # is not sent. Not appended to the feed either -- see below.
        st.session_state.error = (
            f"That message was not sent — the checkpoint for "
            f"`{st.session_state.active_app_id}` could not be read "
            f"({unreadable}), so a pending approval cannot be ruled out. "
            "Try again — if it repeats, the cause is not a transient lock on "
            "the file the CLI shares."
        )
        st.session_state.phase = FAILED
        st.rerun()

    if parked:
        # Not appended to the feed: it was never sent, and a prompt echoed there
        # reads as one the agent has seen and is answering.
        st.session_state.error = (
            "That message was not sent — this run is waiting on an approval, "
            "and a new message would abandon the pending write rather than "
            "answer it. Resolve the approval below, then ask again."
        )
        st.session_state.phase = AWAITING
        st.rerun()

    st.session_state.activity.append(Event(PROMPT, detail=followup))
    # A plain turn on the existing thread, the same payload shape `cli._chat`
    # sends -- not another `draft_request`, which would re-brief the agent to
    # start the whole process over. `active_app_id` is left alone, so the run
    # block below keeps the same thread_id and the checkpoint carries the plan,
    # todos, and history that `grant-writer chat --app-id X` would have resumed.
    st.session_state.payload = {"messages": [{"role": "user", "content": followup}]}
    st.session_state.error = ""
    st.session_state.phase = RUNNING
    # Rerun before streaming, for the reason the form handler does: `busy` was
    # read at the top of this pass, before this handler set the phase, so every
    # control on screen right now was drawn enabled -- the input just above it
    # included. The next pass draws them disabled and runs the turn.
    st.rerun()

# --- Run one turn ------------------------------------------------------------

if st.session_state.phase == RUNNING and st.session_state.payload is not None:
    turn_payload = st.session_state.payload
    # Consume before running: a crash must not leave a payload that replays.
    st.session_state.payload = None
    with feed, st.spinner("Agent is working…", show_time=True):
        try:
            # `get_agent` inside the try, the third site to need it and the one
            # that had it outside: it builds models and opens the SQLite
            # connection, so a read-only `.grant_writer/` or a dropped key
            # raises here rather than in `stream_turn`. That mattered most on
            # the path the blind approval panel opens. AWAITING has no enabled
            # way out but that panel, its Reject needs no readable request to
            # send -- and Reject lands *here*, phase already RUNNING with the
            # payload consumed. Outside the try the raise escaped as a
            # traceback, the resume Command was gone, and the next pass
            # inferred STOPPED, whose banner invites re-submitting the same id
            # over an interrupt still sitting in the checkpoint: the exact
            # abandonment invariant 11 exists to prevent, reached through the
            # one control that was supposed to be the way out.
            # Which builder, decided by the turn's own kind rather than by
            # which id happens to be set: both can be set at once, since
            # browsing a scan does not clear the application that last ran.
            builder = (
                get_discovery_agent
                if st.session_state.active_kind == DISCOVER
                else get_agent
            )
            agent = builder(profile, approve, search)
            config = {
                "configurable": {"thread_id": _active_thread_id()},
                "recursion_limit": int(recursion_limit),
            }
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
        # Read the checkpoint here, in the full app run, and hand the result to
        # the fragment -- see approval_panel on why it does not fetch its own.
        #
        # Guarded because AWAITING is the one phase with no enabled way out:
        # every control that could start a turn is locked, so the panel below is
        # the only exit, and an exception escaping this read takes the panel with
        # it. The page then shows a traceback over a form it cannot submit, an id
        # box it cannot edit, and no Approve or Reject at all -- and every rerun
        # re-throws, because nothing left on screen can change the phase.
        # `agent.get_state` reads the SQLite checkpoint the CLI shares, so
        # `database is locked` is a live possibility, not a hypothetical.
        #
        # Falling through to the blind panel is what restores the exit: Reject
        # needs no readable request to send, and releases the graph without
        # writing. Approve stays behind its acknowledgement checkbox.
        #
        # `get_agent` is inside the try, not above it. It builds models and
        # opens the SQLite connection (see agent._build_checkpointer), so a
        # read-only `.grant_writer/` or a dropped ANTHROPIC_API_KEY raises there
        # rather than in the read below -- one line outside the guard, producing
        # the identical unrecoverable page this whole block exists to prevent.
        try:
            # The same builder switch the run block uses, though AWAITING
            # should only ever be reachable from a drafting turn --
            # `discovery_permissions` returns no interrupt rule, so a scan
            # cannot park. Switching anyway is a one-line hedge against that
            # stopping being true: reading a thread through the wrong graph
            # object means a different roster and a different state shape than
            # the one that produced the interrupt, and the failure would land
            # in the one phase with no other way out. The thread id goes
            # through `_active_thread_id` for the same reason, so the panel and
            # the resume it queues address the same row.
            builder = (
                get_discovery_agent
                if st.session_state.active_kind == DISCOVER
                else get_agent
            )
            agent = builder(profile, approve, search)
            config = {"configurable": {"thread_id": _active_thread_id()}}
            pending = pending_action_requests(agent, config)
            unreadable = ""
        except Exception as exc:  # noqa: BLE001 - the panel is the only exit
            pending = []
            unreadable = f"{type(exc).__name__}: {exc}"
        approval_panel(pending, unreadable)

    elif st.session_state.phase == STOPPED:
        st.warning(
            f"Run stopped before it finished. Progress for {active_noun} "
            f"`{active_ref}` is checkpointed — carry on from the box under the "
            f"activity feed, or re-submit the same {active_noun} id to send the "
            "opening brief again.",
            icon=":material/pause_circle:",
        )

    elif st.session_state.phase == DONE:
        st.success(
            f"Run finished for {active_noun} `{active_ref}`. Review every "
            "number, citation, and `[NEEDS INPUT]` marker before acting on it.",
            icon=":material/task_alt:",
        )

# --- Opportunity shortlist ----------------------------------------------------

# Follows the live scan box, exactly as `browse_id` follows the live id box:
# reading a previous scan must not cost a billed turn. The fallback keeps a
# finished scan on screen if the box is cleared.
browse_scan_id = scan_id.strip() or st.session_state.active_scan_id

with discovery_slot:
    if profile == "local" and browse_scan_id:
        st.subheader("Opportunity shortlist", anchor=False)
        try:
            # Same boundary as the write side. Without it a scan id of ".."
            # lists the repo root, and `render_file`'s download branch would
            # hand `.env` -- live API keys -- to the browser.
            scan_dir = opportunities_dir(settings, browse_scan_id)
        except ValueError as exc:
            st.warning(str(exc), icon=":material/warning:")
        else:
            # Read and rank here, in the full app run, then hand the result to
            # the fragment -- see `opportunity_browser` on why it reads none of
            # its own. Ranking is pure and lives in `opportunities`, because
            # the agent never stated an order and the CLI computes the same one
            # from the same two calls.
            ranked = rank_opportunities(read_scored_opportunities(scan_dir))
            files = scan_files(scan_dir)
            if not files:
                st.caption(f"Nothing in `opportunities/{browse_scan_id}/` yet.")
            else:
                opportunity_browser(scan_dir, ranked, count_gaps(files))


# --- Application files -------------------------------------------------------

# `app_id` is a live local now that its widget sits outside the form, so picking
# or typing points the browser on the same pass -- no session-state round trip,
# and no dependence on a submit having happened. The fallback keeps a finished
# run's output on screen if the selection is cleared.
browse_id = app_id.strip() or st.session_state.active_app_id

with results_slot:
    st.subheader("Application files", anchor=False)

    if profile == "server":
        st.info(
            "The `server` profile keeps drafts in ephemeral graph state, not on "
            "disk. Switch to `local` to browse files.",
            icon=":material/cloud_off:",
        )
    elif not browse_id:
        # "Enter", not "Pick": the picker beside the box is only drawn once
        # `existing_ids` is non-empty, and this branch is exactly the state a
        # fresh install starts in -- no applications, so no picker to point at.
        st.caption("Enter an application id above to browse its files.")
    else:
        # The one thing the move out of the form gave up. `browse_id` follows a
        # live widget; the graph still runs on `active_app_id`, which only the
        # submit handler assigns. So after a run on A, picking B points this
        # pane at B while the follow-up box above -- and any pending approval --
        # still belong to A. Both behaviours are wanted (read an old draft
        # without spending a turn; keep a follow-up on the thread it continues),
        # so the divergence is allowed and said out loud rather than resolved by
        # silently retargeting one of them.
        if (
            st.session_state.active_app_id
            and browse_id != st.session_state.active_app_id
        ):
            # Name the control that actually belongs to `active_app_id` in the
            # phase the reader is in. On AWAITING the follow-up box is disabled,
            # so pointing at it explains the split with the one control on
            # screen that cannot demonstrate it -- and the panel that *is*
            # asking about a `final/` write for that application goes unnamed,
            # in the state where confusing the two is most expensive.
            st.caption(
                f"Reading `{browse_id}`. The approval above is for "
                f"`{st.session_state.active_app_id}`."
                if st.session_state.phase == AWAITING
                else f"Reading `{browse_id}`. The follow-up box above still "
                f"continues `{st.session_state.active_app_id}`."
            )
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
