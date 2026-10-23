"""Turn an uploaded solicitation PDF into text before a run starts.

Invariant 24. The agent never sees a PDF and never names a disk path: a
frontend extracts the text here, then `preload_solicitation` puts it where the
graph's own backend reads `/applications/<id>/rfp.md`. Two failures made that
the design rather than a tool the agent calls:

- **The `server` profile could not see the archive.** Its drafts live in
  `StateBackend`, which is thread state, while the old `extract_pdf_text` tool
  wrote `rfp.md` to real disk. The write succeeded, the receipt said so, and
  every `read_file` afterwards found nothing.
- **A path is not a boundary.** That tool opened any `pdf_path` the model
  named, so once several users share a server, one user's agent could read
  another's upload by guessing where it was saved. Text in the backend has no
  path to guess.

The PDF is untrusted input parsed on the server, so the parse is bounded three
ways: a size cap that matches `server.maxUploadSize` in
`.streamlit/config.toml`, a signature check on the bytes rather than the
client-sent name or MIME type, and a page cap plus wall-clock limit enforced by
running `pypdf` in a child process that is killed when time runs out. A parser
that loops forever on a crafted file would otherwise stall the process every
session shares.
"""

from __future__ import annotations

import multiprocessing
from pathlib import Path
from typing import TYPE_CHECKING, Any

from deepagents.backends.utils import create_file_data

from grant_writer.config import application_dir

if TYPE_CHECKING:
    from collections.abc import Callable
    from multiprocessing.connection import Connection

    from grant_writer.config import Settings

# One value, two enforcement points: Streamlit refuses a larger upload before
# the script sees it, and `extract_solicitation` refuses one from any other
# caller (the CLI has no upload limit of its own).
# `tests/test_solicitation.py` pins the two to agree.
MAX_UPLOAD_MB = 50
MAX_PAGES = 500
EXTRACT_TIMEOUT_S = 120.0
RFP_FILE = "rfp.md"
PDF_MAGIC = b"%PDF-"

# `spawn`, never `fork`: Streamlit runs each session on a thread, and forking a
# threaded process copies whatever locks those threads held. It is the macOS
# default already; naming it makes Linux behave the same.
_CONTEXT = multiprocessing.get_context("spawn")


class SolicitationError(ValueError):
    """The upload was refused or could not be read. The message is for a human."""


def extract_solicitation(
    data: bytes, name: str, *, timeout: float = EXTRACT_TIMEOUT_S
) -> str:
    """Extract `data`'s text, page-marked, or raise `SolicitationError`.

    The output keeps the shape the agent has always studied: a header line and
    a `--- page N ---` marker per page, so requirements can cite the page.

    A document over `MAX_PAGES` is refused rather than truncated. Silently
    dropping its tail is the failure the archive exists to prevent: every
    compliance check afterwards would run against a solicitation with pages
    missing.
    """
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        msg = f"{name} is larger than {MAX_UPLOAD_MB} MB."
        raise SolicitationError(msg)
    if not data.startswith(PDF_MAGIC):
        msg = f"{name} is not a PDF: its contents do not start with %PDF-."
        raise SolicitationError(msg)
    return run_isolated(_extract, (data, Path(name).name, MAX_PAGES), timeout=timeout)


def run_isolated(
    target: Callable[..., str], args: tuple[Any, ...], *, timeout: float
) -> str:
    """Call `target(*args)` in a child process, killed after `timeout` seconds.

    `target` must be importable at module level, since `spawn` pickles it by
    name. Its exceptions come back as `SolicitationError` carrying their text.
    """
    receiver, sender = _CONTEXT.Pipe(duplex=False)
    child = _CONTEXT.Process(target=_child, args=(target, args, sender), daemon=True)
    child.start()
    sender.close()
    try:
        # Poll before joining: a result larger than the pipe's buffer blocks
        # the child in `send` until someone reads, so joining first would wait
        # out the whole timeout on a document that had already finished.
        if not receiver.poll(timeout):
            msg = (
                f"reading the PDF took longer than {timeout:g} seconds, so it "
                "was stopped. Try a smaller file, or paste the text instead."
            )
            raise SolicitationError(msg)
        try:
            ok, value = receiver.recv()
        except EOFError:
            # The child died without answering: killed for memory, or a crash
            # in the parser's C dependencies.
            msg = "the PDF reader stopped unexpectedly on this file."
            raise SolicitationError(msg) from None
    finally:
        receiver.close()
        if child.is_alive():
            child.kill()
        child.join()
    if not ok:
        raise SolicitationError(value)
    return value


def _child(target: Callable[..., str], args: tuple[Any, ...], conn: Connection) -> None:
    try:
        conn.send((True, target(*args)))
    except Exception as exc:  # noqa: BLE001 - reported to the parent, not raised
        conn.send((False, str(exc) or type(exc).__name__))
    finally:
        conn.close()


def _extract(data: bytes, name: str, max_pages: int) -> str:
    """The parse itself. Runs in the child, so it may hang or blow up safely."""
    from io import BytesIO

    from pypdf import PdfReader

    try:
        reader = PdfReader(BytesIO(data))
        total = len(reader.pages)
    except Exception as exc:
        msg = f"could not read {name} as a PDF: {exc}"
        raise ValueError(msg) from exc
    if total > max_pages:
        msg = f"{name} has {total} pages; the limit is {max_pages}."
        raise ValueError(msg)

    parts = [f"[{name} - {total} page(s)]"]
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001 - one bad page is not the file
            text = f"<page {i + 1} could not be extracted: {exc}>"
        parts.append(f"\n--- page {i + 1} ---\n{text.strip()}")
    return "\n".join(parts)


def preload_solicitation(
    settings: Settings, app_id: str, text: str, *, user_id: str | None
) -> dict[str, Any]:
    """Put `text` at `/applications/<app_id>/rfp.md` as the graph will read it.

    Returns the keys to merge into the invocation payload, which is empty for
    `local`:

    - **`server`**: the `files` key. `StateBackend` reads thread state, so the
      archive travels with the turn into the checkpoint, the way deepagents
      documents pre-populating it. The thread id already carries the user
      (invariant 23), so the virtual path needs no user segment.
    - **`local`**: written to disk under `application_dir`, which is where the
      graph's `FilesystemBackend` reads it. Single-user only: that backend
      reads the shared tree, so a user's preload under `@<user>/` would be a
      file the agent never finds. Refused rather than written somewhere
      invisible -- signing in must mean `server` (E4).

    The id goes through `application_dir` either way, so an id the boundary
    refuses is refused here too, before anything is written.
    """
    app_dir = application_dir(settings, app_id, user_id=user_id)
    if settings.backend_profile == "server":
        return {"files": {f"/applications/{app_id}/{RFP_FILE}": create_file_data(text)}}
    if user_id is not None:
        msg = (
            "the local profile is single-user: its agent reads the shared "
            "applications/ tree, so a signed-in upload needs the server profile."
        )
        raise ValueError(msg)
    target = (app_dir / RFP_FILE).resolve()
    # Invariant 2's resolve-then-check: a symlink planted at `rfp.md` must not
    # carry the write out of the application directory.
    if not target.is_relative_to(app_dir):
        msg = f"refusing to write {RFP_FILE} outside {app_dir.name}/"
        raise ValueError(msg)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return {}
