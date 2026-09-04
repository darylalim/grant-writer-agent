"""Push the four `ScoutCase` fixtures into a LangSmith dataset. One direction.

    uv run python -m evals.push_dataset                  # push, then verify
    uv run python -m evals.push_dataset --dry-run        # plan only, write nothing
    uv run python -m evals.push_dataset --out rows.json  # payloads only, no client
    uv run python -m evals.push_dataset --dataset scratch --create --no-prune

**This needs a live `LANGSMITH_API_KEY` and writes to a real workspace.** Like
everything else in `evals/`, it is not collected by `pytest tests/` -- but the
pure half of it is, in `tests/test_evals.py`, because a mirror that is quietly
wrong overwrites the fixture set with a lossy copy of itself and reports
success doing it.

`scout_cases.py` is the source of truth and this dataset is a push-only mirror
of it. A hand-edit in the LangSmith UI is drift to overwrite, not a second
opinion to merge -- two editable copies of a fixture set is the drift this repo
keeps writing tests against, and the point of a mirror is that only one end
holds a pen.

## Why the id is derived and not minted

`example_id(key) = uuid5(_NAMESPACE, key)`, so `genuine-fit` lands on the same
row today, next week, and from a colleague's laptop. Without that, "overwrite"
degrades into "append": every push mints a fresh uuid, and the only way to
avoid duplicates is to empty the dataset first -- which churns every id an
experiment referenced and leaves a window where a concurrent `evaluate` reports
on nothing.

The `key` in metadata is *not* a second identity scheme. Reconciliation never
reads it: a hand-edited `key` is drift like any other field and is overwritten.
It is there so a human reading the UI knows which fixture a row is, and so
`case_from_example` can rebuild a `ScoutCase`.

## Why the digest is recomputed rather than stored

The obvious design stamps a `source_sha` into metadata and compares it. That
design *preserves* the drift it exists to erase: a hand-edit changes
`outputs.expect_eligibility` and leaves `source_sha` untouched, the stored hash
agrees with itself, and the push skips the row. So `digest` is recomputed from
the fetched `inputs`/`outputs`/`metadata` every time and compared against the
digest of the locally derived row. Unequal means overwrite -- drift-safe and
idempotent from the same comparison, with no third copy of the fixture set kept
anywhere to go stale.

## Why the push re-reads what it wrote

`update_examples` is documented partial -- it overwrites the fields it is given
and leaves the rest -- so every write here supplies all three mirrored fields
rather than diffing and patching. What that documentation does not settle is
whether supplying `outputs` *replaces* that document or *merges* into it, and
nothing in the installed SDK decides it either. If it merges, a key a human
added inside `outputs` could never be cleared and the digest would never
converge.

Rather than assert which it is, `push` re-reads after writing and re-plans. A
second plan that is not empty means the push did not take: it is printed and
the process exits non-zero. The idempotency claim is therefore checked on every
run instead of being documented, which is the difference between a guarantee
and a convention. A push with nothing to do skips the second read, because the
first one already proved it -- two reads, zero writes, no new dataset version.

`--no-verify` exists because that check has no other way out. If LangSmith ever
maintains a second server-side example-metadata key, every row reads as drift
forever and the push exits 1 forever; the flag is how you get a write through
while `_SERVER_METADATA` is corrected.

## What this cannot repair

`langsmith` 0.11.1 ships no `update_dataset`: only `update_dataset_tag` and
`update_dataset_splits`. The dataset's own description is therefore write-once
at creation -- editing `NOTICE` here reaches every example's metadata and never
the blurb on a dataset that already exists, and the push reports success. Delete
the dataset and let the next push recreate it if that matters.

A rename is worse and is not detectable: the read raises not-found, `--create`
makes a fresh empty dataset under the old name, and the renamed one becomes a
stale fork that no longer receives pushes. The dataset URL is printed on every
run for exactly this reason -- a person who renamed it sees the wrong link.

Unlike `run_scout.main`, this returns non-zero on failure. That eval is a
measurement, and a non-zero exit there would invite blocking a pipeline on a
model's mood; a push is an operation on the world, and a failed one is a fact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from dataclasses import dataclass, fields, replace
from typing import TYPE_CHECKING, Any, TextIO

from langsmith.utils import LangSmithConflictError, LangSmithNotFoundError

from evals.scout_cases import CASES, ScoutCase

if TYPE_CHECKING:  # constructing one reads the environment; only build_client does
    from langsmith import Client
    from langsmith.schemas import Dataset

DATASET_NAME = "grant-writer-scout-cases"
MIRROR_SOURCE = "evals/scout_cases.py"
NOTICE = (
    "Push-only mirror of evals/scout_cases.py. Edits made here are overwritten "
    "by the next push, not merged."
)

#: Frozen forever. The id *is* the row, so changing this does not rewrite four
#: rows -- it orphans four and pushes four more, and the run reports success.
#: It is uuid5(NAMESPACE_URL, "<origin>/evals/scout_cases.py"), inlined so the
#: derivation cannot drift with whatever `git remote` happens to say.
_NAMESPACE = uuid.UUID("fabd0b39-bf93-57c2-874b-627d03a18a4a")

_INPUT_FIELDS = ("brief", "candidate", "profile")
_OUTPUT_FIELDS = (
    "expect_eligibility",
    "expect_disqualified",
    "expect_gap_markers",
    "forbidden",
)
_METADATA_FIELDS = ("key", "why")

#: Written by the server, never by us. `Example` carries no `split` field, so a
#: split comes back inside metadata -- and a key we never wrote would read as
#: drift on every push, rewriting all four rows forever. Exactly one entry, and
#: widening it blindly hides real drift: any *other* unexpected metadata key is
#: a hand-edit, which is the thing this module exists to overwrite.
_SERVER_METADATA = frozenset({"dataset_split"})

_ROW_KEYS = ("inputs", "outputs", "metadata")


class PushRefused(RuntimeError):
    """Raised before any write: a lossy row, or a dataset not ours to touch."""


def example_id(key: str) -> uuid.UUID:
    """The row a case owns, for the life of the key."""
    return uuid.uuid5(_NAMESPACE, key)


def _jsonable(value: Any) -> Any:
    """Through JSON, because JSON is what comes back.

    `forbidden` is a `tuple[str, ...]` here and a list there. Uncanonicalised,
    every row differs from itself, and the "idempotent" push rewrites all four
    on every run, forever, while reporting success.
    """
    return json.loads(json.dumps(value))


def mirror_row(case: ScoutCase) -> dict[str, Any]:
    """One case as the dataset should hold it. Pure: no clock, no fresh uuid.

    Raises `PushRefused` when `ScoutCase` has grown a field no group mirrors.
    A lossy copy ships a dataset that asserts less than the file does, and a
    dimension that is never checked scores exactly like one that passed.
    """
    declared = {f.name for f in fields(ScoutCase)}
    mirrored = {*_INPUT_FIELDS, *_OUTPUT_FIELDS, *_METADATA_FIELDS}
    if declared != mirrored:
        raise PushRefused(
            f"ScoutCase fields not mirrored: {sorted(declared ^ mirrored)}"
        )
    return {
        "id": str(example_id(case.key)),
        "inputs": {name: getattr(case, name) for name in _INPUT_FIELDS},
        "outputs": _jsonable({n: getattr(case, n) for n in _OUTPUT_FIELDS}),
        "metadata": {"mirror_source": MIRROR_SOURCE}
        | {name: getattr(case, name) for name in _METADATA_FIELDS},
    }


def case_from_example(
    inputs: dict[str, Any], outputs: dict[str, Any], metadata: dict[str, Any]
) -> ScoutCase:
    """The inverse, so an `evaluate` target reuses `score_programmatically`.

    A second scoring path is a second thing to keep right, and the scorers in
    `evals/scorers.py` are the ones `tests/test_evals.py` already covers. This
    is how a dataset-driven run stays drivable by them rather than growing its
    own.
    """
    merged: dict[str, Any] = {
        **{name: inputs[name] for name in _INPUT_FIELDS},
        **{name: outputs[name] for name in _OUTPUT_FIELDS},
        **{name: metadata[name] for name in _METADATA_FIELDS},
    }
    merged["forbidden"] = tuple(merged["forbidden"])
    return ScoutCase(**merged)


def digest(row: dict[str, Any]) -> str:
    """A content fingerprint, recomputed from the row every time.

    Never read back from a stored hash: a hand-edit leaves that agreeing with
    itself. `id` is excluded -- identity is the key, not part of the content.
    """
    body = {
        "inputs": _jsonable(row.get("inputs") or {}),
        "outputs": _jsonable(row.get("outputs") or {}),
        "metadata": {
            k: v
            for k, v in _jsonable(row.get("metadata") or {}).items()
            if k not in _SERVER_METADATA
        },
    }
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Plan:
    """What one push would do. Derived from a fetch, never stored.

    `create`, `update` and `unchanged` all carry whole rows rather than ids so
    that printing a plan needs no second lookup -- in particular no read of a
    remote `metadata["key"]`, which reconciliation deliberately does not trust.
    `prune` is ids because there is no row of ours behind them.
    """

    create: tuple[dict[str, Any], ...]
    update: tuple[dict[str, Any], ...]
    unchanged: tuple[dict[str, Any], ...]
    prune: tuple[str, ...]

    def settled(self, *, prune: bool) -> bool:
        """Nothing left to do. With pruning off, a stray row is not unfinished."""
        return not self.create and not self.update and (not prune or not self.prune)

    def counts(self) -> tuple[int, int, int, int]:
        """create, update, unchanged, prune."""
        return (
            len(self.create),
            len(self.update),
            len(self.unchanged),
            len(self.prune),
        )


def plan(cases: tuple[ScoutCase, ...], remote: dict[str, dict[str, Any]]) -> Plan:
    """Pure. `remote` maps example id -> the row as fetched.

    The whole reconciliation, and the only place a decision is made: everything
    the network does is either the reads that produce `remote` or the writes
    that carry this out. There is deliberately no state file -- a local record
    of what was pushed last time is a third copy of the fixture set, which is
    the drift the README objects to.
    """
    create: list[dict[str, Any]] = []
    update: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    rows = [mirror_row(case) for case in cases]
    for row in rows:
        current = remote.get(row["id"])
        if current is None:
            create.append(row)
        elif digest(current) == digest(row):
            unchanged.append(row)
        else:
            update.append(row)
    claimed = {row["id"] for row in rows}
    return Plan(
        create=tuple(create),
        update=tuple(update),
        unchanged=tuple(unchanged),
        prune=tuple(sorted(set(remote) - claimed)),
    )


def fetch(client: Client, dataset_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    """Every example, keyed by id, normalised to the mirror's own shape.

    Typed `uuid.UUID` rather than the SDK's permissive id union on purpose:
    `list_examples` with neither a dataset id nor a name enumerates every
    example in the tenant, which would hand `assert_ours` a marked row from
    some *other* dataset and let a prune through.

    No `limit=`: it is both the page size and the total in this SDK, so a limit
    on a grown fixture set would hand `plan` a truncated remote view -- which
    plans `create` for rows that already exist.
    """
    return {
        str(example.id): {
            "inputs": dict(example.inputs or {}),
            "outputs": dict(example.outputs or {}),
            "metadata": dict(example.metadata or {}),
        }
        for example in client.list_examples(dataset_id=dataset_id)
    }


def assert_ours(name: str, remote: dict[str, dict[str, Any]]) -> None:
    """Refuse a dataset this mirror never wrote, before anything is written.

    Keyed on the examples rather than on the dataset, because there is no
    `update_dataset` in this SDK to repair a dataset whose own metadata marker
    did not stick. This catches the wrong-name accident -- pointing at
    somebody's real dataset and pruning it -- and not a determined editor, who
    can copy a marked row's metadata.
    """
    if remote and not any(
        (row.get("metadata") or {}).get("mirror_source") == MIRROR_SOURCE
        for row in remote.values()
    ):
        raise PushRefused(
            f"{name!r} holds {len(remote)} examples and none carries "
            f"metadata.mirror_source == {MIRROR_SOURCE!r}. Pushing would "
            f"overwrite rows in it and delete the rest. Name the mirror with "
            f"--dataset."
        )


def ensure_dataset(client: Client, name: str) -> Dataset:
    """Read it, or create it. A racing push is not an error.

    `create_dataset` 409s on a duplicate name -- names are unique per workspace
    -- which arrives as `LangSmithConflictError`. Read-create-read is the same
    shape `langsmith.testing` uses upstream for the same reason.
    """
    try:
        return client.read_dataset(dataset_name=name)
    except LangSmithNotFoundError:
        try:
            return client.create_dataset(name, description=NOTICE)
        except LangSmithConflictError:
            return client.read_dataset(dataset_name=name)


def apply(client: Client, dataset_id: uuid.UUID, todo: Plan, *, prune: bool) -> None:
    """Carry out a plan. Create, then update, then prune.

    Every write supplies all three mirrored fields. `update_examples` is
    partial -- an omitted field is left exactly as a human edited it -- so
    supplying everything is what turns a partial API into a full overwrite of
    what the mirror owns.

    Deletion is soft (`hard_delete=False`, the default), which is what makes
    pruning defensible: a hand-added row stays recoverable through dataset
    versioning, and its id was printed before this ran.
    """
    if todo.create:
        client.create_examples(dataset_id=dataset_id, examples=list(todo.create))
    if todo.update:
        client.update_examples(dataset_id=dataset_id, updates=list(todo.update))
    if prune and todo.prune:
        client.delete_examples(list(todo.prune))


@dataclass(frozen=True)
class PushResult:
    """What one run did, with nothing printed yet."""

    dataset_id: str | None
    #: `Dataset.url` is `Optional[str]`; the id is the fallback.
    url: str | None
    planned: Plan
    #: The plan a re-read produced, or `None` when no re-read happened -- a dry
    #: run, or `--no-verify`. `None` is "not checked", never "checked and fine".
    residual: Plan | None
    #: The pre-write rows, so a prune can be named before it is carried out.
    snapshot: dict[str, dict[str, Any]]
    #: True when this was a real run rather than a dry one. It does *not* mean
    #: bytes went out: a settled plan is live and writes nothing.
    live: bool
    pruning: bool

    @property
    def converged(self) -> bool:
        """No further writes are planned -- or none were checked for."""
        return self.residual is None or self.residual.settled(prune=self.pruning)


def push(
    client: Client,
    *,
    name: str = DATASET_NAME,
    prune: bool = True,
    dry_run: bool = False,
    create: bool = True,
    verify: bool = True,
) -> PushResult:
    """Read, plan, write, read back, plan again. Prints nothing.

    `create` is checked before the dataset is made rather than after, so a
    mistyped `--dataset` is refused instead of quietly becoming a fresh empty
    dataset that four rows are then pushed into successfully.
    """
    exists = client.has_dataset(dataset_name=name)
    if not exists and not create:
        raise PushRefused(
            f"No dataset named {name!r}. Pass --create to make it, or check "
            f"the name -- a typo here creates a second mirror rather than "
            f"updating the one you meant."
        )

    dataset: Dataset | None = None
    snapshot: dict[str, dict[str, Any]] = {}
    if exists or not dry_run:
        dataset = ensure_dataset(client, name)
        snapshot = fetch(client, dataset.id)

    assert_ours(name, snapshot)
    planned = plan(CASES, snapshot)

    result = PushResult(
        dataset_id=str(dataset.id) if dataset else None,
        url=dataset.url if dataset else None,
        planned=planned,
        residual=None,
        snapshot=snapshot,
        live=not dry_run,
        pruning=prune,
    )
    if dry_run:
        return result
    if planned.settled(prune=prune):
        # The first read already proved it. A second would cost a round trip
        # to learn the same answer, and no write means no new dataset version.
        return replace(result, residual=planned)

    assert dataset is not None  # not dry_run, so ensure_dataset ran
    apply(client, dataset.id, planned, prune=prune)
    if not verify:
        return result
    return replace(result, residual=plan(CASES, fetch(client, dataset.id)))


def build_client() -> Client:
    """The one place a client is constructed, so a test can stand in for it.

    A local import: `Client()` resolves endpoint, key and workspace from the
    environment at construction, and nothing about importing this module should
    depend on any of them being set.
    """
    from langsmith import Client

    return Client()


def _excerpt(row: dict[str, Any], width: int = 40) -> str:
    """A pruned row named by something a human recognises, on one line."""
    inputs = row.get("inputs") or {}
    text = str(inputs.get("brief") or inputs)
    flat = " ".join(text.split())
    return flat if len(flat) <= width else f"{flat[: width - 1]}…"


def render(result: PushResult, *, out: TextIO | None = None) -> None:
    """The whole report. Separate from `main` so a test can read it.

    `out=None` rather than `out=sys.stdout`: a default argument is evaluated
    once, at import, so the parameter would hold whatever `sys.stdout` was then
    and ignore every later redirect -- including the one `capsys` installs, so
    the test asserting a prune is named would read an empty string while the
    text went to the terminal.
    """
    out = out if out is not None else sys.stdout
    where = result.url or result.dataset_id or "(not created)"
    print(f"\n{DATASET_NAME}  {where}\n", file=out)

    for verb, rows in (
        ("create", result.planned.create),
        ("update", result.planned.update),
        ("unchanged", result.planned.unchanged),
    ):
        for row in rows:
            print(f"  {verb:<9}  {row['metadata']['key']}", file=out)
    stale_verb = "prune" if result.pruning else "stray"
    for stale in result.planned.prune:
        excerpt = _excerpt(result.snapshot.get(stale, {}))
        print(f"  {stale_verb:<9}  {stale}  (no case; {excerpt!r})", file=out)

    created, updated, unchanged, pruned = result.planned.counts()
    if not result.pruning:
        pruned = 0
    if result.live:
        tail = "" if created or updated or pruned else " Nothing was written."
        print(
            f"\n{created} created, {updated} updated, {unchanged} unchanged, "
            f"{pruned} pruned.{tail}",
            file=out,
        )
    else:
        print(
            f"\n{created} to create, {updated} to update, {unchanged} unchanged, "
            f"{pruned} to prune. Nothing was written.",
            file=out,
        )

    if not result.pruning and result.planned.prune:
        print(
            f"{len(result.planned.prune)} example(s) no case claims were left "
            f"in place by --no-prune.",
            file=out,
        )

    if result.residual is None:
        if result.live:
            print("Not verified: --no-verify skipped the read-back.", file=out)
        return
    if result.converged:
        print("Re-read: no further writes planned.", file=out)
        return
    left = len(result.residual.create) + len(result.residual.update)
    print(
        f"Re-read: {left} example(s) still differ from {MIRROR_SOURCE} after "
        f"the write.",
        file=out,
    )
    for row in (*result.residual.create, *result.residual.update):
        print(f"  {'update':<9}  {row['metadata']['key']}", file=out)
    print(
        "The push did not take. See what `update_examples` does to a field it "
        "was not given, in this module's docstring.",
        file=out,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="evals.push_dataset",
        description="Mirror evals/scout_cases.py into a LangSmith dataset.",
    )
    parser.add_argument(
        "--dataset", default=DATASET_NAME, help="dataset name to mirror into"
    )
    parser.add_argument(
        "--create",
        action="store_true",
        help="make the dataset if it does not exist (implied for the default name)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="read the dataset and print the plan; write nothing",
    )
    parser.add_argument(
        "--no-prune",
        action="store_true",
        help="leave examples no case claims (they are deleted by default)",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="skip the read-back that proves the write took",
    )
    parser.add_argument(
        "--out",
        help="write the example payloads as JSON and exit; makes no network "
        "call and needs no credential",
    )
    args = parser.parse_args()

    try:
        if args.out:
            rows = [mirror_row(case) for case in CASES]
            with open(args.out, "w", encoding="utf-8") as handle:
                json.dump(rows, handle, indent=2)
            print(f"Wrote {len(rows)} payloads to {args.out}. Nothing was pushed.")
            return 0

        # Both spellings, for the reason invariant 19 spells out: the value is
        # read across two namespaces and checking only one reads as absent.
        if not (os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY")):
            print(
                "LANGSMITH_API_KEY is not set; this writes to a real workspace.",
                file=sys.stderr,
            )
            return 1

        result = push(
            build_client(),
            name=args.dataset,
            prune=not args.no_prune,
            dry_run=args.dry_run,
            create=args.create or args.dataset == DATASET_NAME,
            verify=not args.no_verify,
        )
    except PushRefused as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1

    render(result)
    return 0 if (not result.live or result.converged) else 1


if __name__ == "__main__":
    raise SystemExit(main())
