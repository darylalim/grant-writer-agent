# Plan: upgrade dependencies, then deploy for a multi-user team

Drafted 2026-10-19 against `main` @ `eb33a4c` (v0.2.1). Re-run the survey commands in step 0
before starting, because the versions below go stale.

| Track | What | Ships as |
|---|---|---|
| A | Lock upgrade to latest, plus checkpoint safety; A2: `.gitignore` additions | 2 commits (+ optional release) |
| B | `uv_build` 0.12 → 0.13 | 1 commit |
| C | CI tool pins: ruff / ty; C2: pin the hooks to match, widen their triggers, add `ask` rules; C3: `setup-uv` v10, pin uv, Dependabot for actions | 3 commits (C, C2, C3) |
| H | Evaluation: pinned judge and repeats, a delegation check on the full graph, a drafting eval, a false-refusal case | 4 steps (H1–H4); runs between C and D |
| D | Per-role effort, refusal handling, model fit | 2+ commits (D1, D2 separate) |
| E | Multi-user deployment for one team: E0 org-profile template split, then Postgres, per-user threads, sign-in, E7 operations (logging, run table, audit, alerts) | Several commits; the largest track |
| F | Streamlit theme designed for the app: warm paper neutrals, one indigo accent, serif headings; modes mirrored | 2–3 commits (test, config, re-pin) |
| G | Adaptive layout: approval panel first, one tab per workflow, a pane-height control, readable prose width | 4 commits (G1–G4) |

Do the tracks in order (A, B, C, **H**, D, E, with F and G placed as their sections say; H runs between C and D so D can be measured). Each one leaves the suite green and can ship on its own. E depends on A's
upgraded langgraph libraries and on D2, because a hosted user has no terminal to notice a refusal
in.

**Security gate before every merge.** Run `/security-review` on the track's diff (it is built into
Claude Code), on top of the codebase-specific checks each track lists. Also run `uv audit` after
any change to the lock. Both are cheap, and neither replaces the other: the review reads *our*
code, and the audit checks *dependencies* for published advisories.

**Accessibility gate before merging any track that changes the UI (D2, F, G, E4).** Run an
automated scan (inject axe-core into the running app through the Chrome tools' JavaScript
runner, or run Lighthouse's accessibility audit) in **both theme modes**, at 390px and 1366px
wide. Then do a **VoiceOver** pass (built into macOS) over the approval flow, end to end. Record
the results in the PR. Treat a new axe "serious" or "critical" finding as blocking, and fix or
document the existing ones.

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

**Security (part of this step):**

- `uv audit` must report zero advisories, or list each accepted one, with the reason, in the
  commit message. On 2026-10-19 the *current* lock had **3 advisories, all in aiohttp 3.14.3**
  (GHSA-494j-54xm-rwp3, GHSA-5wr8-gcqf-gqf3, GHSA-2g87-pp6c-29x5), pulled in via
  `langchain-tavily`. All three are fixed in 3.14.4, and the dry run resolves 3.14.5. Two are in
  aiohttp's *server* code, which this project doesn't run, so the real exposure was low. Still,
  nothing would have reported them. This upgrade should clear all three; confirm it.
- Read the lock diff for **added and removed packages**, not just version bumps. This upgrade adds
  `opentelemetry-api` and removes `httptools` and `toml`. For each added package, check what
  requires it (`uv tree --invert --package <name>`) and that it is the well-known project of that
  name.

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

### 4b. Performance baseline, before and after (live; ask first; reuse step 4's runs)

Nothing else in this plan would notice a cost or speed regression from the upgrade, which changes
prompts (deepagents), request shapes (langchain-anthropic) and the SDK (anthropic 1.10 → 1.15).

1. **Before `uv lock --upgrade`**, on the old lock, run one `discover` and one `draft` on a
   **fixed, small** RFP and focus, and keep the inputs in `evals/out/perf/`. Record, per run:
   input tokens, output tokens, **cache-read and cache-creation input tokens**, number of model
   calls, wall-clock time, and cost at the current prices (Opus 5.5 $4/$20, Sonnet 5.5 $2/$10
   per M tokens). Take the figures from each response's usage metadata (or from LangSmith, if
   tracing is on).
2. **After the upgrade**, run the same two with the same inputs. Flag anything over about +15% on
   cost or time, and explain it before merging. Model output varies from run to run, so treat
   one run as a smoke signal, not a benchmark. Repeat a run before concluding anything.
3. **Prompt caching must work.** deepagents installs Anthropic's caching middleware
   unconditionally, and `MemoryMiddleware` marks the org profile for caching. Across one run's
   turns, `cache_read_input_tokens` must be above zero, and most of the stable prefix (system
   prompt, tool list, memory) should be read from cache after the first call. If it is zero,
   something in the prefix changes between requests (a timestamp, an unstable tool order, memory
   rewritten each turn). Find and fix it before Track D, which assumes caching works.

Baseline already measured offline on 2026-10-19 (no API calls): `grant-writer --help` 0.86s warm
(1.25s cold); `import grant_writer.agent` 0.74s, almost all of it `deepagents.graph`; the suite
12.9s for 426 tests. Re-measure after the upgrade, and explain any jump of more than about 25%.

### 4c. Tracing still works (live; one of step 4's runs, with tracing on)

`test_frontends.py` §⑥ checks the trace labels offline against `CONFIG_KEYS` and a real
`ensure_config` round-trip, but only a live run shows that langsmith 0.14.11 *accepts* them.
With tracing on for one `draft`, check that:

- the trace appears, with `run_name`, the tags, and the metadata (`frontend`, `command`, `app_id`,
  `funder`, `rubric`);
- it can be **found by filtering**: `langsmith trace list --tags …` returns it;
- the upgrade's new `opentelemetry-api` dependency doesn't change where traces go. Read
  langsmith's notes for any OpenTelemetry export that turns on by default.

Turn tracing back off afterwards if it isn't normally on. Invariant 19's kill-switch only governs
tests and CI.

### 5. Update docs where a fact changed

- Leave `pyproject.toml` floors alone unless a fix *requires* a newer version. The last upgrade
  left them unchanged on purpose. If one does need raising, give it a comment in the same style
  as the existing pins.
- Update any version-specific claim in `CLAUDE.md` that became false (invariant 9's 1.7.5 figures,
  the Streamlit notes). The hook runs the suite on `CLAUDE.md` edits, because
  `test_invariants.py` parses it.

**README (same commit).** In "Where state lives", add the missing row for the checkpoints, which
is the state this track has to protect: `.grant_writer/checkpoints.sqlite` | Until deleted |
Conversation state, todos, and writes awaiting approval. Back it up before upgrading
dependencies. Point the row at step 0b's backup command.

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

### A2. `.gitignore` additions (own small commit, after A)

The current `.gitignore` is hand-written and commented, and it beats GitHub's Python template for
this repo, so keep it and extend it. Match the existing style: each entry gets a comment saying
*why*.

- `.env.*` followed by `!.env.example`. Only the exact name `.env` is ignored today, and Track E's
  deployment work tends to create `.env.production` / `.env.local`. The `PreToolUse` hooks already
  treat `.env.*` as secret, so the two should agree. Make this edit with `Edit`, not a shell
  command. The `.env` Bash guard matches the name anywhere in a command, including inside a
  heredoc of prose; it blocked a plan edit on 2026-10-19 for exactly that reason.
- `evals/out/`, as the conventional `--out` target for `run_scout` and `push_dataset`. Their
  records carry raw model output. Mention the convention in `evals/README.md`.
- Optional: drop `.mypy_cache/`, since the project uses ty, not mypy. It is harmless either way.

Check: `git status --short --ignored` before and after. No tracked file may become ignored, and
`.env.example` must still show as tracked.

**Same commit: the project description.** Decided 2026-10-19. One text is used in both places,
so the wheel's metadata and the GitHub "About" box agree:

> AI grant-writing agent on LangChain Deep Agents: finds funding opportunities, fit-scores them
> with quoted evidence, and drafts proposals with human approval of final files. Marks unknowns
> for input instead of inventing data.

(One line in the actual field; it is wrapped here for width. 218 characters, under GitHub's 350.)

1. `pyproject.toml` → `[project] description = "..."`. The `PostToolUse` suite hook doesn't watch
   `pyproject.toml` until C2, so run `uv run pytest tests/ -q` and `uv run grant-writer --help` by
   hand. The `--help` text may come from elsewhere, so check whether it should change too. This
   edit does **not** release anything: only a `version` change does.
2. GitHub repo settings. These are outward-facing, so the user confirms before they run:
   ```bash
   gh repo edit --description "<the text above>" \
     --add-topic grant-writing,grants,grant-proposals,funding,nonprofit,research-funding,ai-agents,multi-agent,llm,human-in-the-loop,deep-agents,langchain,langgraph,claude,anthropic,streamlit,python
   ```
   That's 17 topics. Hold back `postgresql` until Track E ships, and `grants-gov` unless that tool
   is meant to be a headline feature.
3. **README:** change the intro line ("A grant writing agent built on Deep Agents") so it agrees
   with the description, keeping the Deep Agents link. The two bullets under it (`discover`,
   `draft`) can stay. Mention the `evals/out/` convention in `evals/README.md` (see above).

---

## Track B (separate commit, after A): `uv_build` 0.12 → 0.13

`[build-system] requires = ["uv_build>=0.12.5,<0.13.0"]` blocks 0.13.1.

1. Read the uv 0.13 changelog for build-backend changes, especially module discovery, src-layout
   handling, and license-file handling.
2. Change the bound to `>=0.13.1,<0.14.0`.
3. Run `uv build`, then compare the wheel and sdist contents against a v0.2.1 build. `evals/` and
   `tests/` must stay out of the wheel, and `LICENSE` must stay in. **This is a security check:
   CI attaches both files to a public GitHub release.** Baseline, built 2026-10-19 with
   `uv build -o <scratch dir>` (don't build into the repo's `dist/`):
   - sdist: `PKG-INFO`, `pyproject.toml`, `pyproject.toml.orig`, `LICENSE`, `README.md`, and
     `src/grant_writer/*.py` (11 modules);
   - wheel: the same 11 modules, plus `dist-info` (`LICENSE`, `WHEEL`, `entry_points.txt`,
     `METADATA`, `RECORD`).

   **Fail on anything else.** In particular: no secrets files, nothing from `applications/`,
   `opportunities/`, `.grant_writer/` or `memories/`, and no `.claude/`.
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
5. **README:** in "Development", pin the `uvx ruff …` commands to the same version (`uvx ruff@<ver>`),
   so the README, the hooks, CI and `CLAUDE.md` show one version. Add one line saying that billed
   commands ask for confirmation in Claude Code.

The hooks gate every later edit, so C2 lands before Track D's first Python change.

**Suite-time budget.** The suite hook has a 60s timeout, and the suite took 12.9s (426 tests) on
2026-10-19. Keep it **under about 30s**, half the timeout, and put the suite time in each
track's PR. If a track pushes it past that, mark slow cases opt-in (the Postgres and load tests
already are) instead of raising the hook timeout. A slow hook adds a wait to every Python edit.

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
2b. **Add `uv audit` to CI**, as a step in the `lint` job after `uv sync --locked`. It should fail
   on any advisory that isn't listed as accepted in the workflow, with a reason. Through the
   setup-uv v10 bump, keep the workflow's least-privilege permissions exactly as they are: the
   default `contents: read`, and `contents: write` on `release` only. A new major version must not
   need broader permissions; if its notes say it does, stop and decide.
3. **Keep actions current automatically (optional).** Add `.github/dependabot.yml` with **only**
   the `github-actions` ecosystem, on a weekly or monthly schedule. Dependabot updates SHA pins
   and their version comments. Leave Python dependencies out of it: `uv.lock` is upgraded
   deliberately, with the audit in Track A, and a bot PR per package would bypass that audit.
4. Validate: the PR's own CI run *is* the test. Both matrix legs, `lint`, and the `--help` smoke
   step must pass. `release` does not run on a PR. It is checked on the first push to `main` after
   merge, so read that run's "Decide whether to release" output.
5. **README:** if step 2 adds `required-version`, state the required uv version in Quick start
   (where `uv sync` is the first command) and in "Development". A user with an older uv then
   learns why `uv sync` refuses to run.

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
5. **README:** in "Configuration", add a `GRANT_WRITER_*_EFFORT` row next to the model row, and show
   each role's default model and effort (a small table: role → model → effort). The README
   currently names the override variables but not the defaults, so a reader can't tell what they
   are overriding.
6. **Observability:** check in one trace that each model call shows its model and the *effort
   actually sent*. When D4's cost comparison looks odd, the trace is where to check which effort
   ran. If langchain-anthropic doesn't surface effort in the run's metadata, add it to the per-call
   labels.

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
   change reaches both frontends. **Security:** `stop_details.explanation` and the category are
   model/API output, so render them as plain text, never with `unsafe_allow_html` or as
   interpreted markdown.
3. Decide separately whether to enable the server-side fallback (the
   `server-side-fallback-2026-07-01` beta with `fallbacks: "default"`). That changes what every
   request sends and how it is billed, so it is a product decision; do not slip it into this
   commit.
4. If D2.2 lands, add it as **invariant 20** with a test that claims it (`Pins invariant 20.`).
5. **README:** add a "Guardrails" bullet saying that a turn declined by a safety classifier is
   shown as declined, with its category, and not as an empty reply. Say that health and biomedical
   topics can trigger false refusals, and what to do about one (rephrase, or retry later). If the
   fallback was enabled, say which model it falls back to.
6. **Accessibility:** the declined message states the outcome and category **in text**, not
   only through colour or an icon. Use `st.warning`/`st.error` with a sentence, so it is announced
   the same as other alerts.
7. **Observability:** put `stop_reason` and the refusal category into the turn's trace metadata,
   so refused runs can be filtered and counted in LangSmith. That's allowed under `CLAUDE.md`'s
   rule for `metadata` (nothing that isn't already in the thread), because both come from the
   turn's own response. Without it, the real-use refusal rate H4 estimates is never observed.

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

Use Track H's pinned judge and `--repeat 3` for every comparison below, plus H3's drafting eval
and H4's refusal rate, not single runs.

1. Run `uv run python -m evals.run_scout` at the D1 discovery effort and compare it against a run at
   `high`. The scout's citation-accuracy and invention scorers are the bar.
2. **Optional: try Haiku 5.5 as the scout.** Run
   `GRANT_WRITER_DISCOVERY_MODEL=anthropic:claude-haiku-5-5 uv run python -m evals.run_scout`.
   It is 20× cheaper than Sonnet. Switch `DEFAULT_MODELS["discovery"]` only if the citation and
   invention scores hold. The judge stays on `GRADER_MODEL` (Opus), so it still differs from the
   scout.
3. For drafting, run one real `draft` at the new settings and read the output. No offline test can
   judge prose quality.

4. **Cost per completed run, not per request.** For the D1 settings against the old all-`high`
   settings, on the same inputs: total tokens, cache-read tokens, model calls, rubric rounds
   (`max_rubric_iterations` is 3), wall-clock time and cost, **for one finished run**, next to
   the eval scores. A lower effort that needs more turns or more rubric rounds is not a saving.
   Also confirm cache reads stay above zero. Each role's effort is fixed, which shouldn't affect
   caching, but changing effort partway through a conversation would throw the cache away.

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

### E0. Separate the org-profile template from the real profile (before E1)

`memories/org/AGENTS.md` is **tracked** because it ships as a template, and its own header
invites the agent to append durable organisation facts to it. The drafting graph may write
`/memories/**`, the discovery rules allow it too (`backends.py:194`), and so do the real-disk
tools (`tools._DRAFTING_WRITE_DIRS`). So the first real run writes organisation and possibly
personnel data into a tracked file, and `git commit -a` publishes it. That is the data
`.gitignore` keeps out of `applications/`, arriving by another path. As of 2026-10-19 the file was
unmodified (clean `git status`). Adding it to `.gitignore` alone does nothing, because git keeps
tracking files it already tracks.

1. `git mv memories/org/AGENTS.md memories/org/AGENTS.example.md`.
2. In `Settings.ensure_dirs()` (`config.py:182`), copy the template to `memory_path` **only if
   `memory_path` does not exist**. Never overwrite: that file is the organisation's accumulated
   profile. Add a `memory_template_path` property beside `memory_path`.
3. Add `memories/org/AGENTS.md` to `.gitignore`, with a comment in the existing style.
4. `seed_store_from_disk` (the `server` profile, and E1's Postgres seeding) must read the real
   file if it exists and fall back to the template, and must never seed the template *over* an
   edited Store entry. That is E1's idempotency rule, settled here first.
5. `ensure_dirs` only runs in the `local` profile's `build_backend`. Check that a fresh checkout
   in the `server` profile still gets a profile to read, and that `discover` doesn't score against
   a missing file. The scout's first instruction is to read it.
6. Tests (offline): the copy happens when the file is missing; it never overwrites an existing
   file; the template is what ships (it exists and is tracked). Consider a `Pins invariant N.`
   test that `memories/org/AGENTS.md` is *not* tracked. This is the kind of silent leak the
   invariant list exists for.
7. **Warn everyone else before this lands.** When a clone pulls a commit that deletes a tracked
   file, git deletes its copy too. If their copy holds real data (modified), the pull refuses
   instead, and the obvious fix, `git stash` or `git checkout --`, can throw that data away.
   Write it in the commit message and the README: *back up `memories/org/AGENTS.md` before
   pulling this commit.* For this repo alone, the `git mv` in step 1 leaves the real file to be
   recreated from the template; anyone with real data in it must copy it aside first.
8. **README:**
   - Quick start: `$EDITOR memories/org/AGENTS.md` still works, because the first run creates the
     file. To edit before any run, `cp memories/org/AGENTS.example.md memories/org/AGENTS.md`
     first, the same pattern as the `.env.example` line just above it.
   - "Where state lives": the `AGENTS.md` row becomes *not tracked; created from
     `AGENTS.example.md` on first run; holds your organisation's data*. Add a row for the
     template.
   - The pull warning from step 7, as a short note under Quick start, since that's what an existing
     user rereads.

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
- **README:** add a `DATABASE_URL` row to "Configuration" (required only for a deployment;
  unset means SQLite). The rest belongs in the new Deployment section (E5).
- **Performance: checkpoint growth.** In the `server` profile, drafts live *in graph state*, so
  every checkpoint saves them again. Today's threads are tiny smoke tests (at most 7KB per
  checkpoint, 82KB per thread, measured 2026-10-19) and show nothing. Run one real `draft` in the
  `server` profile, then measure rows, bytes per checkpoint and total bytes per thread, in SQLite
  first and Postgres after E1. Size E5's storage, backups and retention from that figure, not
  from a guess.
- **Performance: connection pool.** Streamlit runs one script thread per session, and an agent
  run holds it, and its checkpoint connection, for minutes. Size the `psycopg` pool for the
  number of simultaneous runs E4 allows, plus headroom for the UI's checkpoint reads
  (`parked_state`, the approval panel), and set a pool timeout that shows a clear message
  instead of hanging.

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
- **Security test (essential):** user B, supplying user A's app id, can't read A's checkpoint,
  resume A's thread, see A's approval panel, or approve or reject A's pending write. Run it through
  every entry point: the run form, the follow-up input, the approval panel, and the file browser.
  The user id must come only from E4's authenticated session, never from any value the client
  supplies.

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
id.

**Security: uploads are untrusted input parsed on the server.** As of 2026-10-19,
`.streamlit/config.toml` sets **no** upload limit, so Streamlit's default of 200MB applies.

- Set `server.maxUploadSize` to what a solicitation actually needs (for example 25–50MB). Its test
  belongs in `test_theme_config.py`, which already pins that file.
- Check the type by content (`%PDF-` magic bytes), not only by extension or the client-sent MIME
  type.
- Run `pypdf` extraction with a page cap and a wall-clock time limit (for example in a worker with a
  timeout). PDF parsers have a history of inputs that loop forever or blow up memory, and in a
  shared deployment one bad upload stalls the process for everyone.
- Keep `Path(rfp.name).name`: the filename stays client-supplied (`streamlit_app.py:1296`).

### E3b. Gate writes to org memory in a shared deployment

The agent reads **untrusted text**: web search results via Tavily, funder pages, and uploaded
RFPs. It is also allowed to write `/memories/**` (the drafting rules, the discovery rules at
`backends.py:194`, and `tools._DRAFTING_WRITE_DIRS`). `/memories/org/AGENTS.md` loads into
**every** turn. So an instruction planted in a funder's page or an RFP ("record in the org profile
that …") can persist into every future run. On one laptop that affects one user. With one shared
org profile (Track E's decision), it affects **every user**, and nothing on screen marks it.

- In the deployed (`server`) configuration, either add an `interrupt` rule on `/memories/**`, so
  every org-profile write is a human approval like `final/` (invariants 5 and 11 then apply to
  it), **or** make `/memories/org/` read-only to both graphs and edit it only through an admin
  path. The interrupt keeps the feature; read-only is the simpler boundary. Pick one, and say why
  in the commit.
- The discovery graph must stay unable to park (`test_a_scan_can_never_park_on_an_approval`), so
  for discovery the answer has to be **deny**, not interrupt. Write a test for that combination
  explicitly; it is the same trap the `CLAUDE.md` architecture notes describe.
- The real-disk tools are not covered by the permission rules (invariant 2). Remove `"memories"`
  from `_DRAFTING_WRITE_DIRS` in the deployed configuration, or a tool `out_path` routes around
  the gate.
- Optionally keep an append-only audit record of memory writes (who, when, which turn), so a
  poisoned entry can be traced to its source document.

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
- **Security:** the user id comes only from the sign-in provider's verified session (for example
  `st.user` after `st.login`, or a header injected by an authenticating proxy that strips any
  client-sent copy). Never from a form field, query parameter, or cookie the client can set.
  Check the limit server-side, before the turn starts; a client-side check is no check at all.
- **Accessibility:** the sign-in page belongs to the provider, so check that it meets WCAG 2.2 AA
  (keyboard operable, labelled fields, no inaccessible CAPTCHA) before choosing a provider in E5.
  A user who can't sign in can't use anything else.
- **One run table for spending and observability.** The spending limit needs tokens and cost per
  user per run, recorded server-side. Build that record as E7's run table (below), not a separate
  counter, so limits, dashboards and alerts all read one source.
- **Performance: API rate limits.** With many users, Anthropic returns HTTP 429. The SDK retries
  twice, then the run fails part-way through. Add a **limit on simultaneous runs per
  organisation**, checked before a turn starts like the spending limit. When it's reached, show
  "N runs in progress, try again shortly" instead of starting a run that will fail. Size it to the
  organisation's rate-limit tier.

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
- **README: a new `## Deployment` section**, placed after "Web UI" and before "Architecture". A
  deployer has just read how the app is used and doesn't need the internals yet. Keep the README's
  existing structure everywhere else. It should cover:
  - who it's for (one team sharing one organisation profile) and what it needs: Postgres, a
    sign-in provider, and the host's secret store;
  - the `server` profile is forced in a deployment; never `local` inside a web server;
  - one-time setup (`.setup()` / the `db-setup` command from E1), and the variables needed;
  - **SQLite history does not migrate** to Postgres;
  - the per-user spending limit and how to change it;
  - a pointer to E0's note (back up `AGENTS.md` before pulling).

  Then delete the last paragraph of "Backends" ("replace it with `PostgresStore` before
  deploying"), which this section supersedes, and change the `server` row's Store column to say
  Postgres.
- **Security checklist for the deployment:**
  - **Database:** a dedicated least-privilege role (it owns the checkpoint and store tables, and
    is not a superuser); TLS on the connection (`sslmode=require` or stricter); encryption at
    rest; backups, with a restore that has actually been tested. That database now holds every
    user's proposals.
  - **Transport and app:** HTTPS only, in front of Streamlit. Leave Streamlit's XSRF protection on,
    and don't set `server.enableXsrfProtection = false` or open CORS to make a proxy work.
  - **Secrets:** from the host's secret store, scoped to the deployment, and rotatable. The API
    keys are spent on behalf of every user.
  - **Tracing is a data-sharing decision.** LangSmith tracing is automatic for this app, so with
    many users every draft, including organisation and personnel data, goes to a third party.
    Decide it explicitly: off, on with a documented retention period, or on for a
    staging deployment only. Record the decision in the README's Deployment section.
  - **Data lifecycle:** decide retention and per-user deletion for threads and drafts. Postgres
    keeps them forever by default.
  - **The `execute` tool in the server profile.** `test_execute_tool_is_not_a_permission_bypass`
    only builds the `local` profile (`test_wiring.py:503`). Extend it to `server`
    (`CompositeBackend` over `StateBackend`), so the deployed configuration is pinned too.

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
- **Load test with a fake model (offline, costs nothing).** Build the agents with a stub chat
  model (the suite already fakes models), then drive N simultaneous sessions (for example 5, 10
  and 25) against Postgres. Measure checkpoint write latency, pool waits, memory per session,
  and errors. This tests the deployment's capacity with no model cost. Keep it out of the default
  suite (opt-in, like the Postgres tests), and run it before E5's go-live.

### E7. Operations: logging, run records, audit, health checks and alerts

Today, observability is LangSmith tracing alone, with careful labels (`config.trace_config`;
§⑥, §⑫). There is **no server-side logging** in `src/` or `streamlit_app.py` (checked
2026-10-19). The three broad handlers (`streamlit_app.py:326`, `:1546`, `:1607`) show errors to
the *user*, and nothing else records them. On one laptop the user is the operator. In a team
deployment they aren't, and E5 may turn tracing off for privacy, which would leave nothing.

1. **Server-side logging.** Standard `logging`, configured once at app start, to the host's log
   collector. Each broad `except` logs the exception, user id, thread id, phase and frontend,
   **never proposal content**: no messages, no file text, no profile text. Logs then stay out of
   E5's tracing/privacy decision and can stay on whatever it decides. Add an offline test that the
   handlers log, and that a log record carries no message content (feed a run with a sentinel
   string and assert it's absent).
2. **A content-free run table** in Postgres, shared with E4's spending limit: user, thread id, kind
   (draft or discover), start and end times, outcome (`completed` / `parked` / `failed` /
   `refused` / `stopped`), model calls, input, output and cache-read tokens, and cost. Written
   from usage metadata at the end of each turn. It works with tracing off. This answers "how much,
   how often, how well" without any content.
3. **Tracing, three options rather than two.** For E5's decision: off; on with full content; or
   **on with inputs and outputs hidden**. The last keeps timing, token counts, errors and the call
   tree without sending proposal text. The LangSmith SDK has settings for hiding inputs and
   outputs; confirm their exact names in the installed version before relying on them, and test
   that the setting reaches the client.
4. **Audit records:** who approved or rejected each pending `final/` write, and when (user, thread,
   file path, decision). Also E3b's memory-write record. Append-only, separate from the run
   table. A write bound for submission to a funder deserves a record of who approved it.
5. **Health checks:** a liveness probe on Streamlit's built-in health endpoint, plus a readiness
   check that Postgres answers. Configure both on the host in E5.
6. **Alerts:** failed-run rate, refusal rate (D2), rate-limit (429) errors, connection-pool
   timeouts (E1), spending-limit hits, and **threads parked on an approval for more than N days**,
   found by querying the checkpoint writes for interrupts. The approval design keeps a paused
   write safe indefinitely (invariant 11); in a team, the same property lets an unanswered approval
   sit unnoticed.
7. **One correlation id:** the per-user `thread_id` (E2) plus the LangSmith run id, present in log
   lines, the run table and the audit record, so a user's report leads straight to the trace and
   the logs.

**README:** document what is collected, where it is stored, and how long it is kept, in the
Deployment section. Users of a shared deployment should know what's recorded about their runs.

Commit E0 through E4 separately, including E3b, then E7. E2, E3b and E4 are what make hosting safe, so none
of them ships as part of another commit. Run `/security-review` on each one before it merges.

## Track F (independent; any time after C2): a theme designed for this app

Designed 2026-10-19. The current theme is shadcn's zinc template, copied unchanged and well
engineered (tested contrast, a validated chart ramp, both modes). But it is generic, and entirely
monochrome: **nothing on the page is coloured to mark an action**. In a tool whose most important
moments are *Draft proposal* (a billed run) and *Approve* (a write bound for submission), the primary
buttons look like every other dark-grey control. Track F keeps everything the current file got
right and changes three things: **a warm "paper" neutral, one ink-indigo accent, and serif
headings.**

### Direction

| | Choice | Why |
|---|---|---|
| Feel | A calm document workspace: paper and ink, not a dashboard | Users read long drafts and judge them; the page should feel like a proposal, not a console |
| Neutrals | Tailwind **stone** (warm grey) in place of zinc | Warm paper is easier on the eyes over long reading, and it still reads as neutral |
| Accent | One **indigo**, used for the primary action, links and selected controls | Says "act here", and stays clear of the semantic red, amber, green and blue used by status colours |
| Headings | **Source Serif 4** (a serif), with Inter kept for body text and JetBrains Mono for code | Headings look like a document's; body text stays a UI font for legibility at 14px |
| Geometry | Unchanged: 6px radius, widget borders on, 14px base | The layout is built on `st.container(border=True)`; nothing there needs to change |

### How the two modes complement each other

Each mode is the other **mirrored step for step on the same two scales**. Surfaces that are
stone-50/100/200 in light become stone-900/800/700 in dark, and the accent moves to a lighter indigo
in dark so it stays vivid on a dark page. The *roles* are identical in both: the same token always
does the same job, only the luminance flips. That is what "complementary" means here, and it is
testable: `test_dark_mirrors_the_light_half_s_surface_boundaries` already checks it.

### Measured (2026-10-19, with `test_theme_config.py`'s own formulas)

| Check | Light | Dark | Floor |
|---|---|---|---|
| Body text on page | stone-950 on white **19.76** | stone-50 on stone-950 **18.92** | 4.5 |
| Link on page | indigo-700 **7.90** | indigo-300 **9.91** | 4.5 |
| Text on sidebar | **18.92** | **16.74** | 4.5 |
| Primary button: white label on fill | indigo-700 **7.90** | `#605EEF` **4.83** | 4.5 |
| Lit segment on its 10% wash | **6.71** | **3.82** | 3.0 |
| Code text on code background | stone-800 on stone-100 **13.90** | stone-200 on stone-800 **12.08** | 4.5 |
| `grayColor` stone-500 on white | **4.80** | (rendered via the derived `grayTextColor`; re-measure) | n/a |
| Chart ramp, worst entry on the page | **3.19** | **3.05** | 3.0 |

The dark primary is the one hard value. Streamlit uses `primaryColor` for two jobs that pull
opposite ways: a button fill under a hard-coded **white** label (wants dark) and the foreground of
a lit segmented control (wants light). The two curves cross near 4.4:1 on any near-black page, *so
no hue escapes it*. Contrast is luminance, and choosing a hue doesn't change luminance. No Tailwind
indigo step clears both: indigo-500 gives 4.47 / 4.10 (the label fails AA), indigo-600 gives
6.29 / 2.98 (the segment fails 3:1). `#605EEF`, searched between them, lands at **4.83 / 3.82**,
*the same numbers the current zinc-500 achieves (4.83 / 3.80)*. The accessibility budget is
unchanged, and the control gains a hue as a second cue, alongside its wash and border.
`#6365F1` hits exactly 4.50; don't use it, because it has no margin.

Two caveats to carry into the file's comments:

- The chart ramp is unchanged, and it now clears the new dark page by only 0.05 (3.05). On the dark
  **sidebar** (stone-900), `#A21CAF` is 2.77 and `#B91C1C` 2.70, the same caveat the file already
  documents for zinc-900. The app draws no charts yet.
- On the dark sidebar, the lit segment drops to 3.30 (still above 3.0).

### Proposed `.streamlit/config.toml` values

Keep the file's existing structure and its long comments. Rewrite the provenance note (no longer
"shadcn.toml unchanged"), the primary-colour note (new hue, same trade-off) and the
derived-grey note (re-measure with the new `grayColor`).

```toml
[theme]
base = "light"
baseFontSize = 14
baseFontWeight = 400
showSidebarBorder = true
showWidgetBorder = true
baseRadius = "6px"
buttonRadius = "6px"
font = "'Inter':https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap"
headingFont = "'Source Serif 4':https://fonts.googleapis.com/css2?family=Source+Serif+4:wght@600;700&display=swap"
codeFont = "'JetBrains Mono':https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500&display=swap"
codeFontSize = "0.875rem"
headingFontSizes = ["36px", "30px", "24px", "20px", "16px", "14px"]
headingFontWeights = [600, 600, 600, 600, 600, 600]
linkUnderline = true

grayColor = "#78716C"     # stone-500 (was zinc-500), so muted text matches the warm neutrals
blueColor = "#3B82F6"
greenColor = "#22C55E"
redColor = "#EF4444"
yellowColor = "#EAB308"
orangeColor = "#F97316"
violetColor = "#8B5CF6"
chartCategoricalColors = [ ...unchanged, validated ramp... ]

[theme.light]
primaryColor = "#4338CA"              # indigo-700
backgroundColor = "#FFFFFF"           # paper
secondaryBackgroundColor = "#FAFAF9"  # stone-50
codeBackgroundColor = "#F5F5F4"       # stone-100
textColor = "#0C0A09"                 # stone-950, ink
linkColor = "#4338CA"                 # indigo-700, with linkUnderline as the second cue
borderColor = "#E7E5E4"               # stone-200
codeTextColor = "#292524"             # stone-800
dataframeBorderColor = "#E7E5E4"
dataframeHeaderBackgroundColor = "#FAFAF9"

[theme.light.sidebar]
backgroundColor = "#FAFAF9"           # stone-50
secondaryBackgroundColor = "#F5F5F4"  # stone-100
codeBackgroundColor = "#E7E5E4"       # stone-200
textColor = "#0C0A09"
borderColor = "#D6D3D1"               # stone-300; NOT stone-200, which would collide with code bg
primaryColor = "#4338CA"
dataframeHeaderBackgroundColor = "#F5F5F4"
dataframeBorderColor = "#E7E5E4"

[theme.dark]
primaryColor = "#605EEF"              # searched indigo: 4.83 label / 3.82 segment (see above)
backgroundColor = "#0C0A09"           # stone-950
secondaryBackgroundColor = "#1C1917"  # stone-900
codeBackgroundColor = "#292524"       # stone-800
textColor = "#FAFAF9"                 # stone-50
linkColor = "#A5B4FC"                 # indigo-300: the light link's hue, lifted for a dark page
borderColor = "#44403C"               # stone-700
codeTextColor = "#E7E5E4"             # stone-200
dataframeBorderColor = "#44403C"
dataframeHeaderBackgroundColor = "#1C1917"

[theme.dark.sidebar]
backgroundColor = "#1C1917"           # stone-900
secondaryBackgroundColor = "#292524"  # stone-800
codeBackgroundColor = "#44403C"       # stone-700
textColor = "#FAFAF9"
borderColor = "#57534E"               # stone-600, mirroring light's distinct sidebar border
primaryColor = "#605EEF"
dataframeHeaderBackgroundColor = "#292524"
dataframeBorderColor = "#44403C"
```

The light sidebar's border/code collision that the template inherited (both zinc-200) is
deliberately *fixed* here, with stone-300 for the border. The mirror test only demands that dark
not *introduce* a collision, so this is an improvement it allows.

### Order of edits (the suite hook gates `.streamlit/config.toml` and `tests/*.py`)

Each edit must pass the suite on its own, and today's tests pin the old values, so:

1. **Test first, loosened without losing anything.** In `tests/test_theme_config.py`:
   - Retire `test_light_half_still_matches_the_vendored_template` and `DOCUMENTED_DEVIATIONS`. The
     light half stops being a template copy *by design*. Say so in the commit.
   - Make `LIGHT_PAGE` / `DARK_PAGE` come from the config's `backgroundColor` values, not literals,
     so the contrast tests measure whatever the page is.

   The suite stays green against the *old* config.
2. **Config.** Apply the values above and rewrite the comments. Every remaining test recomputes
   contrast and must pass, which is the evidence the design holds.
3. **Re-pin the design's own decisions** as new tests, in the file's existing style:
   - the light and dark primaries share a hue, so the modes are complementary rather than two
     unrelated palettes;
   - the dark primary keeps the 4.5 label / 3.0 segment split (the existing test already checks
     this);
   - `headingFont` is set and stays in a valid section.

### Visual check (the tests can't judge looks)

Run the app (the `run` skill), switch between Light and Dark in the settings menu, and screenshot
both. Check specifically:

- the *Draft proposal* and *Approve* buttons read as the primary action;
- the Source/Rendered segmented control in `render_file` and `approval_panel` clearly shows which
  side is lit;
- serif headings next to Inter body text look deliberate, not mismatched;
- bordered containers are distinguishable from the page in both modes.

If the serif reads as fussy at 14px, drop `headingFont` and keep the rest. The palette stands
without it.

### Accessibility (part of Track F)

**Input borders fail WCAG 1.4.11 today, and Track F's values keep the failure.** Text inputs,
selects and text areas are identified by their outline, which needs 3:1 against the page.
Measured 2026-10-19:

| `borderColor` | Light (on white) | Dark (on page) |
|---|---|---|
| Current zinc-200 / zinc-700 | 1.27 | 1.91 |
| Track F stone-200 / stone-700 | 1.26 | 1.92 |
| stone-400 / stone-600 | 2.52 | 2.59 |
| stone-500 / stone-500 | **4.80** | **4.12** |

Streamlit uses this one token for **both** input outlines and decorative
`st.container(border=True)` borders, and the app is built from bordered containers, so raising
it to pass makes every box visibly heavier. There's no theme-only way to style inputs separately,
and this project rules out CSS. **Decide one way, and test it:**

- (a) **Pass:** search the stone scale between 400 and 500 for the lightest value at ≥3:1 on both
  pages (and on the sidebar surfaces), and pin it in `test_theme_config.py` like the text
  contrast. Then re-check the "mirrored boundaries" test and the screenshots, because heavier
  container borders change the look.
- (b) **Documented exception:** keep the light borders, and write the decision and the measured
  ratios into the config comments and the README. Add a test that pins the *current* ratios, so
  any change to them is deliberate.

Recommendation: (a). The inputs are where users type facts the agent must not invent, so finding
the field matters more than lighter boxes.

**Focus ring.** Assumed to follow `primaryColor`. Confirm that by tabbing through the running
app. Measured: light 7.90, dark 4.09, dark sidebar 3.62, all ≥3:1. Add these as a test.

## Track G (after F, before E5): a layout that adapts to the display

Designed 2026-10-19 from a read of `streamlit_app.py` against the bundled Streamlit layout
reference (Streamlit 1.64). Constraint, as for Track F: **native layout primitives only, no CSS
injection**. Streamlit has no media queries and no viewport-width API. The adaptive behaviour
available is: columns stack at ≤640px, horizontal containers wrap, `width`/`height="stretch"`,
and user-chosen sizing. The design works with those, not around them.

### Today's layout, and where it breaks

`layout="wide"` (`:68`), then one long column: title → *Find opportunities* form → *Draft a
proposal* form → *Activity* feed (`height=440`, `:1370`) → follow-up `chat_input` → status slot,
where the **approval panel** renders (`:1610`) → shortlist → *Application files*: a metrics row,
then picker and content in `st.columns([1, 2])`, each `height=480` (`:865`–`:870`), with PDFs at
`height=430` (`:779`). Run settings sit in the sidebar.

| Display | What goes wrong |
|---|---|
| **Phone** (≤640px) | Columns stack, so the file picker becomes a **480px box holding a short radio list**, followed by a 480px content box: about 1,000px of fixed boxes. The approval panel, the one thing a human must act on, sits **below both forms and the 440px feed**. |
| **Tablet / small laptop** (~768–1366px) | No stacking, so the `[1, 2]` columns get cramped. 440 + 480 fixed boxes overrun a 768px-tall viewport, so the feed and the files can never be seen together. |
| **Desktop / ultrawide** (≥1920px) | `wide` lets rendered drafts run edge to edge. Prose lines become far too long to read comfortably, in an app whose main job is reading drafts. Meanwhile the fixed heights waste a tall monitor. |
| **All** | Both workflows' forms are always on screen, one above the other, so the page is long before any output appears. |

### The design

**1. Approval first, on every display.** Reserve a slot directly under the title (beside the
existing slot reservations at `:1143`) and render the approval panel there whenever the phase
is AWAITING. Every other control is already disabled then (`turn_locked`), and the panel is the
**sole exit** (invariant 11), so on a phone it must be the first thing on screen, not the last.
Keep it as an inline panel, **not** `@st.dialog`: a dialog can be dismissed, and dismissing it
would leave a page whose every control is disabled. That is the invariant 11 dead end in a new
form.

**2. One workflow at a time: `st.tabs(["Find opportunities", "Draft a proposal"], key=...)`.**
Each tab holds its form and that workflow's results (the shortlist, or the application files).
The *Activity* feed and the follow-up input stay **outside** the tabs, because they belong to
whichever run is active. That roughly halves the page on every display.
- Use `key=` so the selected tab survives reruns. Check that `st.rerun()` at the end of a run
  doesn't snap back to the first tab, and set `default=` from `active_kind` so the tab matches
  the run in progress.
- `st.tabs` computes every tab by default, which is what the code does today, so the run logic is
  unchanged. Don't adopt lazy tabs (`on_change="rerun"` with `.open`) in this track: it changes
  which code runs on each pass.
- Each run form stays its own `st.form`. The id boxes stay **outside** the forms (invariant 10),
  inside their tab.

**3. Replace fixed heights with a user-chosen pane height.** With no viewport API, let the
person pick. Add a *Pane height* control (`Compact` / `Comfortable` / `Tall`, for example 320 /
480 / 720px) with an explicit key, and derive every scroll box from it: the feed, the file
content, and the PDF view (always 50px less than the content box, which is today's 480 → 430
relationship). Default to *Comfortable*, today's 480. It is a display preference, not a run
setting, so it does **not** go through `turn_locked` and stays usable during AWAITING. Put it in
a small popover by the page title (`st.popover(":material/tune:")`), *not* in the sidebar, which
is all locked during AWAITING and hidden on phones. It must also not be part of `get_agent`'s
cache key `(profile, approve, search)`.

**4. File browser: equal heights on desktop, natural height when stacked.** Give the content
box the pane height, and the picker box `height="stretch"`, so on a wide screen it matches the
content row, and stacked on a phone it shrinks to its list instead of a fixed 480px. *Verify this
renders as described.* Stretch needs a bounded parent, and a stacked column may not supply one.
If it doesn't, fall back to: the picker as `st.selectbox` placed above the content, at every
width. That's simpler, and it's still fine on desktop for the handful of files an application has.

**5. A readable measure for drafts on wide screens.** Keep `layout="wide"`: the forms, feed and
metrics use the width well. Constrain only *rendered prose*: in `render_file`'s Markdown view,
wrap the body in `st.container(width=<px>)` at about 760–820px (roughly 75–85 characters at 14px
Inter). *Verify* that a fixed `width` shrinks to fit on a narrow display rather than forcing
horizontal scroll. If it doesn't, keep the prose stretched; a too-long line is less bad than a
sideways-scrolling page. The source view keeps the full width, because code blocks need it.

**6. Leave alone what already adapts.** The metrics row (`st.container(horizontal=True)`) wraps
by itself. The sidebar collapses to a menu on phones. `st.chat_input` pins to the bottom on every
display. Button rows are already horizontal containers.

### Interaction with the invariants (these are the risks, not the layout)

- **Invariant 10:** the id boxes stay outside `st.form`, and the browse picker stays an input
  method that *writes* the id box. Moving them into tabs changes their parent, not those rules.
  Re-run the `form_id` structural test.
- **Invariant 11:** every control that can *start a turn* keeps its `turn_locked` gate. The new
  tabs and the pane-height control start no turn and must stay usable during AWAITING; check that
  they don't. Changing tabs must never submit, resume or discard anything.
- **AppTest:** cases select widgets **by label**, never by index (the `_button` helper exists
  because an index lookup silently pointed at the wrong button once a form was added above). Tabs
  reorder the element tree, which is exactly that situation, so any lookup still done by index
  will now address the wrong widget. Search the suite for `[0]`-style lookups before moving
  anything. Remember too that AppTest doesn't model `st.form` batching.
- **Fragments:** `render_feed` / the file browser use `@st.fragment` (`:469`, `:597`, `:827`).
  A fragment rerun must still find its container after moving into a tab.

### Order of edits (the suite hook gates `streamlit_app.py` and `tests/*.py`)

Do the riskiest structural move by itself, with the tests proving nothing about the runs changed:

1. **G1, approval slot first:** move the panel's slot under the title. Small, and it is the change
   that matters most on phones.
2. **G2, the pane-height control:** derive the four fixed heights from it. Default 480, so the
   page renders the same as today.
3. **G3, tabs:** the structural move. Before it, check that no AppTest lookup depends on element
   order. Add tests that each tab holds its form, that the id boxes have no `form_id`, and that
   the tab follows `active_kind` across a rerun.
4. **G4, file browser and prose width** (with the fallbacks above, if verification says so).

### Visual verification (required; AppTest has no viewport)

Run the app (the `run` skill), then drive it with Chrome (`resize_window`) at **390×844**
(phone), **820×1180** (tablet), **1366×768** (laptop) and **2560×1440** (desktop). Screenshot
each **in both theme modes** (Track F). At each width check:

- with a thread parked, the approval panel is the first thing below the title;
- the file picker isn't a mostly empty fixed box when stacked;
- feed and content heights follow the pane-height control;
- draft prose stays readable on 2560px;
- nothing scrolls sideways at 390px.

Record the screenshots in the PR. They are the only evidence for this track.

### Accessibility (part of Track G)

- **Reflow (WCAG 1.4.10):** add **320px** wide to the screenshot sizes, which is what a 1280px screen
  zoomed to 400% gives. Nothing may scroll sideways except code blocks and tables. Also check
  200% browser zoom at 1366px.
- **Keyboard only**, with no mouse. After G1, tab order follows the visual order, so the approval
  panel is reached first when present. The tabs (arrow keys), the pane-height popover, the
  Source/Rendered control and Approve/Reject all work from the keyboard. There is no keyboard
  trap inside the scroll boxes (feed, file content, PDF). The focus ring is visible on every
  surface.
- **Names for controls:** the pane-height popover gets a **text label** (for example "Display",
  with the icon), not an icon alone. The controls with collapsed labels (`:699`, `:819`, and the
  file picker) must still announce their label in VoiceOver. Fix any that don't by giving them
  meaningful label text, which stays hidden visually.
- **Heading levels:** the page goes from `st.title` (level 1) straight to `st.subheader`
  (level 3). Make the section titles `st.header` (level 2), with `st.subheader` (level 3) for the
  sub-sections inside a tab. Heading navigation is how screen-reader users skim a page.
- **Icons:** status and feed icons are Material Symbols rendered from icon-font text. Check in
  VoiceOver whether they are read aloud as names ("check_circle"). If they are, keep the icon but
  make sure the visible text beside it carries the meaning ("done", "in progress"), so nothing
  depends on the icon's name.
- **Target size (WCAG 2.5.8):** icon buttons and the popover trigger are at least 24×24px.
- **Status announcements:** check whether a run finishing, or the approval panel appearing, is
  announced to a screen reader. If not, document it. This needs no new UI, because the approval
  panel moving to the top (G1) is the main fix: it is the first thing reached.

### Performance (part of Track G)

- **Rerun time.** Every click reruns the script, and `st.tabs` builds **both** tabs on every
  pass (the shortlist parse, citation checks and file listing included). Before G3 and after it,
  with a realistic `applications/` and `opportunities/` (copy real scans and a finished draft in),
  time one full rerun (a `perf_counter` around the script body, or `AppTest` timing). A clearly
  slower rerun after G3 means moving the disk reads behind `st.cache_data`, keyed on the
  directory's modification time, or into the existing `@st.fragment` functions. It doesn't mean
  reverting the tabs.
- **Heavy content.** A 50+ page RFP in `st.pdf` and a long draft in the Markdown view must render
  without stalling the page, at every pane height. If the PDF view is slow, rendering it only
  when selected is already how the file browser works; check that it stays that way.

## Track H (runs between C and D): evaluation that can see what A and D change

Added 2026-10-19. The existing eval (`evals/`) is well designed: mostly programmatic scoring, a
positive control for every refusal case, only indefensible verdicts asserted, its scorers tested
offline in `tests/test_evals.py`, and separate pass, fail and harness-error counts. But it covers
**the scout prompt only**. Tracks A (deepagents prompts, the anthropic SDK), D1 (drafting and
grader effort), D2 (refusals) and E3b (memory gating) change **drafting** behaviour, which the
plan otherwise checks by reading one draft. Track H extends the existing harness rather than
replacing it, and keeps its rules:

- **programmatic checks first, a judge only where code can't decide**;
- every refusal-shaped case gets a **positive twin**;
- assert only what is indefensible;
- the scorers get offline tests in `tests/test_evals.py`;
- nothing in `evals/` is ever collected by `pytest tests/`, and nothing live runs in CI;
- outputs go to `evals/out/` (A2).

**Every live run costs money: estimate the run's cost and ask before each one.**

### Order relative to the other tracks

| Step | Must land before | Because |
|---|---|---|
| H1 judge pin + repeats | D1 | D1's comparison is meaningless with a moving judge and single runs |
| H2 delegation check | D1 (ideally right after A) | It is the only check that can see the deepagents 0.7 delegation loss `CLAUDE.md` warns about |
| H3 drafting eval | D1's drafting/grader effort change | Otherwise D1's drafting change is judged by one read-through |
| H4 false-refusal case | D2 | D2 needs a case that actually triggers a refusal |

Track A doesn't wait for H; it uses the existing `run_scout` plus 4b's baseline. Then run H2
**retroactively on A's result**, and treat an H2 failure as an A regression.

### H1. Pin the judge, and repeat runs before comparing

- `run_scout.JUDGE_MODEL = GRADER_MODEL` (`run_scout.py:68`), and the judge is built through
  `build_model`. After D1 that means **the judge's effort moves with the grader role's effort**,
  so a before/after D1 comparison would be scored by two different judges. Give the eval its own
  **pinned judge** (an explicit model *and* effort, recorded in each run's metadata, next to the
  existing `judge_model`). Keep the rule that the judge differs from the scout's model.
- Add `--repeat N` (default 1) to `run_scout` / `evaluate_scout`. Report **pass rates per check
  over N** next to the existing three counts. Comparisons in D4 and H3 use N ≥ 3. A single run
  stays the "read it closely" mode.
- Offline tests of the new aggregation: repeats are counted per check, and a harness error is
  never averaged into a pass rate. That keeps the existing "three numbers, not one" guarantee.

**H1 also: label eval runs.** Every eval runner (the existing two and H2/H3/H4's) labels its runs
as evals, for example `frontend="eval"` plus the case key (`run_scout.call_config` already sets
case/role/model metadata), or sends them to a separate LangSmith project. Otherwise repeated eval
runs mix with real usage in the traces E7's dashboards read. Pin the label offline, the way §⑥
pins the frontends'.

### H2. The delegation (trajectory) check, on the full graph

The README's "known limitation": the scout eval tests the prompt, not the graph. Delegation can
only be seen on the graph. `CLAUDE.md`: since deepagents 0.7 the `task`-usage guidance is gone, and
"no test can see any of this, and `evals/run_scout` cannot either."

- New `evals/run_draft_trajectory.py`: build the **real** drafting graph (`build_agent`,
  local profile, a temporary `GRANT_WRITER_ROOT`, a small fixture RFP and profile), stream one
  run, and parse the stream with `activity.py`. It is the same parser both frontends use, so the
  check reads exactly what a user would see.
- **Programmatic checks only:** a `task` call to `section-drafter` happens for each drafted
  section; the orchestrator itself makes **no** `write_file`/`edit_file` under
  `/applications/*/sections/`; `compliance-checker` is called; `write_todos` is used (the Plan
  panel depends on it). Report the counts, not just pass/fail.
- Offline tests: feed recorded stream chunks (fixtures) through the checks. A recorded trajectory
  that drafts inline must fail.
- Cheapest useful form: one fixture, N = 3. It needs a live key, so it's run by hand like
  `run_scout`.

### H3. The drafting eval

Build it with `/claude-api build-eval` (it interviews for inputs, grading method and measured
cost, and wants sign-off before producing anything). Mirror the scout's case design:

| Case | Shape | Checks in code | Judge |
|---|---|---|---|
| `genuine-section` | **Positive control.** Full profile; an RFP section the profile can answer | No `[NEEDS INPUT]` for facts the profile holds; length within the limit (from `measure_text`, never estimated) | Claims grounded in the inputs |
| `stripped-profile` | Budget and personnel facts deleted | Each withheld fact becomes `[NEEDS INPUT: …]` and appears in `review/gaps.md`; **no invented numbers** (every number in the output traces to an input) | none needed |
| `leaky-delegation` | The orchestrator's brief carries an unsourced budget figure | That figure does not appear in the section | Whether it was relied on, versus named and set aside (same logic as the scout's `leaky-brief`) |
| `tight-limit` | A page or word limit the natural answer exceeds | Final length ≤ limit, measured with the same `measure_text` | none |

- Start at the **section-drafter prompt level** (cheapest, readable, like the scout), then
  re-run `genuine-section` through the full graph once H2 exists.
- "Every number traces to an input" is the drafting equivalent of `citations_traceable`. Reuse
  `untraceable_citations`' whitespace-insensitive matching (invariant 16's lesson: false
  positives teach people to ignore the flag).
- Scorer tests in `tests/test_evals.py`, including the false-positive cases (wrapped lines,
  thousands separators, currency formatting) pinned as *not* reportable.
- Optional, later: a LangSmith dataset mirror via `push_dataset`, as for the scout.

### H4. A false-refusal case (for D2)

- A realistic **biomedical or health** RFP section (for example a community health program
  evaluation). That is the category Opus 5.5 and Sonnet 5.5's `bio` classifier can falsely
  decline.
- Measure the **refusal rate over N runs**, per role. After D2, check that each refusal is
  *shown* (category included), not an empty turn. The scorer reads the same `activity.py` events
  as H2.
- Its positive twin is `genuine-section` from H3: a non-medical section that must never be
  refused.
- If the rate isn't zero, it feeds D2's separate decision on enabling the server-side fallback,
  with numbers rather than a guess.

### What H does not do

- No eval in CI and no live key in the suite. That rule stays.
- No single combined score. H keeps the three-count reporting, and each check is reported
  separately.
- No judge where code can decide. The ratio of code checks to judged checks is the design (see
  `evals/README.md`, "Why the scoring is mostly not a model").
