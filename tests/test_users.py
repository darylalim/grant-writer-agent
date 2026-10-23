"""Two users who name an application alike must not reach each other's work.

Invariant 23, and the security cases E2 calls essential. Today's single-user
app passes `None` everywhere and is pinned unchanged; the cases that matter
impersonate two users through `identity.current_user` -- the one seam sign-in
fills -- and drive every entry point a human has: the run form, the follow-up
input, the approval panel, and the file browser, plus the real-disk tools that
write past the permission rules.

The AppTest cases build their own `_app_test` rather than importing
`test_frontends`'s: test modules here import nothing from each other. It is
the same three lines, for the same reasons given there.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from grant_writer import cli, config
from grant_writer.config import (
    Settings,
    application_dir,
    application_ids,
    discovery_thread_id,
    opportunities_dir,
    opportunity_scan_ids,
    thread_user_id,
    user_content_root,
    user_thread_id,
)
from grant_writer.tools import (
    _DISCOVERY_WRITE_DIRS,
    _DRAFTING_WRITE_DIRS,
    _resolve_output_path,
    build_extract_pdf_tool,
    build_fetch_opportunity_tool,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

ALICE = "zz-pytest-alice"
BOB = "zz-pytest-bob"
APP = "zz-pytest-shared-name"
ALICE_THREAD = user_thread_id(ALICE, APP)
BOB_THREAD = user_thread_id(BOB, APP)
ALICE_FINAL = f"/applications/{APP}/final/alice-only.md"


# --- Thread ids ----------------------------------------------------------------


def test_two_users_with_one_application_id_get_different_threads():
    """Pins invariant 23.

    The collision itself: without a user in the thread id, two people who both
    name an application `nsf-2027` resume one conversation, one plan, one set
    of drafts and one pending approval, and nothing raises. Each half of the
    pair is checked to parse back to its own user, since the real-disk tools
    trust that parse rather than a second key.
    """
    assert ALICE_THREAD != BOB_THREAD
    assert thread_user_id(ALICE_THREAD) == ALICE
    assert thread_user_id(BOB_THREAD) == BOB
    # And composes with invariant 12's namespace rather than competing with it.
    scan = user_thread_id(ALICE, discovery_thread_id(APP))
    assert scan not in {ALICE_THREAD, BOB_THREAD, discovery_thread_id(APP)}
    assert thread_user_id(scan) == ALICE


def test_the_single_user_thread_id_is_unchanged():
    """Decision 9: the CLI, and the UI until sign-in, keep the ids they had, so
    every existing checkpoint resumes and a UI run continues in `chat`."""
    assert user_thread_id(None, APP) == APP
    assert user_thread_id(None, discovery_thread_id(APP)) == discovery_thread_id(APP)
    assert thread_user_id(APP) is None
    assert thread_user_id(discovery_thread_id(APP)) is None
    assert thread_user_id(None) is None


@pytest.mark.parametrize(
    "user_id", ["", ".", "..", "a:b", "a/b", "@a", " a", "a ", "a\nb", "é"]
)
def test_a_user_id_that_could_break_the_construction_is_refused(user_id):
    """`:` would let one user id end inside another's thread id, and `/` or `@`
    would let one name a directory that is not its own. Neither is trimmed: a
    user id comes from a session, not a text box."""
    with pytest.raises(ValueError, match="invalid user id"):
        user_thread_id(user_id, APP)
    with pytest.raises(ValueError, match="invalid user id"):
        user_content_root(Path("applications"), user_id)


@pytest.mark.parametrize(
    "thread_id", ["user:", "user:alice", "user:alice:", "user:a/b:x", "user::x"]
)
def test_a_malformed_per_user_thread_id_is_refused_rather_than_shared(thread_id):
    """Read as None, a malformed id would send a tool's write into the shared
    tree -- the fail-open direction. It raises instead."""
    with pytest.raises(ValueError):
        thread_user_id(thread_id)


# --- Directories ---------------------------------------------------------------


def test_each_users_directories_are_their_own(tmp_path):
    """Pins invariant 23.

    Thread ids alone left the disk shared: `applications/` and
    `opportunities/` are written past the permission rules (invariant 2), so
    user B's scan `x` would overwrite user A's.
    """
    settings = Settings(root=tmp_path)
    apps = settings.applications_path.resolve()
    scans = settings.opportunities_path.resolve()

    assert application_dir(settings, APP, user_id=ALICE) == apps / f"@{ALICE}" / APP
    assert application_dir(settings, APP, user_id=BOB) == apps / f"@{BOB}" / APP
    assert application_dir(settings, APP, user_id=None) == apps / APP
    assert opportunities_dir(settings, APP, user_id=ALICE) == scans / f"@{ALICE}" / APP
    assert opportunities_dir(settings, APP, user_id=None) == scans / APP


def test_no_typed_id_reaches_a_user_directory(tmp_path):
    """`@` is outside `_SAFE_ID`, so the single-user tree cannot name a user's
    folder and one user cannot climb into another's, by construction."""
    settings = Settings(root=tmp_path)
    for typed in (f"@{ALICE}", f"@{ALICE}/{APP}", f"../@{ALICE}", ".."):
        for user_id in (None, BOB):
            with pytest.raises(ValueError):
                application_dir(settings, typed, user_id=user_id)
            with pytest.raises(ValueError):
                opportunities_dir(settings, typed, user_id=user_id)


def test_each_user_lists_only_their_own_applications_and_scans(tmp_path):
    settings = Settings(root=tmp_path)
    for base in (settings.applications_path, settings.opportunities_path):
        (base / "shared-one").mkdir(parents=True)
        (base / f"@{ALICE}" / "alice-one").mkdir(parents=True)
        (base / f"@{BOB}" / "bob-one").mkdir(parents=True)

    for listing in (application_ids, opportunity_scan_ids):
        # The single-user picker drops the user directories as names its own
        # boundary refuses, rather than by a rule of its own.
        assert listing(settings, user_id=None) == ["shared-one"]
        assert listing(settings, user_id=ALICE) == ["alice-one"]
        assert listing(settings, user_id=BOB) == ["bob-one"]


# --- Real-disk tools -----------------------------------------------------------


def test_a_tool_write_lands_in_the_callers_own_directory(tmp_path):
    """Pins invariant 23.

    The graph is cached and shared by every session, so a tool learns its
    caller only from the run. Mapped here, never named by the agent.
    """
    mapped = _resolve_output_path(
        f"/applications/{APP}/rfp.md", _DRAFTING_WRITE_DIRS, tmp_path, user_id=BOB
    )
    assert mapped == tmp_path.resolve() / "applications" / f"@{BOB}" / APP / "rfp.md"
    mapped = _resolve_output_path(
        f"/opportunities/{APP}/candidates/1.md",
        _DISCOVERY_WRITE_DIRS,
        tmp_path,
        user_id=BOB,
    )
    assert mapped.is_relative_to(tmp_path.resolve() / "opportunities" / f"@{BOB}")


@pytest.mark.parametrize("user_id", [None, BOB])
@pytest.mark.parametrize(
    "out_path",
    [
        f"/applications/@{ALICE}/{APP}/final/a.md",
        f"/applications/{APP}/../@{ALICE}/{APP}/a.md",
        f"/applications/x/@{ALICE}/a.md",
    ],
)
def test_a_tool_cannot_name_a_user_directory(tmp_path, out_path, user_id):
    """The mapping adds the user segment; an `@` segment of the agent's own is
    refused rather than nested, from the single-user tree as well."""
    with pytest.raises(ValueError, match="user directory"):
        _resolve_output_path(out_path, _DRAFTING_WRITE_DIRS, tmp_path, user_id=user_id)


def test_a_symlink_in_a_users_directory_cannot_carry_a_write_out(tmp_path):
    """Invariant 2's resolve-then-check, applied again after the mapping."""
    alice = tmp_path / "applications" / f"@{ALICE}"
    alice.mkdir(parents=True)
    bob = tmp_path / "applications" / f"@{BOB}"
    bob.mkdir(parents=True)
    (bob / APP).symlink_to(alice)
    with pytest.raises(ValueError, match="your own directory"):
        _resolve_output_path(
            f"/applications/{APP}/rfp.md", _DRAFTING_WRITE_DIRS, tmp_path, user_id=BOB
        )


def _blank_pdf(path: Path) -> Path:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def test_extract_pdf_text_reads_the_user_from_the_thread_id(tmp_path):
    """End to end through the real tool, with the config LangGraph hands it."""
    pdf = _blank_pdf(tmp_path / "rfp.pdf")
    root = tmp_path / "root"
    tool = build_extract_pdf_tool(root)
    args = {"pdf_path": str(pdf), "out_path": f"/applications/{APP}/rfp.md"}

    tool.invoke(args, config={"configurable": {"thread_id": BOB_THREAD}})
    tool.invoke(args, config={"configurable": {"thread_id": APP}})

    assert (root / "applications" / f"@{BOB}" / APP / "rfp.md").is_file()
    assert (root / "applications" / APP / "rfp.md").is_file()
    assert not (root / "applications" / f"@{ALICE}").exists()


def test_fetch_opportunity_reads_the_user_from_the_thread_id(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "grant_writer.tools._grants_gov_post", lambda *_a: {"id": 1, "title": "T"}
    )
    monkeypatch.setattr("grant_writer.tools._format_opportunity", lambda _d: "text")
    root = tmp_path / "root"
    tool = build_fetch_opportunity_tool(root)
    args = {"opportunity_id": "1", "out_path": f"/opportunities/{APP}/c/1.md"}

    receipt = tool.invoke(
        args, config={"configurable": {"thread_id": user_thread_id(ALICE, "s")}}
    )

    assert receipt.startswith("Wrote")
    assert (root / "opportunities" / f"@{ALICE}" / APP / "c" / "1.md").is_file()
    assert not (root / "opportunities" / APP).exists()


def test_a_malformed_thread_id_writes_nothing(tmp_path):
    pdf = _blank_pdf(tmp_path / "rfp.pdf")
    root = tmp_path / "root"
    receipt = build_extract_pdf_tool(root).invoke(
        {"pdf_path": str(pdf), "out_path": f"/applications/{APP}/rfp.md"},
        config={"configurable": {"thread_id": "user:a/b:x"}},
    )
    assert receipt.startswith("Error:")
    assert not (root / "applications").exists()


def test_the_injected_config_is_not_shown_to_the_model(tmp_path):
    """`config` is injected by `@tool`; in the schema it would invite the model
    to supply its own thread id -- and with it, its own user."""
    for tool in (
        build_extract_pdf_tool(tmp_path),
        build_fetch_opportunity_tool(tmp_path),
    ):
        assert "config" not in tool.args


# --- CLI (decisions 9 and 18) --------------------------------------------------


def _run_cli(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr("sys.argv", ["grant-writer", *argv])
    return cli.main()


class _NoGraph:
    """Fails the case if a refused command gets as far as building a graph."""

    def __call__(self, *_args, **_kwargs):
        pytest.fail("a refused command built a graph")


@pytest.mark.parametrize(
    "argv",
    [
        ("draft", "--app-id", APP),
        ("chat", "--app-id", APP),
        ("discover", "--scan-id", APP),
    ],
)
def test_the_cli_refuses_the_shared_database_without_the_operator_flag(
    monkeypatch, capsys, argv
):
    """Decision 18. With no sign-in and unprefixed ids, the CLI against the
    deployment's database would reach any thread at all."""
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://u@db/x")
    monkeypatch.setattr("grant_writer.cli.require_api_keys", lambda **_k: [])
    monkeypatch.setattr("grant_writer.cli.build_agent", _NoGraph())
    monkeypatch.setattr("grant_writer.cli.build_discovery_agent", _NoGraph())

    assert _run_cli(monkeypatch, *argv) == 1
    assert "--operator" in capsys.readouterr().err


def test_the_operator_override_is_logged_and_keeps_unprefixed_ids(monkeypatch, caplog):
    """Decisions 18 and 9 together: the override proceeds, says so in the log,
    and runs on the bare id -- the CLI never claims to be a signed-in user."""
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://u@db/x")
    monkeypatch.setattr("grant_writer.cli.require_api_keys", lambda **_k: [])
    monkeypatch.setattr("grant_writer.cli.build_agent", lambda _s: object())
    seen = []
    monkeypatch.setattr(
        "grant_writer.cli._run", lambda _agent, _payload, cfg: seen.append(cfg)
    )

    with caplog.at_level(logging.WARNING, logger="grant_writer.cli"):
        assert _run_cli(monkeypatch, "draft", "--app-id", APP, "--operator") == 0

    assert "operator override" in caplog.text and APP in caplog.text
    assert seen[0]["configurable"]["thread_id"] == APP


def test_without_a_database_the_cli_needs_no_flag(monkeypatch, caplog):
    """The laptop case is untouched: no URL, no refusal, no log line."""
    monkeypatch.setattr("grant_writer.cli.require_api_keys", lambda **_k: [])
    monkeypatch.setattr("grant_writer.cli.build_agent", lambda _s: object())
    seen = []
    monkeypatch.setattr(
        "grant_writer.cli._run", lambda _agent, _payload, cfg: seen.append(cfg)
    )

    with caplog.at_level(logging.WARNING, logger="grant_writer.cli"):
        assert _run_cli(monkeypatch, "draft", "--app-id", APP) == 0

    assert caplog.text == ""
    assert seen[0]["configurable"]["thread_id"] == APP


# --- The UI's entry points -----------------------------------------------------


def _app_test(monkeypatch, user: str | None):
    """The real script, headless, signed in as `user`.

    The credential guard is forced open, as `test_frontends._app_test` does
    when handed `monkeypatch`, and `cache_resource` is cleared so a cached
    graph from an earlier case cannot answer for this one's fake.
    """
    streamlit = pytest.importorskip("streamlit", reason="dev dependency; run `uv sync`")
    from streamlit.testing.v1 import AppTest

    streamlit.cache_resource.clear()
    monkeypatch.setattr("grant_writer.config.require_api_keys", lambda **_kwargs: [])
    monkeypatch.setattr("grant_writer.identity.current_user", lambda: user)
    return AppTest.from_file(str(PROJECT_ROOT / "streamlit_app.py"), default_timeout=60)


class _Checkpoints:
    """A stand-in graph whose checkpoint is a dict of parked threads.

    Answers `get_state` per thread id, which is the whole question: a fake that
    answered the same for every thread could not tell Alice's row from Bob's,
    and every case below would pass against code that confused them.
    """

    def __init__(self, parked: dict[str, list[str]]):
        self.parked = parked
        self.streamed: list[tuple[str, object]] = []

    def get_state(self, cfg):
        paths = self.parked.get(cfg["configurable"]["thread_id"], [])
        requests = [
            {"name": "write_file", "args": {"file_path": p, "content": f"Text {p}"}}
            for p in paths
        ]
        interrupts = [SimpleNamespace(value={"action_requests": requests})]
        return SimpleNamespace(
            tasks=[SimpleNamespace(interrupts=interrupts)] if paths else []
        )

    def stream(self, payload, *, config, **_kwargs):
        self.streamed.append((config["configurable"]["thread_id"], payload))
        return iter(())


def _button(app, label: str):
    return next(button for button in app.button if button.label == label)


def _page_text(app) -> str:
    parts = [e.value for e in (*app.markdown, *app.caption, *app.code, *app.text)]
    parts += [str(option) for radio in app.radio for option in radio.options]
    return " ".join(str(part) for part in parts)


def _alice_is_parked(monkeypatch) -> _Checkpoints:
    agent = _Checkpoints({ALICE_THREAD: [ALICE_FINAL]})
    monkeypatch.setattr("grant_writer.agent.build_agent", lambda *_a, **_k: agent)
    return agent


def test_the_run_form_never_reaches_another_users_parked_thread(monkeypatch):
    """Pins invariant 23.

    Bob submits the id Alice's run is parked under. He must start his own
    thread, not be routed into her approval -- and Alice, submitting the same
    thing, must be, which is what proves the fake can tell them apart.
    """
    agent = _alice_is_parked(monkeypatch)

    app = _app_test(monkeypatch, BOB)
    app.run()
    app.text_input(key="app_id_input").set_value(APP)
    _button(app, "Draft proposal").click().run()

    assert not app.exception
    assert app.session_state["phase"] == "done"
    assert [thread for thread, _ in agent.streamed] == [BOB_THREAD]
    assert ALICE_FINAL not in _page_text(app)

    agent.streamed.clear()
    app = _app_test(monkeypatch, ALICE)
    app.run()
    app.text_input(key="app_id_input").set_value(APP)
    _button(app, "Draft proposal").click().run()

    assert app.session_state["phase"] == "awaiting"
    assert agent.streamed == []


def test_a_follow_up_runs_on_the_senders_own_thread(monkeypatch):
    """Pins invariant 23."""
    agent = _alice_is_parked(monkeypatch)
    app = _app_test(monkeypatch, BOB)
    app.run()
    app.text_input(key="app_id_input").set_value(APP)
    _button(app, "Draft proposal").click().run()
    agent.streamed.clear()

    app.chat_input(key="followup_input").set_value("Tighten the need.").run()

    assert not app.exception
    assert [thread for thread, _ in agent.streamed] == [BOB_THREAD]


def test_the_approval_panel_shows_and_resolves_only_the_viewers_thread(monkeypatch):
    """Pins invariant 23.

    Bob's session claims an approval on Alice's application id -- a stale or
    forged `active_app_id`, the strongest position session state can give him.
    The panel reads his own row, so her pending write is never on his screen,
    and his Reject resumes his thread and leaves hers parked.
    """
    agent = _alice_is_parked(monkeypatch)
    app = _app_test(monkeypatch, BOB)
    app.run()
    app.session_state["phase"] = "awaiting"
    app.session_state["active_app_id"] = APP
    app.run()

    assert not app.exception
    assert ALICE_FINAL not in _page_text(app)
    _button(app, "Reject").click().run()

    assert [thread for thread, _ in agent.streamed] == [BOB_THREAD]
    assert agent.parked == {ALICE_THREAD: [ALICE_FINAL]}


SOLO = "zz-pytest-single-user-app"


@pytest.fixture
def alices_application():
    """Alice's drafts, on real disk under the project's own `applications/` --
    the only tree `application_dir` resolves into, as in `test_frontends` --
    beside one application in the single-user tree, so the picker has something
    to wrongly offer Bob if it lists the shared tree rather than his own."""
    alice_dir = PROJECT_ROOT / "applications" / f"@{ALICE}"
    solo_dir = PROJECT_ROOT / "applications" / SOLO
    draft = alice_dir / APP / "sections" / "alice-need.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("# Need\n\nAlice's working draft.\n", encoding="utf-8")
    solo_dir.mkdir(parents=True, exist_ok=True)
    try:
        yield
    finally:
        shutil.rmtree(alice_dir, ignore_errors=True)
        shutil.rmtree(solo_dir, ignore_errors=True)


def _picker_options(app) -> list[str]:
    """The browse picker's options; it is not drawn at all when there are none."""
    return [
        option
        for box in app.selectbox
        if box.key == "browse_pick"
        for option in box.options
    ]


@pytest.mark.usefixtures("alices_application")
def test_the_file_browser_shows_only_the_viewers_files(monkeypatch):
    """Pins invariant 23.

    Same id typed into the box; Bob is shown nothing of Alice's, and the picker
    offers him neither her application nor the single-user tree's -- only his
    own, of which he has none. Alice, typing the same, sees hers.
    """
    app = _app_test(monkeypatch, BOB)
    app.run()
    assert _picker_options(app) == []
    app.text_input(key="app_id_input").set_value(APP)
    app.run()
    assert not app.exception
    assert "alice-need.md" not in _page_text(app)

    app = _app_test(monkeypatch, ALICE)
    app.run()
    assert APP in _picker_options(app) and SOLO not in _picker_options(app)
    app.text_input(key="app_id_input").set_value(APP)
    app.run()
    assert not app.exception
    assert "alice-need.md" in _page_text(app)
