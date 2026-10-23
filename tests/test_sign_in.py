"""Sign-in, and the limits a signed-in turn is held to (invariant 26, E4).

The identity cases are pure. The page cases run the real script headlessly
with `identity.current_user` patched, because `AppTest` cannot complete an
OIDC exchange: what they pin is everything the page does with the answer --
the gate, the forced profile, and the one limit check every turn passes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from grant_writer import config
from grant_writer.config import _SAFE_ID, PROJECT_ROOT, user_thread_id
from grant_writer.identity import NotSignedIn, admission, current_user, profile_for
from grant_writer.runs import LimitReached, UsageMeter

ALICE = "zz-pytest-signed-in"
APP = "zz-pytest-limited"
ISSUER = "https://accounts.example.com"


# --- Who is signed in ---------------------------------------------------------------


def test_no_auth_configured_is_the_single_user_app():
    """Streamlit leaves `is_logged_in` out entirely when there is no `[auth]`
    section, which is what `AppTest` and a laptop both see."""
    assert current_user({}) is None
    assert current_user({"email": "test@example.com"}) is None


@pytest.mark.parametrize("flag", [False, None, "true", 1])
def test_configured_but_not_signed_in_raises_rather_than_going_single_user(flag):
    """Pins invariant 26.

    None here would put an unauthenticated visitor in the shared single-user
    tree with no budget -- so anything but a literal True refuses.
    """
    with pytest.raises(NotSignedIn):
        current_user({"is_logged_in": flag, "iss": ISSUER, "sub": "1"})


def test_a_signed_in_user_is_a_stable_pseudonym_of_issuer_and_subject():
    """Pins invariant 26.

    Not the email: the id lands in thread ids, checkpoint rows and trace
    metadata, and an address can change hands where `sub` cannot.
    """
    claims = {
        "is_logged_in": True,
        "iss": ISSUER,
        "sub": "1",
        "email": "a@example.com",
    }
    user = current_user(claims)
    assert user is not None and _SAFE_ID.match(user)
    assert "example" not in user
    assert current_user(dict(claims, email="b@example.com")) == user
    assert current_user(dict(claims, sub="2")) != user
    assert current_user(dict(claims, iss="https://other.example")) != user


@pytest.mark.parametrize("missing", ["iss", "sub"])
def test_a_session_without_issuer_or_subject_is_refused(missing):
    claims = {"is_logged_in": True, "iss": ISSUER, "sub": "1"}
    claims[missing] = ""
    with pytest.raises(ValueError, match="issuer and subject"):
        current_user(claims)


def test_signed_in_always_means_the_server_profile():
    """Pins invariant 26.

    `local` would write every user's drafts into one directory tree.
    """
    assert profile_for("u1", "local") == "server"
    assert profile_for(None, "local") == "local"
    assert profile_for(None, "server") == "server"


# --- The page -------------------------------------------------------------------------


class _Ledger:
    """A run table that records what the page asked of it."""

    def __init__(self, refuse: Exception | None = None):
        self.refuse = refuse
        self.begun: list[dict] = []
        self.finished: list[tuple[str, str, UsageMeter]] = []
        self.approvals: list[tuple[list, dict]] = []
        self.refuse_record: Exception | None = None
        self.error_types: list[str | None] = []

    def begin(self, **kwargs):
        if self.refuse is not None:
            raise self.refuse
        self.begun.append(kwargs)
        return f"run-{len(self.begun)}"

    def finish(self, run_id, outcome, meter, *, error_type=None, **_kwargs):
        self.finished.append((run_id, outcome, meter))
        self.error_types.append(error_type)

    def record_approvals(self, approvals, **kwargs):
        if self.refuse_record is not None:
            raise self.refuse_record
        self.approvals.append((approvals, kwargs))


class _Agent:
    def __init__(self, parked: list[str] | None = None):
        self.parked = parked or []
        self.streamed: list[tuple[object, dict]] = []
        self.built_with: list[str] = []

    def get_state(self, _config):
        requests = [
            {"name": "write_file", "args": {"file_path": p, "content": "Final."}}
            for p in self.parked
        ]
        interrupts = [SimpleNamespace(value={"action_requests": requests})]
        return SimpleNamespace(
            tasks=[SimpleNamespace(interrupts=interrupts)] if self.parked else []
        )

    def stream(self, payload, *, config, **_kwargs):
        self.streamed.append((payload, config))
        return iter(())


def _app(monkeypatch, *, user, agent=None, ledger=None, admit=True):
    streamlit = pytest.importorskip("streamlit", reason="dev dependency; run `uv sync`")
    from streamlit.testing.v1 import AppTest

    streamlit.cache_resource.clear()
    monkeypatch.setattr("grant_writer.config.require_api_keys", lambda **_k: [])

    def who(*_args):
        if isinstance(user, BaseException):
            raise user
        return user

    monkeypatch.setattr("grant_writer.identity.current_user", who)
    if admit:
        monkeypatch.setattr("grant_writer.identity.admission", lambda *_a, **_k: None)
    agent = agent or _Agent()

    def build(settings):
        agent.built_with.append(settings.backend_profile)
        return agent

    monkeypatch.setattr("grant_writer.agent.build_agent", build)
    monkeypatch.setattr(
        "grant_writer.agent.build_run_ledger", lambda _s: ledger or _Ledger()
    )
    return AppTest.from_file(str(PROJECT_ROOT / "streamlit_app.py"), default_timeout=60)


def _button(app, label):
    return next(button for button in app.button if button.label == label)


def _labels(app):
    return {button.label for button in app.button}


def _draft(app):
    app.run()
    app.text_input(key="app_id_input").set_value(APP)
    _button(app, "Draft proposal").click().run()


def test_an_unsigned_visitor_sees_only_the_sign_in_prompt(monkeypatch):
    """Pins invariant 26.

    No form, no sidebar, no graph built: nothing below the gate may run until
    the user is known, because every thread id and directory derives from it.
    """
    agent = _Agent()
    app = _app(monkeypatch, user=NotSignedIn(), agent=agent)
    app.run()
    assert not app.exception
    assert _labels(app) == {"Sign in"}
    assert not app.selectbox and not agent.built_with


def test_a_shared_database_without_sign_in_will_not_run(monkeypatch):
    """Pins invariant 26.

    "No sign-in, no deployment", enforced: with `DATABASE_URL` set, the
    single-user app would hand every visitor every thread and no budget.
    """
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://db.invalid/x")
    app = _app(monkeypatch, user=None)
    app.run()
    assert not app.exception
    assert "no sign-in" in app.error[0].value
    assert "Draft proposal" not in _labels(app)


def test_a_signed_in_turn_runs_on_server_and_is_metered(monkeypatch):
    """Pins invariant 26.

    No profile picker; the graph built for `server`; one slot claimed on the
    user's own thread before streaming; the meter attached; the run recorded.
    """
    agent, ledger = _Agent(), _Ledger()
    app = _app(monkeypatch, user=ALICE, agent=agent, ledger=ledger)
    _draft(app)

    assert not app.exception
    assert not [s for s in app.selectbox if s.label == "Backend profile"]
    assert set(agent.built_with) == {"server"}
    (begun,) = ledger.begun
    assert begun["user_id"] == ALICE and begun["kind"] == "draft"
    assert begun["thread_id"] == user_thread_id(ALICE, APP)
    (_payload, run_config) = agent.streamed[0]
    assert any(isinstance(cb, UsageMeter) for cb in run_config["callbacks"])
    assert [(run_id, outcome) for run_id, outcome, _ in ledger.finished] == [
        ("run-1", "completed")
    ]


@pytest.mark.parametrize(
    ("refusal", "shown"),
    [
        (LimitReached("3 runs are in progress"), "3 runs are in progress"),
        # Unknown is refused, never run unmetered: `parked_state`'s rule.
        (OSError("database is locked"), "Could not check this deployment's limits"),
    ],
)
def test_a_refused_turn_never_reaches_the_model(monkeypatch, refusal, shown):
    """Pins invariant 26."""
    agent = _Agent()
    app = _app(monkeypatch, user=ALICE, agent=agent, ledger=_Ledger(refusal))
    _draft(app)

    assert not app.exception
    assert not agent.streamed
    assert shown in app.error[0].value
    assert app.session_state["phase"] == "failed"


def test_a_refused_follow_up_is_not_left_in_the_feed(monkeypatch):
    """It was never sent, so it must not read as a message the agent is
    answering -- the same rule the parked check keeps."""
    agent, ledger = _Agent(), _Ledger()
    app = _app(monkeypatch, user=ALICE, agent=agent, ledger=ledger)
    _draft(app)
    ledger.refuse = LimitReached("Today's limit")
    app.chat_input(key="followup_input").set_value("Tighten the budget.").run()

    assert not app.exception
    assert len(agent.streamed) == 1
    assert "Tighten the budget." not in [
        e.detail for e in app.session_state["activity"]
    ]
    assert "Today's limit" in app.error[0].value


def test_a_refused_approval_returns_to_the_panel(monkeypatch):
    """Pins invariants 11 and 26.

    The panel is AWAITING's only exit. A refusal landing on FAILED would offer
    a fresh brief over the committed interrupt -- invariant 11's abandonment --
    so a refused resume goes back to the panel, where the same decision can be
    sent again once a slot frees.
    """
    agent = _Agent(parked=[f"/applications/{APP}/final/p.md"])
    ledger = _Ledger(LimitReached("3 runs are in progress"))
    app = _app(monkeypatch, user=ALICE, agent=agent, ledger=ledger)
    _draft(app)
    assert app.session_state["phase"] == "awaiting"

    _button(app, "Approve").click().run()

    assert not app.exception
    assert not agent.streamed
    assert app.session_state["phase"] == "awaiting"
    assert "Approve" in _labels(app)
    assert "3 runs are in progress" in app.error[0].value


def test_a_single_user_turn_is_not_metered(monkeypatch):
    """The laptop case is untouched: no run table, no callbacks."""
    agent = _Agent()

    def no_ledger(_settings):
        raise AssertionError("a single-user turn reached the run table")

    app = _app(monkeypatch, user=None, agent=agent)
    monkeypatch.setattr("grant_writer.agent.build_run_ledger", no_ledger)
    _draft(app)
    assert not app.exception
    (_payload, run_config) = agent.streamed[0]
    assert "callbacks" not in run_config


# --- Who is allowed in ---------------------------------------------------------------

ORG = frozenset({"example.org"})
NOBODY: frozenset[str] = frozenset()


def _claims(email="Pat@Example.org", verified=True):
    return {
        "is_logged_in": True,
        "iss": ISSUER,
        "sub": "1",
        "email": email,
        "email_verified": verified,
    }


def test_an_empty_allowlist_admits_nobody():
    """Pins invariant 26.

    "No list" read as "everyone" would admit any account the provider knows --
    the whole internet, for a client open to any Google account.
    """
    refusal = admission(_claims(), domains=NOBODY, emails=NOBODY)
    assert refusal is not None and "No one is allowed" in refusal


def test_a_verified_address_in_an_allowed_domain_is_admitted():
    assert admission(_claims(), domains=ORG, emails=NOBODY) is None
    assert admission(_claims(verified="true"), domains=ORG, emails=NOBODY) is None


@pytest.mark.parametrize(
    "claims",
    [
        _claims(verified=False),
        _claims(verified=None),
        _claims(verified="false"),
        {k: v for k, v in _claims().items() if k != "email_verified"},
        _claims(email=None),
        _claims(email="no-at-sign"),
    ],
)
def test_an_unverified_or_missing_address_is_refused(claims):
    """Pins invariant 26.

    An address the user can set to anything is how a stranger claims the
    organisation's domain, so verification is required, not assumed.
    """
    refusal = admission(claims, domains=ORG, emails=NOBODY)
    assert refusal is not None and "verified email" in refusal


@pytest.mark.parametrize(
    "email",
    [
        "pat@other.org",
        "pat@sub.example.org",
        "pat@example.org.evil.com",
        "pat@xexample.org",
    ],
)
def test_the_domain_must_match_exactly(email):
    """Pins invariant 26.

    A suffix match would admit `example.org.evil.com` and `xexample.org`; a
    subdomain is somebody else's to vouch for.
    """
    refusal = admission(_claims(email=email), domains=ORG, emails=NOBODY)
    assert refusal is not None and email not in refusal


def test_a_listed_address_is_admitted_from_any_domain():
    """The way in for an outside collaborator, and only for that address."""
    emails = frozenset({"guest@partner.org"})
    assert (
        admission(_claims(email="Guest@Partner.org"), domains=ORG, emails=emails)
        is None
    )
    assert admission(_claims(email="other@partner.org"), domains=ORG, emails=emails)


def test_the_lists_are_read_lowercased_and_without_a_leading_at(monkeypatch):
    monkeypatch.setenv("GRANT_WRITER_ALLOWED_DOMAINS", " @Example.ORG, ,other.org ")
    assert config._allowlist("GRANT_WRITER_ALLOWED_DOMAINS", strip="@") == {
        "example.org",
        "other.org",
    }


@pytest.mark.parametrize(
    ("domains", "shown"),
    [
        (NOBODY, "No one is allowed"),
        # AppTest's own `st.user` carries an email but no `email_verified`,
        # which is exactly a provider that does not vouch for the address.
        (frozenset({"example.com"}), "verified email"),
    ],
)
def test_a_signed_in_account_off_the_list_sees_only_the_refusal(
    monkeypatch, domains, shown
):
    """Pins invariant 26.

    Through the real `admission`, on the page: refused before any form or
    graph exists, with only a way to sign out.
    """
    monkeypatch.setattr(config, "ALLOWED_DOMAINS", domains)
    agent = _Agent()
    app = _app(monkeypatch, user=ALICE, agent=agent, admit=False)
    app.run()
    assert not app.exception
    assert shown in app.error[0].value
    assert _labels(app) == {"Sign out"}
    assert not agent.built_with
