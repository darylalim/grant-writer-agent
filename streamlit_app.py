"""Streamlit front end for the grant writer agent.

Runs the same graph as `grant-writer draft`, against the same SQLite
checkpoint, so a run started here continues from `grant-writer chat --app-id X`
and back again. The app id is the LangGraph thread id in both.

Deliberately outside `src/grant_writer/`: the package must never import
streamlit. That is what keeps the `ui` extra genuinely optional -- installing
the console script without it still works.

    uv sync --extra ui
    uv run --extra ui streamlit run streamlit_app.py
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
    iter_activity,
    pending_action_requests,
)
from grant_writer.agent import build_agent
from grant_writer.config import BackendProfile, persistent_settings, require_api_keys
from grant_writer.prompts import draft_request

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
IDLE, RUNNING, AWAITING, DONE, FAILED = "idle", "running", "awaiting", "done", "failed"

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

# The order WORKSPACE_CONVENTIONS lays out an application directory in, so the
# file list reads the way the agent works rather than alphabetically.
DIRECTORY_ORDER = (
    "rfp.md",
    "requirements.md",
    "research",
    "sections",
    "review",
    "final",
)

TEXT_SUFFIXES = {".md", ".txt", ".json", ".csv", ".yaml", ".yml"}

st.session_state.setdefault("activity", [])
st.session_state.setdefault("phase", IDLE)
st.session_state.setdefault("payload", None)
st.session_state.setdefault("active_app_id", "")
st.session_state.setdefault("error", "")


@st.cache_resource(show_spinner=False)
def get_agent(profile: BackendProfile, approve: bool, search: bool) -> Any:
    """Build the graph once per settings combination.

    `cache_resource` is shared across sessions, which is what we want: the
    SQLite checkpointer opens one connection for the process (with
    `check_same_thread=False`, see agent._build_checkpointer) instead of one
    per rerun. Toggling a sidebar setting rebuilds the graph, but the thread's
    history and any pending approval live in the checkpoint file, so nothing
    in flight is lost.
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
    """Queue a resume for the next rerun and trigger it."""
    st.session_state.payload = Command(resume={"decisions": decisions})
    st.session_state.phase = RUNNING
    st.rerun()


def application_files(app_dir: Path) -> list[Path]:
    """Every file in an application directory, in workspace order."""
    if not app_dir.is_dir():
        return []

    def sort_key(path: Path) -> tuple[int, str]:
        head = path.relative_to(app_dir).parts[0]
        rank = (
            DIRECTORY_ORDER.index(head)
            if head in DIRECTORY_ORDER
            else len(DIRECTORY_ORDER)
        )
        return rank, str(path.relative_to(app_dir))

    return sorted((p for p in app_dir.rglob("*") if p.is_file()), key=sort_key)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def count_gaps(files: list[Path]) -> int:
    """Unresolved `[NEEDS INPUT: ...]` markers across the drafts.

    The single most important number on this page: every one of these is a
    fact the agent refused to invent and a human still has to supply.
    """
    return sum(
        read_text(path).count("[NEEDS INPUT")
        for path in files
        if path.suffix == ".md" and path.parent.name != "review"
    )


def compliance_verdict(files: list[Path]) -> str:
    """The compliance reviewer's verdict, as it wrote it."""
    for path in files:
        if path.parent.name != "review":
            continue
        body = read_text(path)
        if "NOT-READY" in body:
            return "NOT-READY"
        if "SUBMIT-READY" in body:
            return "SUBMIT-READY"
    return "—"


# --- Sidebar: run settings ---------------------------------------------------

with st.sidebar:
    st.subheader("Run settings", anchor=False)
    profile = st.selectbox(
        "Backend profile",
        ("local", "server"),
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
        help="Pause before each submission-bound file and show it for review.",
    )
    search = st.toggle(
        "Web search",
        value=True,
        help="Funder research needs TAVILY_API_KEY. Turn off to run without it.",
    )
    recursion_limit = st.number_input(
        "Recursion limit",
        min_value=20,
        max_value=500,
        value=150,
        step=10,
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

busy = st.session_state.phase == RUNNING

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
    if not app_id.strip():
        st.session_state.error = "An application id is required."
        st.session_state.phase = FAILED
    else:
        app_id = app_id.strip()
        rfp_path = None
        if rfp is not None:
            # Keep the source PDF beside the drafts rather than in a temp file,
            # so the same app id works again tomorrow. Written by the app, not
            # the agent, so it is not subject to FilesystemPermission.
            target = settings.applications_path / app_id / rfp.name
            target.parent.mkdir(parents=True, exist_ok=True)
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

        st.session_state.activity = []
        st.session_state.error = ""
        st.session_state.active_app_id = app_id
        st.session_state.payload = payload
        st.session_state.phase = RUNNING

# --- Activity feed -----------------------------------------------------------

with activity_slot:
    st.subheader("Activity", anchor=False)
    feed = st.container(height=440, border=True, autoscroll=True)
    with feed:
        for event in st.session_state.activity:
            render_event(event)
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

# --- Approval and status -----------------------------------------------------

with status_slot:
    if st.session_state.error:
        st.error(st.session_state.error, icon=":material/error:")

    if st.session_state.phase == AWAITING:
        agent = get_agent(profile, approve, search)
        config = {"configurable": {"thread_id": st.session_state.active_app_id}}
        requests = pending_action_requests(agent, config)
        # One decision per action request, not per interrupt: the middleware
        # bundles a turn's interrupted calls into one interrupt and resume
        # requires the counts to match.
        count = max(len(requests), 1)

        with st.container(border=True):
            st.subheader("Approval required", anchor=False)
            st.caption(
                f"{count} write(s) to `final/`. These are the submission-bound "
                "files — read each one before approving."
            )
            for request in requests:
                args = request.get("args") or {}
                path = args.get("file_path") or "(unknown path)"
                with st.expander(f"{request.get('name', '?')} → {path}"):
                    content = args.get("content")
                    if content:
                        st.markdown(content)
                    else:
                        st.json(args)
            reason = st.text_input(
                "Reason",
                key="reject_reason",
                placeholder="Sent back to the agent if you reject.",
            )
            with st.container(horizontal=True):
                if st.button("Approve", type="primary", icon=":material/check:"):
                    resume_with([{"type": "approve"} for _ in range(count)])
                if st.button("Reject", icon=":material/close:"):
                    message = reason.strip() or "Rejected."
                    resume_with(
                        [{"type": "reject", "message": message} for _ in range(count)]
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
        app_dir = settings.applications_path / browse_id
        files = application_files(app_dir)
        if not files:
            st.caption(f"Nothing in `applications/{browse_id}/` yet.")
        else:
            with st.container(horizontal=True):
                st.metric("Files", len(files), border=True)
                st.metric(
                    "Needs input",
                    count_gaps(files),
                    border=True,
                    help="Unresolved `[NEEDS INPUT]` markers — facts the agent "
                    "refused to invent and a human must supply.",
                )
                st.metric(
                    "Compliance",
                    compliance_verdict(files),
                    border=True,
                    help="The compliance reviewer's verdict, from `review/`.",
                )

            names = [str(path.relative_to(app_dir)) for path in files]
            picker_col, content_col = st.columns([1, 2], vertical_alignment="top")
            with picker_col, st.container(height=480, border=True):
                selected = st.radio(
                    "File", names, key="file_pick", label_visibility="collapsed"
                )
            with content_col, st.container(height=480, border=True):
                chosen = app_dir / selected
                if chosen.suffix == ".pdf":
                    st.pdf(chosen.read_bytes(), height=430)
                elif chosen.suffix in TEXT_SUFFIXES:
                    st.markdown(read_text(chosen))
                else:
                    st.download_button(
                        "Download",
                        chosen.read_bytes(),
                        file_name=chosen.name,
                        icon=":material/download:",
                    )
