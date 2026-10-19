# Plan: upgrade all dependencies to latest

Drafted 2026-10-19 against `main` @ `eb33a4c` (v0.2.1). Re-run the survey commands in step 0
before starting, because the versions below go stale.

## What the survey found

- **None of `pyproject.toml`'s upper bounds blocks anything.** Every direct runtime dependency's
  latest release on PyPI already fits inside its existing range (`deepagents<0.8`,
  `langchain*<2.0`, `langsmith<1.0`). A single `uv lock --upgrade` reaches the latest version of
  everything, with no change to `pyproject.toml`. That is the same shape as the last upgrade
  (`02b1f96`).
- **The only caps that do bind are outside `[project]`.** They are tracks B and C below.

**Decided order (2026-10-19):** Track A first, as its own commit. Then B and C, each as its own
separate commit afterwards, in that order. Do not fold either into A's commit.

| Package | Locked | Latest | Notes |
|---|---|---|---|
| deepagents | 0.7.21 | 0.7.28 | Patch releases have broken things before; audit required |
| langchain | 1.4.3 | 1.4.6 | |
| langchain-core | 1.6.6 | 1.6.12 | |
| langchain-anthropic | 1.7.5 | 1.7.6 | Affects invariant 9 |
| anthropic (transitive) | 1.10.0 | 1.15.0 | No offline test exercises it |
| langgraph / -checkpoint | 1.2.12 / 4.2.0 | 1.2.16 / 4.3.0 | Checkpoint metadata, pinned by §⑫ |
| langgraph-checkpoint-sqlite | 3.1.1 | 3.1.2 | |
| langsmith | 0.14.2 | 0.14.11 | Affects invariant 19 and `create_feedback` |
| pypdf | 6.19.0 | 6.21.1 | |
| python-dotenv | 1.2.3 | 1.2.4 | |
| streamlit (dev) | 1.64.0 | 1.66.0 | Affects `AppTest` and the theme keys |
| multidict, pyarrow (transitive) | 6.9.1, 25.0.1 | 7.1.0, 26.0.0 | Major bumps, pulled in by other packages |
| `uv_build` (build-system) | `<0.13.0` | 0.13.1 | **Blocked by the cap**; track B |
| ruff / ty (CI pins) | 0.16.4 / 0.0.74 | 0.17.1 / 0.0.89 | Not dependencies; track C |

The full transitive diff also adds `opentelemetry-api`, and removes `httptools` and `toml`.

---

## Track A: lock upgrade (the request itself)

### 0. Re-survey, then branch

```bash
git switch upgrade-deps   # already exists; it carries this plan
uv tree --outdated --depth 1
uv lock --upgrade --dry-run
```

If a direct dependency now shows a latest version *outside* its bound (for example
`deepagents 0.8.x`), stop. Crossing that bound is a separate decision. The `deepagents` pin
comment says why: 0.6 → 0.7 removed the todo middleware, added the `delete` tool, and dropped
about 5.6k characters of system prompt.

### 1. Upgrade and sync

```bash
uv lock --upgrade
uv sync
```

Use `uv` for this, never an edit: `uv.lock` is write-blocked for `Edit`/`Write`. These commands
do not name the file, so the Bash guard lets them through.

### 2. Run the local gates by hand

The hooks only fire on `Edit`/`Write`, so a Bash-only change has run nothing yet.

```bash
uv run pytest tests/ -q
uvx ruff@0.16.4 check src/ tests/ evals/ streamlit_app.py
uvx ruff@0.16.4 format --check src/ tests/ evals/ streamlit_app.py
uvx ty@0.0.74 check src/ tests/
```

Use the CI-pinned tool versions, not bare `uvx`, so that track A's result doesn't mix with track
C's. **Read the ty diagnostics; don't just count them.** There should be exactly the two upstream
causes `CLAUDE.md` names: `TodoListMiddleware`'s type-parameter arity, and
`BaseStore.put`'s `dict` vs `TypedDict`. If either was fixed upstream and a new one of ours took
its place, the count stays at 2 and hides it.

### 3. Audit each risky package, even if the tests pass

A green suite is necessary but not sufficient here: most invariants fail silently. Read each
package's release notes for the range and check them against these points.

- **deepagents 0.7.21 → 0.7.28**: the highest risk. Last time a patch release changed `_grade`'s
  signature and its satisfied-verdict semantics (`tests/test_wiring.py` grader stub).
  - Did the tool roster change? A new model-visible tool, and is it classed `write`?
    (Invariant 18: `delete` is governed only by inheritance.)
  - Did permission matching or the `exact`/`bulk` scopes change?
  - Did any shipped prompt text or `task` description change? (No test can see that; see the
    0.7 note under `prompts.py` in `CLAUDE.md`.)
  - Do the internals the tests reach into still exist (`_check_fs_permission`,
    `supports_execution`, `tools_by_name`)?
  - Does `execute` stay inert on `FilesystemBackend`?
- **langchain-anthropic 1.7.6 + anthropic 1.15** (invariant 9): `max_tokens` and `effort` must
  still reach the outgoing payload. The test asserts on the request itself, so green here means
  something. Check whether the profile registry changed what it resolves for the shipped ids,
  and update the version numbers quoted in invariant 9 if so.
- **langsmith 0.14.11** (invariant 19): `tracing_is_enabled`'s env-var precedence, and whether
  the names `test_review_fixes.py` §⑪ derives from `langsmith` changed. Also check that
  `Client.create_feedback` still takes `session_id` by name (the reason for the `>=0.6.7` floor).
- **langgraph-checkpoint 4.3.0**: does `get_checkpoint_metadata` still copy scalars from
  `metadata` and `configurable` the same way (§⑫, and the `config.trace_config` notes)?
- **streamlit 1.66** (`AppTest`, `test_theme_config.py`): check whether any theme key was added,
  renamed, or moved between top-level and per-mode sections. Check whether `AppTest`'s handling of
  forms, selectbox serialisation, or rerun queues changed; `CLAUDE.md` documents each of those.
- **multidict 7 / pyarrow 26**: pulled in by other packages and used by no code here. Just
  confirm `uv sync` resolved them and the app starts.

### 4. Live smoke test (costs money; ask before running)

Only a live run covers the prompts, the `anthropic` SDK bump, and deepagents prompt changes.

```bash
uv run python -m evals.run_scout
uv run grant-writer draft --app-id upgrade-smoke --rfp <a small RFP> --funder NSF
```

On the draft run, look for: the orchestrator delegating via `task` (instead of drafting inline),
`[NEEDS INPUT]` markers, and no output cut off mid-sentence.

### 5. Update docs where a fact changed

- Leave `pyproject.toml` floors alone unless a fix *requires* a newer version. The last upgrade
  left them unchanged on purpose. If one does need raising, give it a comment in the same style
  as the existing pins.
- Update any version-specific claim in `CLAUDE.md` that became false (invariant 9's 1.7.5 figures,
  the Streamlit notes). The hook runs the suite on `CLAUDE.md` edits, because
  `test_invariants.py` parses it.

### 6. Commit (and optionally release)

Model the message on `02b1f96`: say what moved, which bumps matter and why, what broke and how it
was fixed, and confirm the ty baseline was read rather than counted. A release is a *separate*
commit bumping `version` to `0.2.2`, because bumping the version is the release: CI tags and
publishes it on a green push to `main`.

---

## Track B (separate commit, after A): `uv_build` 0.12 → 0.13

`[build-system] requires = ["uv_build>=0.12.5,<0.13.0"]` blocks 0.13.1.

1. Read the uv 0.13 changelog for build-backend changes, especially module discovery, src-layout
   handling, and license-file handling.
2. Change the bound to `>=0.13.1,<0.14.0`.
3. Run `uv build`, then compare the wheel and sdist contents against a v0.2.1 build. `evals/` and
   `tests/` must stay out of the wheel, and `LICENSE` must stay in.
4. Note that the local `uv` is 0.12.22. With a mismatched backend, uv installs `uv_build` from
   PyPI instead of using its built-in fast path. That works, but it is slower, so consider
   `uv self update`.

Keep this as its own commit: it changes what gets published, not what runs.

## Track C (separate commit, after B): CI tool pins ruff 0.17.1 / ty 0.0.89

1. Run `uvx ruff@0.17.1 format --check` and `check`. A new minor version can reformat code or
   enable new rules within the selected families.
2. Run `uvx ty@0.0.89 check src/ tests/`, and read every diagnostic. If the upstream count
   changes, update **both** thresholds: the ty hook in `.claude/settings.json` and the type-check
   step in `.github/workflows/ci.yml`.
3. Bump the pins in `ci.yml`. The `CLAUDE.md` Commands section names `ruff@0.16.4` /
   `ty@0.0.74`, so update it in the same change.

Keep this as its own commit too. It mixes badly with track A, because a reformat would bury the
lock diff.
