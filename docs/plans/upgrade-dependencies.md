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
separate commit afterwards, in that order. Do not fold either into A's commit. Track D (model
configuration) comes last, because it changes what the agents do rather than what they are built
from, and its checks depend on A's langchain-anthropic version.

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

## Track D (after C): per-role effort, refusals, and model fit

Added 2026-10-19 after a model-fit review. The conclusion of that review was **keep the current
model split.** Opus 5.5 handles drafting and grading; Sonnet 5.5 handles research, compliance and
discovery. Model choice is not where the gains are. They are in effort settings that are tuned to
each role, and in a failure mode nothing handles yet.

| Model | Input / output, $ per 1M tokens | Default effort | Note |
|---|---|---|---|
| Fable 5.1 | $10 / $50 | n/a | Needs 30-day retention (no zero-retention option). Not recommended here |
| Opus 5.5 | $4 / $20 | `medium` | The project overrides this to `high` |
| Sonnet 5.5 | $2 / $10 | `high` | Effort levels were recalibrated; `medium` is suggested for agentic work |
| Haiku 5.5 | $0.10 / $0.50 | `medium` | No server-side refusal fallback |

### D1. Per-role effort in `build_model` (one commit)

At present `MODEL_EFFORT = "high"` is applied to every role (`config.py:74`). Make effort a
per-role setting while keeping invariant 9's rule: send it explicitly on every `anthropic:` spec,
and never inherit the provider's default.

The hooks run the whole suite after every edit, so sequence the work so each edit passes on its own:

1. Add a `DEFAULT_EFFORT` mapping keyed like `DEFAULT_MODELS`, with **every role set to `"high"`**,
   plus `GRANT_WRITER_<ROLE>_EFFORT` overrides that work like the model overrides. `build_model`
   takes the effort as a parameter. Callers in `agent.py`, `subagents.py` and `evals/run_scout.py`
   pass their role's value. Nothing changes on the wire, so
   `test_default_model_sends_an_explicit_effort` (`tests/test_wiring.py:673`) still passes.
2. Change that test to assert each role's *own* effort on the outgoing payload, rather than the
   single global value.
3. Only then change the values. The starting point is drafting and grader at `high`, and research
   and discovery at `medium`. Keep compliance at `high`: it judges an Opus drafter's work, so it
   should not be made cheaper. Validate the change in step D4 before release.
4. Update invariant 9 in `CLAUDE.md`. The rule ("set explicitly, never inherited") stays the same;
   what changes is where it is set.

### D2. Refusal handling (investigate first, then decide)

Opus 5.5 and Sonnet 5.5 run broader safety classifiers, including a `bio` category. A
biomedical or health grant can be **falsely declined**. When that happens the API still returns
HTTP 200, with `stop_reason: "refusal"` and empty or partial content, and raises nothing. The
word "refusal" appears nowhere in langchain-anthropic 1.7.5, so nothing in the stack surfaces it.
That makes it a new silent failure of exactly the kind `CLAUDE.md` lists.

1. Find where `stop_reason` and `stop_details` end up on the `AIMessage` (probably
   `response_metadata`). Write an offline test that feeds a canned refusal response through
   `ChatAnthropic` to confirm it.
2. Surface it in `activity.py` so both the CLI and the UI show "declined by safety classifier
   (<category>)" rather than an empty turn. `activity.py` is the shared stream parser, so one
   change reaches both frontends.
3. Decide separately whether to enable the server-side fallback (the
   `server-side-fallback-2026-07-01` beta with `fallbacks: "default"`). That changes what every
   request sends and how it is billed, so it is a product decision; do not slip it into this
   commit.
4. If D2.2 lands, add it as **invariant 20** with a test that claims it (`Pins invariant 20.`).

### D3. Forced `tool_choice`: verify it, then pin it with a test

Opus 5.5, Sonnet 5.5 and Fable 5.1 reject a forced `tool_choice` (`any` or `tool`) with a 400. This
was checked on 2026-10-19 and **is already handled upstream for the shipped ids**:

- langchain-anthropic 1.7.5's `_supports_forced_tool_choice` (`chat_models.py:1161`) excludes ids
  starting with `claude-opus-5-5`, `claude-sonnet-5-5` and `claude-fable-5-1`.
- `RubricMiddleware` chooses `ProviderStrategy` (native structured output) when the model profile
  declares `structured_output`, so it never needs a forced tool call.

The remaining risk is how this is decided. Both checks match on the model id, so a
`GRANT_WRITER_*_MODEL` override to a future id would fail on the first grader call. Add an
offline test that builds the grader with each shipped spec and asserts the outgoing payload has
no forced `tool_choice`. After Track A's upgrade, re-check that the line above still exists.

### D4. Validate with evals (live; costs money; ask first)

1. Run `uv run python -m evals.run_scout` at the D1 discovery effort and compare it against a run at
   `high`. The scout's citation-accuracy and invention scorers are the bar.
2. **Optional: try Haiku 5.5 as the scout.** Run
   `GRANT_WRITER_DISCOVERY_MODEL=anthropic:claude-haiku-5-5 uv run python -m evals.run_scout`.
   It is 20× cheaper than Sonnet. Switch `DEFAULT_MODELS["discovery"]` only if the citation and
   invention scores hold. The judge stays on `GRADER_MODEL` (Opus), so it still differs from the
   scout.
3. For drafting, run one real `draft` at the new settings and read the output. No offline test can
   judge prose quality.

Commit D1 and D2 separately. Each one changes what a live run does, so each should be revertible
on its own.
