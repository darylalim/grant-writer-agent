"""The solicitation reaches the agent as text in its backend (invariant 24).

Two halves. The parse is untrusted input on a shared server, so its bounds are
pinned here -- size, signature, pages, and the child process that a hung parse
is killed in. And the preload has to land where the graph's *own* backend
reads, which differs by profile; the end-to-end cases drive a real graph to
prove it, because the failure this replaced was a write that succeeded into a
place `read_file` never looked.
"""

from __future__ import annotations

import io
import os
import re
import time
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from pypdf import PdfWriter

from grant_writer import agent as agent_module
from grant_writer import solicitation
from grant_writer.agent import build_agent
from grant_writer.config import PROJECT_ROOT, Settings
from grant_writer.solicitation import (
    RFP_FILE,
    SolicitationError,
    extract_solicitation,
    preload_solicitation,
    run_isolated,
)

FIXTURE = Path(PROJECT_ROOT, "evals", "fixtures", "scmp-2026-nofo-excerpt.pdf")


def _blank_pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# Module level, so `spawn` can import them by name in the child.
def _sleep(seconds: float) -> str:
    time.sleep(seconds)
    return "finished"


def _die() -> str:
    os._exit(1)


# --- The parse -------------------------------------------------------------------


def test_a_real_solicitation_is_extracted_page_by_page():
    """Pins invariant 24.

    The shape the agent studies: a header, then a marker per page that
    requirements can cite. Through the real child process.
    """
    text = extract_solicitation(FIXTURE.read_bytes(), "nofo.pdf")
    assert text.startswith("[nofo.pdf - 10 page(s)]")
    assert "--- page 1 ---" in text and "--- page 10 ---" in text
    assert "Specialty Crop Multi-State Program" in text


def test_the_name_is_reduced_to_its_last_component():
    """The upload name is client-supplied and lands in the archive's header."""
    text = extract_solicitation(_blank_pdf(1), "../../etc/nofo.pdf")
    assert text.startswith("[nofo.pdf - 1 page(s)]")


@pytest.mark.parametrize(
    "data",
    [b"<html>not a pdf</html>", b"", b" %PDF-1.7", b"PK\x03\x04 a zip renamed"],
)
def test_the_type_is_checked_by_content_not_by_name(data):
    """Pins invariant 24.

    Every one of these arrives named `.pdf`; none starts with the signature.
    Refused before any parser runs.
    """
    with pytest.raises(SolicitationError, match="not a PDF"):
        extract_solicitation(data, "rfp.pdf")


def test_an_oversized_upload_is_refused_before_parsing(monkeypatch):
    monkeypatch.setattr(solicitation, "MAX_UPLOAD_MB", 1)
    monkeypatch.setattr(
        solicitation, "run_isolated", lambda *_a, **_k: pytest.fail("parsed")
    )
    with pytest.raises(SolicitationError, match="larger than 1 MB"):
        extract_solicitation(b"%PDF-" + b"0" * (1024 * 1024), "big.pdf")


def test_a_document_over_the_page_cap_is_refused_not_truncated(monkeypatch):
    """Dropping a tail silently is the abridgement the archive exists to prevent."""
    monkeypatch.setattr(solicitation, "MAX_PAGES", 2)
    assert "--- page 2 ---" in extract_solicitation(_blank_pdf(2), "ok.pdf")
    with pytest.raises(SolicitationError, match="has 3 pages; the limit is 2"):
        extract_solicitation(_blank_pdf(3), "long.pdf")


def test_a_corrupt_pdf_is_a_message_not_a_traceback():
    with pytest.raises(SolicitationError, match=re.escape("could not read bad.pdf")):
        extract_solicitation(b"%PDF-1.7\nthis is not a body", "bad.pdf")


def test_a_hung_parse_is_killed_at_the_timeout():
    """Pins invariant 24.

    One crafted file must not stall a process every session shares. The
    child is gone afterwards, not merely abandoned.
    """
    started = time.monotonic()
    with pytest.raises(SolicitationError, match=re.escape("longer than 0.5 seconds")):
        run_isolated(_sleep, (30,), timeout=0.5)
    assert time.monotonic() - started < 10
    import multiprocessing

    assert not multiprocessing.active_children()


def test_a_parser_that_dies_is_reported():
    with pytest.raises(SolicitationError, match="stopped unexpectedly"):
        run_isolated(_die, (), timeout=30)


def test_the_upload_cap_is_the_one_streamlit_enforces():
    """One limit, two enforcement points.

    Pins invariant 24.

    Streamlit refuses a larger upload before the script runs; the CLI has
    no such gate, so `extract_solicitation` restates it. Drifted apart, one
    frontend accepts what the other refuses.
    """
    import tomllib

    config = tomllib.loads(
        Path(PROJECT_ROOT, ".streamlit", "config.toml").read_text("utf-8")
    )
    assert config["server"]["maxUploadSize"] == solicitation.MAX_UPLOAD_MB


# --- The preload -----------------------------------------------------------------


def test_the_local_preload_writes_the_archive_into_the_application(tmp_path):
    settings = Settings(root=tmp_path)
    assert preload_solicitation(settings, "x", "TEXT", user_id=None) == {}
    assert (tmp_path / "applications" / "x" / RFP_FILE).read_text() == "TEXT"


def test_the_server_preload_writes_nothing_to_disk(tmp_path):
    settings = Settings(root=tmp_path, backend_profile="server")
    extra = preload_solicitation(settings, "x", "TEXT", user_id="alice")
    assert extra["files"][f"/applications/x/{RFP_FILE}"]["content"] == "TEXT"
    assert not (tmp_path / "applications").exists()


def test_the_local_profile_refuses_a_signed_in_preload(tmp_path):
    """Its backend reads the shared tree, so the user's copy would be invisible.

    Refused rather than written where the agent never looks -- or, worse,
    into the shared tree another user's run reads.
    """
    with pytest.raises(ValueError, match="single-user"):
        preload_solicitation(Settings(root=tmp_path), "x", "T", user_id="alice")
    assert not (tmp_path / "applications" / "x").exists()


@pytest.mark.parametrize("profile", ["local", "server"])
@pytest.mark.parametrize("app_id", ["../x", "/etc", "a/b", ""])
def test_an_unsafe_id_is_refused_in_both_profiles(tmp_path, profile, app_id):
    """The id is joined into a virtual path for `server`, too -- so the
    boundary runs there even though nothing touches disk."""
    settings = Settings(root=tmp_path, backend_profile=profile)
    with pytest.raises(ValueError):
        preload_solicitation(settings, app_id, "T", user_id=None)


def test_a_symlinked_archive_cannot_carry_the_write_out(tmp_path):
    """Invariant 2's resolve-then-check, for the one file this writer names."""
    app = tmp_path / "applications" / "x"
    app.mkdir(parents=True)
    outside = tmp_path / "elsewhere.md"
    (app / RFP_FILE).symlink_to(outside)
    with pytest.raises(ValueError, match="outside x/"):
        preload_solicitation(Settings(root=tmp_path), "x", "T", user_id=None)
    assert not outside.exists()


@pytest.mark.parametrize("profile", ["local", "server"])
def test_the_agent_reads_the_preloaded_archive(tmp_path, monkeypatch, profile):
    """Pins invariant 24.

    Through a real graph in each profile: the frontend preloads, the
    orchestrator's `read_file` finds the text. In `server` this is what the
    old tool got wrong -- it wrote `rfp.md` to disk, and the `StateBackend`
    the agent reads never saw it.
    """
    path = f"/applications/x/{RFP_FILE}"
    script = iter(
        [
            AIMessage(
                "",
                tool_calls=[
                    {"name": "read_file", "args": {"file_path": path}, "id": "r"}
                ],
            ),
            AIMessage("done"),
        ]
    )

    class _Scripted(GenericFakeChatModel):
        def bind_tools(self, tools, **_kwargs):
            del tools
            return self

    monkeypatch.setattr(
        agent_module, "build_model", lambda *_a, **_k: _Scripted(messages=script)
    )
    root = tmp_path if profile == "local" else PROJECT_ROOT
    settings = Settings(root=root, backend_profile=profile, enable_search=False)
    extra = preload_solicitation(settings, "x", "THE SOLICITATION", user_id=None)
    graph = build_agent(settings)
    out = graph.invoke(
        {"messages": [{"role": "user", "content": "go"}], **extra},
        config={"configurable": {"thread_id": "t"}},
    )

    (read,) = [
        m.content
        for m in out["messages"]
        if isinstance(m, ToolMessage) and m.name == "read_file"
    ]
    assert "THE SOLICITATION" in read


def test_no_graph_carries_a_tool_that_opens_a_pdf():
    """Pins invariant 24.

    The agent never names a disk path. A PDF tool here would bring back the
    read-any-path hole between users that preloading closed.
    """
    for graph in (
        build_agent(Settings()),
        agent_module.build_discovery_agent(Settings()),
    ):
        names = graph.nodes["tools"].bound.tools_by_name
        assert not [name for name in names if "pdf" in name.lower()]
