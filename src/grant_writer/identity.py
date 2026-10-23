"""Whose session a frontend is serving (invariants 23 and 26).

A module of its own, and a leaf, for one reason: it is the seam sign-in fills.
`streamlit_app.py` asks it once per need rather than keeping the answer in a
function of its own, because a function defined inside a Streamlit script is
redefined on every run and cannot be patched -- so the cases proving that two
users stay apart could not impersonate either of them. Imported by attribute
each run, like `config.application_ids`, so `monkeypatch` reaches it.

It takes the session's claims as an argument rather than reading `st.user`
itself, because the package must never import streamlit: the console script
installs without it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # For the annotation only, so this stays a leaf at run time: it imports
    # nothing from the package, like `activity.py` and `opportunities.py`.
    from grant_writer.config import BackendProfile


class NotSignedIn(Exception):
    """Sign-in is configured and this session has not completed it."""


def current_user(claims: Mapping[str, Any]) -> str | None:
    """The signed-in user's id, or None for the single-user app.

    `claims` is `st.user`: the identity provider's verified ID-token claims,
    which Streamlit fills only after `st.login` completes the OIDC exchange.
    Nothing a client can set reaches it -- not a widget, a query parameter or a
    cookie of its own choosing -- which is the whole requirement on this
    value, since anything the client can set it can set to someone else.

    Three states, read from Streamlit's own contract for `is_logged_in`: the
    key is **absent** when no `[auth]` section is configured, False when one is
    and the session has not signed in, True after it has.

    - Absent is single-user and returns None: the tree the CLI uses
      (decision 9), so a local run still resumes from `grant-writer chat`.
    - False **raises** rather than returning None. Collapsed, a deployment
      whose login page was skipped would put every visitor in one shared
      tree with one shared budget -- invariant 23's fail-open. The page
      draws the sign-in prompt and stops before any caller gets here.
    - True is a digest of the issuer and subject. Pseudonymous rather than an
      email address: the id is inside every thread id, so it reaches the
      checkpoint rows and LangSmith metadata (E2 finding 3), and `sub` is the
      claim a provider promises is stable and never reassigned, where an
      email can change hands. The issuer is mixed in so two providers' `sub`
      spaces cannot collide. The result satisfies `config._SAFE_ID`.

    Every thread id goes through `config.user_thread_id` with this, and every
    directory through `application_dir` / `opportunities_dir` with it, so two
    people who name an application alike reach different checkpoints and
    different folders.

    Raises:
        NotSignedIn: sign-in is configured and not completed.
        ValueError: signed in, but the claims carry no issuer or subject.
    """
    if "is_logged_in" not in claims:
        return None
    if claims["is_logged_in"] is not True:
        raise NotSignedIn
    issuer, subject = claims.get("iss"), claims.get("sub")
    if (
        not isinstance(issuer, str)
        or not isinstance(subject, str)
        or not (issuer and subject)
    ):
        msg = "the sign-in session carries no issuer and subject"
        raise ValueError(msg)
    digest = hashlib.sha256(f"{issuer}\n{subject}".encode()).hexdigest()
    return f"u{digest[:32]}"


def admission(
    claims: Mapping[str, Any],
    *,
    domains: frozenset[str],
    emails: frozenset[str],
) -> str | None:
    """Why a signed-in session may not use this deployment, or None if it may.

    Signing in proves only that the provider knows the account. A client
    configured for any Google account admits anyone with one -- who could then
    read the organisation profile every run loads, approve their own write to
    it, and take a fresh daily budget per account. So the account must also be
    on this deployment's list: its email's domain in `domains` (exactly; a
    subdomain is a different organisation), or the address in `emails`.

    Both lists empty is a refusal, never "everyone": a deployment that forgot
    to set one would otherwise fail open, which is invariant 26's rule.

    The email must be **verified** by the provider. An address a user can set
    to anything is precisely how a stranger would claim `@yourorg.org`, so a
    provider that does not send `email_verified` is refused rather than
    trusted. `"true"` is accepted beside `True` because some providers send
    the claim as a string. Returns a message for the page, which names no
    address: the claim is the provider's, and it would be drawn as markdown.
    """
    if not domains and not emails:
        return (
            "No one is allowed to sign in yet: set `GRANT_WRITER_ALLOWED_DOMAINS` "
            "or `GRANT_WRITER_ALLOWED_EMAILS` for this deployment."
        )
    email = claims.get("email")
    verified = claims.get("email_verified")
    if not isinstance(email, str) or "@" not in email or verified not in (True, "true"):
        return (
            "Your sign-in provider did not supply a verified email address, so "
            "this deployment cannot tell whether you belong to it."
        )
    address = email.strip().lower()
    if address in emails or address.rpartition("@")[2] in domains:
        return None
    return "This account is not on this deployment's list of allowed users."


def profile_for(user_id: str | None, chosen: BackendProfile) -> BackendProfile:
    """The backend profile a run must use: always `server` once signed in.

    `local` roots one `FilesystemBackend` at the project and writes
    `/applications/<id>/` into the shared tree, so only the real-disk tools
    would be per-user -- two signed-in users' drafts would land in one
    directory (E2 finding 1). `server` keeps drafts in thread-scoped state,
    which the per-user thread id already separates. Single-user keeps the
    choice it was given.
    """
    return "server" if user_id is not None else chosen
