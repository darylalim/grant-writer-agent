"""Whose session a frontend is serving (invariant 23).

A module of its own, and a leaf, for one reason: it is the seam sign-in fills.
`streamlit_app.py` asks it once per need rather than keeping the answer in a
function of its own, because a function defined inside a Streamlit script is
redefined on every run and cannot be patched -- so the cases proving that two
users stay apart could not impersonate either of them. Imported by attribute
each run, like `config.application_ids`, so `monkeypatch` reaches it.
"""

from __future__ import annotations


def current_user() -> str | None:
    """The signed-in user's id, or None for the single-user app.

    Every thread id goes through `config.user_thread_id` with this, and every
    directory through `application_dir` / `opportunities_dir` with it, so two
    people who name an application alike reach different checkpoints and
    different folders.

    None until sign-in exists. That is the single-user tree the CLI uses
    (decision 9), so a local run still resumes from `grant-writer chat`. When
    sign-in lands, the id must come only from the provider's verified session
    -- `st.user` after `st.login`, or a header an authenticating proxy sets and
    strips from the client -- and never from a widget, a query parameter, or a
    cookie: anything the client can set, the client can set to someone else. It
    must also satisfy `config._SAFE_ID`, so map the provider's subject onto one
    (a digest, say) rather than passing an email address through.
    """
    return None
