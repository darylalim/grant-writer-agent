# Plan: upgrade dependencies, then deploy for a multi-user team

Drafted 2026-10-19 against `main` @ `eb33a4c` (v0.2.1). Re-run the survey commands in step 0
before starting, because the versions below go stale.

| Track | What | Ships as |
|---|---|---|
| A | Lock upgrade to latest, plus checkpoint safety | 1 commit (+ optional release) |
| B | `uv_build` 0.12 → 0.13 | 1 commit |
| C | CI tool pins: ruff / ty; C2: pin the hooks to match, widen their triggers, add `ask` rules; C3: `setup-uv` v10, pin uv, Dependabot for actions | 3 commits (C, C2, C3) |
| D | Per-role effort, refusal handling, model fit | 2+ commits (D1, D2 separate) |
| E | Multi-user deployment for one team: Postgres, per-user threads, sign-in | Several commits; the largest track |

Do the tracks in order. Each one leaves the suite green and can ship on its own. E depends on A's
upgraded langgraph libraries and on D2, because a hosted user has no terminal to notice a refusal
in.

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
git switch upgrade-and-deploy   # already exists; it carries this plan
uv tree --outdated --depth 1
uv lock --upgrade --dry-run
```

If a direct dependency now shows a latest version *outside* its bound (for example
`deepagents 0.8.x`), stop. Crossing that bound is a separate decision. The `deepagents` pin
comment says why: 0.6 → 0.7 removed the todo middleware, added the `delete` tool, and dropped
about 5.6k characters of system prompt.

### 0b. Back up the checkpoints, and have a parked thread to test

`.grant_writer/checkpoints.sqlite` is the only state this upgrade can damage. The workspace is
plain files, and the `server` Store is rebuilt at every start. The checkpoint file holds
in-progress conversations, including writes parked for human approval (invariant 11). It was
written by langgraph-checkpoint 4.2.0 / sqlite 3.1.1, and after the upgrade 4.3.0 / 3.1.2 will
read it.

Close every `grant-writer chat` and Streamlit process first. Then:

```bash
sqlite3 .grant_writer/checkpoints.sqlite ".backup .grant_writer/checkpoints.pre-upgrade.sqlite"
sqlite3 "file:$PWD/.grant_writer/checkpoints.sqlite?immutable=1" \
  "select thread_id, count(*) from checkpoints group by thread_id;
   select distinct thread_id from writes where channel='__interrupt__';"
```

Use `.backup`, not `cp`. `SqliteSaver` opens the database in WAL mode, so a plain copy of the
main file can miss committed rows still held in `-wal`. The `immutable=1` URI is how the database
is read without locking it. A plain `-readonly` open failed here with "unable to open database
file", because WAL mode needs the `-shm` side file.

As of 2026-10-19 there were four threads (`X`, `discover:smoke-2026q3`, `probe-eoc-2`, `smoke`)
and **no parked thread**. The parked case matters most, so create one *before* upgrading. Run
`grant-writer draft --app-id upgrade-parked --approve ...` until it stops at the approval prompt,
then answer `quit`. This is a live run that costs money, so ask first. The `__interrupt__` query is
a quick check; the authority is `activity.is_parked(agent, {"configurable": {"thread_id": ...}})`.

### 1. Upgrade and sync

```bash
uv lock --upgrade
uv sync
```

Use `uv` for this, never an edit: the `PreToolUse` hook blocks `Edit`/`Write` on `uv.lock`.
There is no Bash guard on `uv.lock` (the only Bash guard covers `.env`), so these commands run
unimpeded. For the same reason, they also run no `PostToolUse` hook, which is why step 2 exists.

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

### 2b. Check that existing checkpoints still resume

The suite uses `InMemorySaver` and fresh threads, so it never reads a checkpoint written by the
*previous* version. Do it by hand against the real file:

1. Run `uv run grant-writer chat --app-id smoke` (or any existing thread). It must continue the
   earlier conversation. If it answers as if starting fresh, the old rows were not read, even
   though nothing raised.
2. For the parked thread from 0b, `activity.is_parked` must return `True`, and
   `pending_action_requests` must list the same write as before the upgrade. Then **approve or
   reject it**: the resume must complete. That is invariant 11 checked across a version boundary.
   A parked thread that loads as unparked means the pending write has been discarded.
3. Optionally, open the parked thread in the Streamlit app after a reload. `parked_state` must
   put the approval panel back on screen.

If any of these fail, stop and restore the backup (see step 6). Do not continue with a checkpoint
format you can't read.

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
  `metadata` and `configurable` the same way (§⑫, and the `config.trace_config` notes)? Also
  check the release notes for a serialisation or schema change; step 2b checks the result by hand.
- **The `server` profile's Store**: no test builds it against a real `Store`. Check whether
  deepagents' `StoreBackend` or `CompositeBackend` prefix stripping changed (invariant 6:
  `/skills/` and `/memories/` must stay in separate namespaces). Then start the `server` profile
  once and run `ls /skills/` to confirm no memory files appear in it.
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

**Rolling back is two steps, not one.** `git revert` brings back the old lock, but the checkpoint
file may now contain rows written by the new version, and reading new rows with the old version
is the direction that tends to break. Restore the backup too:

```bash
mv .grant_writer/checkpoints.pre-upgrade.sqlite .grant_writer/checkpoints.sqlite
rm -f .grant_writer/checkpoints.sqlite-wal .grant_writer/checkpoints.sqlite-shm
```

Any conversation that happened after the upgrade is lost by doing this, so say so before doing
it. Keep the backup until the upgrade has been in use for a while.

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

### C2. Make the hooks match CI, and close their gaps (one commit, after C)

On 2026-10-19 the hooks in `.claude/settings.json` ran `uvx ruff` and `uvx ty` **without a
version**, so they resolved to the latest releases (ruff 0.17.1, ty 0.0.89) while CI pinned 0.16.4
and 0.0.74. Checked then: **no divergence yet.** 0.17.1 reported "27 files already formatted" and
"All checks passed", and both ty versions reported 2 diagnostics. That agreement was luck. The next
ruff release can reformat a file during Track D or E, and CI's `format --check` then rejects what
the hook wrote.

1. **Pin the hooks to CI's versions.** Change `uvx ruff` → `uvx ruff@<ver>` and `uvx ty` →
   `uvx ty@<ver>` in the format and ty hooks, using the same versions C just put in `ci.yml`. From
   now on, a version bump touches **three** places in one commit: `ci.yml`, `.claude/settings.json`,
   and `CLAUDE.md`'s Commands section. Rewrite the `CLAUDE.md` sentence that accepts the drift
   ("pins … where the hooks run `uvx` unpinned") to describe the pinned state.
2. **Widen the suite hook's trigger list.** It currently fires on `src/grant_writer/*.py`,
   `tests/*.py`, `evals/*.py`, `streamlit_app.py`, `CLAUDE.md` and `.streamlit/config.toml`. Add
   `pyproject.toml`, which Track B's `[build-system]` and Track E's dependencies edit. Consider
   `.claude/settings.json` as well, so a hook edit is checked by the suite it gates. The suite
   took 12.9s for 426 tests against the hook's 60s timeout. Track E's Postgres tests must be
   opt-in (E6), or they eat into that.
3. **Turn the plan's "ask first" into permission rules, not hooks.** Add `permissions.ask` entries
   so Claude Code asks for confirmation before running these:
   - live, billed runs: `uv run grant-writer draft …`, `uv run grant-writer discover …`,
     `uv run grant-writer chat …`, `uv run python -m evals.run_scout …`,
     `uv run python -m evals.evaluate_scout …`;
   - checkpoint destruction: any `rm` or `mv` whose target is `.grant_writer/checkpoints*`. Track
     A's rollback `mv` is exactly this.

   An `ask` rule fits better than a blocking hook here. These commands are sometimes right, and
   the plan wants a human to confirm them, not to forbid them. Write the rules with the
   `/update-config` skill and check the matcher syntax against the current settings schema rather
   than guessing it. A rule that silently never matches fails *open*.
4. Validate: make a trivial `.py` edit and confirm the pinned ruff and ty run (the status messages
   show it). Then try one guarded command and confirm the prompt appears.

The hooks gate every later edit, so C2 lands before Track D's first Python change.

### C3. GitHub Actions: bump, pin uv, keep current (one commit, after C2)

The "upgrade everything" survey above covered Python packages only, but the actions in
`ci.yml` are dependencies too. As of 2026-10-19 (both SHAs verified against their tags with
`gh api repos/<repo>/git/ref/tags/<tag>`):

| Action | Pinned | Latest | |
|---|---|---|---|
| `actions/checkout` | `3d3c42e…` = v7.0.1 | v7.0.1 | Current |
| `astral-sh/setup-uv` | `c771a70…` = v9.0.0 | **v10.3.0** | One major behind |

1. **Bump `setup-uv` to v10.** Read its v10.0.0 release notes first. A major version can change
   defaults that this workflow relies on: `enable-cache`, how `python-version` sets `UV_PYTHON`
   (the `test` job's matrix depends on that), and cache keys. Pin the new **SHA**, with the tag in a
   trailing comment, as the file already does. Resolve the SHA with `gh api`; don't copy it from a
   web page. Update all three `uses:` lines (`test`, `lint`, `release`).
2. **Pin CI's uv version.** None of the three jobs passes `version:` to `setup-uv`, and
   `pyproject.toml` has no `[tool.uv] required-version`, so CI runs whichever uv `setup-uv` picks,
   independent of the local 0.12.22. Prefer `required-version` in `pyproject.toml`, which pins it
   for local `uv` *and* CI from one place, over a `version:` input repeated three times. Check in
   the v10 notes that `setup-uv` honours it. This interacts with Track B: `uv_build`'s range and the
   uv that runs it should move together. If B's packaging check needs a known uv in CI, do this
   step before B.
3. **Keep actions current automatically (optional).** Add `.github/dependabot.yml` with **only**
   the `github-actions` ecosystem, on a weekly or monthly schedule. Dependabot updates SHA pins
   and their version comments. Leave Python dependencies out of it: `uv.lock` is upgraded
   deliberately, with the audit in Track A, and a bot PR per package would bypass that audit.
4. Validate: the PR's own CI run *is* the test. Both matrix legs, `lint`, and the `--help` smoke
   step must pass. `release` does not run on a PR. It is checked on the first push to `main` after
   merge, so read that run's "Decide whether to release" output.

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

## Track E (after D): multi-user deployment for one team

Decided 2026-10-19: **one organisation, a team of users.** Everyone shares one org profile
(`/memories/org/AGENTS.md`) and one set of `/skills/`, and each user's applications and threads
are kept separate. **The host and the sign-in provider are deliberately left open.** Write E so
it depends only on a `DATABASE_URL` and an authenticated user id. Choosing the host is E5's
decision, made after A–D ship.

Today the `server` profile is not deployable. `agent.py:71` and `:149` build an `InMemoryStore`,
so skills and memories are lost on every restart. Checkpoints still go to local SQLite, which
allows one writer at a time and lives on one machine. `StateBackend` keeps drafts *in graph
state*, so in that profile the checkpointer database holds the proposals themselves, not just
chat history.

### E1. Postgres for the checkpointer and the Store

- Add `langgraph-checkpoint-postgres` (3.2.0 on 2026-10-19; it brings `psycopg` 3) with
  `uv add`. Re-check its version against Track A's `langgraph-checkpoint`.
- In `config.py`, the only module that reads env vars, add an optional `DATABASE_URL` to
  `Settings`. It goes in `.env.example` too, never in the secrets file itself.
- In `_build_checkpointer` (`agent.py:39`), add a branch that returns a `PostgresSaver` when the
  URL is set; SQLite and in-memory stay as they are. The Store gets the same treatment:
  `PostgresStore` when the URL is set, `InMemoryStore` otherwise. Build both from one connection
  pool. They are built in two places (`build_agent` and `build_discovery_agent`), so factor out
  one `_build_store`. Two copies is the shape invariant 2's history warns against.
- Call `.setup()` for both once (it creates the tables). Do it from a deploy step or an explicit
  `grant-writer db-setup` subcommand, not on every request.
- `seed_store_from_disk` currently re-seeds an in-memory store on every start. Against Postgres it
  must be **idempotent** (upsert by key), or every restart duplicates or overwrites edited
  memories. Decide which wins on a restart: the files on disk or the edits in the Store.
- **Existing SQLite threads do not migrate.** I'm not aware of an official SQLite → Postgres
  checkpoint copier. Treat local and hosted history as separate, and say so in the README.

### E2. Per-user thread ids (new invariant)

Today `--app-id` *is* the `thread_id` (`cli.py:155`, `:189`; `streamlit_app.py:325`, `:1537`). With
many users, two people who name an application `nsf-2027` would resume **each other's**
conversation and drafts, and nothing would raise. That is invariant 12's collision, between users
instead of graphs, and it gets the same fix:

- Add a `config.user_thread_id(user_id, app_id)` that every caller uses, never a literal, exactly
  as `discovery_thread_id` is used today. It composes with the discovery prefix. Use a separator
  `_SAFE_ID` forbids (it already forbids `:`), so a crafted app id cannot impersonate another
  user's thread *by construction*.
- `_active_thread_id` (`streamlit_app.py:282`) is the UI's single owner of thread ids, so route
  it through the new function. The CLI stays single-user and keeps unprefixed ids, or uses a
  fixed local user. Pick one and pin it with a test.
- In the `server` profile, scope the Store namespaces for anything per-user. `/skills/` and
  `/memories/org/` stay shared, because there is one organisation. If per-user memories are ever
  added, they need their own namespace (invariant 6's reason, one level down).
- Add this as **invariant 21**, with a claiming test (`Pins invariant 21.`) that two users with the
  same app id get different thread ids and cannot read each other's checkpoints. If D2 did not
  produce invariant 20, renumber this one to 20.
- `parked_state` and `is_parked` read the checkpoint by thread id. Both must use the per-user id,
  or a user could be shown, or approve, a different user's pending write.

### E3. Uploads must not depend on one machine's disk

`streamlit_app.py:1300` writes the uploaded RFP to real disk under `application_dir`, and the agent
reads it with `extract_pdf_text`, which also reads real disk. That works with one server process
and breaks as soon as there are two replicas: the upload lands on one and the agent runs on the
other. Options:

- (a) **Extract the text at upload time** and put the text, not the file, into the backend or the
  opening message. This is simplest, and nothing is shared on disk.
- (b) Use shared object storage (for example S3 or GCS), read by a tool. This is more
  infrastructure, and that tool is a new real-disk-style writer, so it needs invariant 2's
  treatment.

Recommendation: (a). Either way, uploads must land under the per-user id from E2, never a bare app
id, and the size and type limits on uploads stay.

### E4. Sign-in and spending limits

- Sign-in: left open, as decided above. The requirement is one stable user id per request,
  available before any thread id is built. If Streamlit stays as the front end, its built-in OIDC
  sign-in (`st.login` / `st.user`) provides that. A gateway in front (for example an
  authenticating proxy) works too, provided the id it supplies cannot be spoofed by a client.
- **No sign-in, no deployment.** The app spends API credit on every turn. Without sign-in, anyone
  with the URL can spend it, and E2 has no user to separate by.
- Spending limits: a per-user cap on turns or tokens per day, checked before a turn starts, as the
  same kind of guard as the `parked_state` check. Record usage per user (the `metadata` that
  `config.trace_config` already sends to LangSmith can carry the user id, but see the
  `CLAUDE.md` rule: put nothing in `details` that the thread doesn't already hold).

### E5. Deployment (decide the host here)

- The host is open: a container platform with managed Postgres, or LangSmith Deployment, which
  provides Postgres, threads and auth hooks, with Streamlit becoming a client. Decide after A–D,
  using what E1–E4 turned out to need.
- `backend_profile` must be `server` in any web deployment. Today that is a sidebar choice
  (`streamlit_app.py:894`). For a deployment, force it from config and hide the choice, so that
  nobody can select `local` inside a web server (the `CLAUDE.md` rule, enforced rather than
  documented).
- Secrets (`ANTHROPIC_API_KEY`, `TAVILY_API_KEY`, `DATABASE_URL`, LangSmith) come from the host's
  secret store, never from a committed file. `config.py`'s `load_dotenv()` only skips names that
  are already set, so a host-set value wins.
- `get_agent` is `st.cache_resource`, shared across sessions (`streamlit_app.py:237`). That is
  safe only while the graph holds no per-user state: user identity must travel in the run config
  or thread id, never in a cached object. Pin that with a test.

### E6. Tests

- The suite stays offline by contract. Postgres tests are opt-in: mark them and skip unless a
  `TEST_DATABASE_URL` is set, in the same spirit as `pytest.importorskip("streamlit")`.
- **CI: add a separate `postgres` job** to `ci.yml`, rather than adding the database to `test`.
  Give it a `services: postgres:` container with a health check, set `TEST_DATABASE_URL` to that
  container, run `uv sync --locked`, then run only the Postgres-marked tests. Pin the image to a
  specific major version (and ideally a digest), on the same principle as the SHA-pinned actions.
  Keep the existing `test` job exactly as it is: it is the proof that the suite needs no database.
  Add `postgres` to `release`'s `needs:` only once it is stable, because a flaky service container
  there blocks every release.
- Offline cases to add: `_build_checkpointer` picks Postgres only when the URL is set; the
  `user_thread_id` collision and escape cases (parametrized like §⑨); the deployed UI cannot
  select `local`; `parked_state` uses the per-user id.
- Update `CLAUDE.md`'s "Backend profiles" section. "Swap for `PostgresStore` before deploying"
  becomes a description of what is there.

Commit E1 through E4 separately. E2 and E4 are what make hosting safe, so neither ships as part of
another commit.
