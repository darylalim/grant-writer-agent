# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                                       # install (Python >=3.13, uv_build backend)
uv run pytest tests/ -q                       # full suite: offline, no API calls
uv run pytest tests/test_wiring.py::test_subagent_roster -q   # single test
uv run pytest tests/ -q -k permission         # by keyword
uvx ruff check src/ tests/ streamlit_app.py   # lint (select list in [tool.ruff.lint])
uvx ruff format --check src/ tests/ streamlit_app.py   # ruff's default 88-col

uv run grant-writer discover --scan-id X --focus "rural health"   # before drafting
uv run grant-writer draft --app-id X --rfp path.pdf --funder NSF
uv run grant-writer chat --app-id X           # resume the same thread

uv run streamlit run streamlit_app.py         # optional Streamlit front end
```

streamlit is in the `dev` group, which `uv sync` installs by default, so this needs no extra flag.
Never develop with `--no-dev`: `tests/` imports streamlit and ty resolves imports statically, so
without it the ty hook reports 3 diagnostics and blocks every Python edit — and the `AppTest` cases
skip, which is the only coverage `streamlit_app.py` has. It is a dev dependency rather than an
optional extra because `streamlit_app.py` sits at the repo root and is not in the wheel, so an
extra would have been installable by someone who then had no app to run.

When working with Python, invoke the relevant `/astral:<skill>` — `/astral:uv`, `/astral:ruff`,
`/astral:ty` — to ensure best practices are followed rather than guessed at. uv is the only
supported package manager: never `pip`, never a hand-rolled venv. There is deliberately no
`[tool.ty]` config; `uvx ty check src/ tests/` reports two diagnostics, both upstream signature
problems in `deepagents`/`langgraph`, and neither is worth suppressing — leave them. That
tolerance is encoded as a threshold in two places, the ty hook in `.claude/settings.json` and
the type-check step in `.github/workflows/ci.yml`; lower both when upstream fixes theirs.

Common flags (`--profile`, `--approve`, `--no-search`, `--recursion-limit`) live on a shared
parent parser and must be passed **after** the subcommand. `--app-id` is also the LangGraph
`thread_id`; the CLI persists to `.grant_writer/checkpoints.sqlite`, so state resumes across
processes. `applications/`, `opportunities/`, and `.grant_writer/` are gitignored.

`discover` takes `--scan-id` rather than `--app-id`: a scan is not an application, and the two
must not collide on a thread id even when a human reuses the name (invariant 12). It inherits
`--approve` from the shared parent parser, where it is inert — nothing a scan writes is
submission-bound, so no interrupt rule reaches it. Accepted rather than split out so every
subcommand takes the same flags and the UI's one sidebar can drive both graphs.

## Architecture

Two [Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview) graphs over shared
infrastructure: a drafting orchestrator plus three subagents, and a discovery orchestrator plus one.
`agent.py` builds both — `build_agent` and `build_discovery_agent` — and everything below the
orchestrator (`Settings`, backend, permission builder, checkpointer) is shared as-is.

Discovery is a second graph rather than a mode of the first, and the deciding reason is a permission,
not a preference: `backends.discovery_permissions` returns **no interrupt rule at any setting**, so a
discovery thread cannot park on an approval. That is what lets both frontends skip the approval
panel, the `is_parked` check, and the rest of invariant 11's machinery for a scan. Folded into
`build_agent` it would be one graph that sometimes can park and sometimes cannot, which is far harder
to keep true than two that differ honestly.

**That guarantee is a property of the rules, not of the roster**, and the distinction is not
academic — it was got wrong once. `build_discovery_agent` originally shared
`build_permissions(settings)`, on the argument that this graph has no `section-drafter` to reach
`/applications/*/final/**`. But the *orchestrator* has `write_file` like any other, and
`WORKSPACE_CONVENTIONS` describes that directory to it, so one stray write there would have parked a
thread nothing was watching — and with the frontends' `parked_state` check deliberately absent, the
next scan would have discarded the pending write silently. Invariant 11's failure, reached through
the graph built to be exempt from it. `test_a_scan_can_never_park_on_an_approval` now asserts the
absence of `interrupt` across the whole write surface rather than the presence of `allow` on the
happy path. The second
reason is scope: dispatching between drafting and discovery inside `ORCHESTRATOR_PROMPT` puts the
choice in prose, where a misread costs a wrong workflow; in the roster it cannot be misread, because
the discovery graph has no `section-drafter` to reach.

Module dependencies flow one way:

```
config.py  →  tools.py, prompts.py  →  backends.py, subagents.py  →  agent.py  →  cli.py
opportunities.py ─→ prompts.py, workspace.py, cli.py ─────────────────────────↗
              activity.py, workspace.py  ──────────────────────────────────────↗  streamlit_app.py
```

**`opportunities.py`** is a pure leaf — the fit rubric, the grammar a scout writes, the parser, and
the ranking. No filesystem, no network, no model, and no `grant_writer` imports of its own. That is
what makes the scoring testable offline, which matters more here than anywhere else in the package:
a shortlist is read once, acted on, and never audited, so a scorer that is quietly wrong is a funder
never applied to. `prompts.py` imports `rubric_brief()` from it so the criteria the scout is asked
for and the criteria the parser counts cannot drift — that failure is silent in the worst way, since
the scout answers a criterion nobody scores and the only symptom is a total that comes out low.
`workspace.py` owns the disk side (`scan_files`, `read_scored_opportunities`).

Two frontends sit at the end of that chain, and what they share is deliberate — each item below
was duplicated logic waiting to happen:

- **`activity.py`** parses the `stream_mode="updates"` chunks and builds the approval decisions.
  A renamed `deepagents` key blanks a frontend's labels silently; a decision list whose length
  disagrees with the interrupted calls leaves the graph stuck with no exception raised.
- **`workspace.py`** reads an application directory back — file listing, `[NEEDS INPUT]` count,
  compliance verdict. Not presentation logic, and putting it here is what makes it testable: a
  Streamlit script cannot be imported without executing it.
- **`prompts.draft_request`** composes the opening brief, which steers the whole run.
- **`config.persistent_settings`** decides that a frontend checkpoints to disk.
- **`config.application_dir`** is the boundary for a user-supplied application id. The UI joins it
  onto a real path and writes an upload there, outside `FilesystemPermission` — and
  `Path("applications") / "/etc/x"` is `/etc/x`, so an unvalidated id is an arbitrary read/write.
  This is the third place enforcing that boundary; see invariant 2.

- **`config.py`** is the only module that touches `os.environ`. Model IDs, `PROJECT_ROOT`, and the
  frozen `Settings` dataclass all resolve here. Everything downstream takes `Settings`.
- **`agent.py::build_agent`** is the single assembly point and returns a plain LangGraph graph.
- **`prompts.py`** is product surface, not boilerplate — the anti-fabrication and delegation rules
  live in the prompt text. `WORKSPACE_CONVENTIONS` is shared across orchestrator, drafter, and
  reviewer; the directory layout it describes must match `backends.py` and `config.py`.

Roles are split where context isolation pays: research floods context with search results, drafting
needs skills loaded, and compliance must judge drafts without the drafter's rationalizations in
context. Model tier per role (`DRAFTING_MODEL` opus, others sonnet) is overridable via
`GRANT_WRITER_*_MODEL` env vars.

### Virtual paths vs. disk

The agent sees `/skills/`, `/memories/org/AGENTS.md`, `/applications/<id>/` — virtual paths the
backend maps onto real directories (`local`) or a `Store` (`server`). When editing prompts, skills,
or tool docstrings, use the virtual form; when editing Python that touches disk, use `Settings.root`.

`_resolve_root()` prefers `GRANT_WRITER_ROOT`, then the source-relative root if it contains
`skills/`, then cwd — so the console script works outside a checkout.

## Invariants that fail silently

These are the reasons `tests/` exists. Each is pinned by a test; breaking one still imports, still
runs, and still produces plausible output.

1. **Permission rules are first-match-wins, and no match means allow.** In
   `backends.py::build_permissions`, specific allows must precede the catch-all
   `paths=["/**"], mode="deny"`. Reordering the list silently opens or closes the whole tree.
2. **Anything writing to the real filesystem bypasses `FilesystemPermission` entirely** — the
   permission rules only cover writes that go through the backend. Two mechanisms do not, and each
   enforces the boundary its own way: `tools.py::_resolve_output_path` for the virtual paths
   `extract_pdf_text` and `fetch_grants_gov_opportunity` write to, and
   `config.py::_resolve_content_id` for the ids a frontend joins onto a path. Both resolve `..`
   *before* the prefix check — validating the raw string lets `/applications/../src/x` pass and
   then escape when `..` collapses. A further such writer needs the same treatment, not its own
   variant.

   **`config.CONTENT_DIRS` is the union the *permission rules* cover — it is not what a writer
   gets.** `build_permissions` derives `/<name>/**` globs from it, so a new content directory
   reaches the rules in one edit. But `_resolve_output_path` takes its allowed set **from the
   caller**, and that distinction is the correction to a hole this feature opened: handed the
   union, it could not know which graph was calling. The discovery orchestrator carries
   `fetch_grants_gov_opportunity`, and `discovery_permissions` denies it `/applications/**` — but
   those rules govern the *backend*, and a tool writing to real disk never consults them, so one
   `out_path="/applications/x/final/proposal.md"` overwrote a submission-bound file with no
   approval interrupt and no error. `tools._DRAFTING_WRITE_DIRS` and `_DISCOVERY_WRITE_DIRS` are
   each narrower than the union and neither is a superset of the other. A permission rule and a
   real-disk writer are two enforcement points, not one, and only the first knows about graphs.

   The two id boundaries *are* one function: `application_dir` and `opportunities_dir` both call
   `_resolve_content_id`, which takes the noun only to shape the message. Fixing an ordering bug in
   one copy and not the other is exactly the failure having one copy prevents;
   `tests/test_review_fixes.py` §⑨ parametrizes the same escape list over both to pin that sharing
   it did not loosen either side.
   `config.application_ids` is the read side of the same boundary — it supplies the UI's picker,
   so an id it offers must be one `application_dir` accepts. It gets that by calling
   `application_dir` on each candidate rather than by re-testing `_SAFE_APP_ID`: sharing the regex
   is not sharing the boundary, and the difference is `applications/legacy -> /elsewhere`, which
   passes any name test and is then refused once the symlink resolves.
3. **Custom subagents do not inherit `skills` from the parent.** `subagents.py` passes
   `"skills": [SKILLS_DIR]` explicitly to `section-drafter` and `compliance-checker`. Omit it and
   the drafter still answers — just generically.
4. **`--approve` needs a checkpointer.** Interrupts are a no-op without one; `_build_checkpointer`
   returns SQLite when `Settings.checkpoint_db` is set, in-memory otherwise (tests, library use).
5. **Approval decisions are per action request, not per interrupt.** `HumanInTheLoopMiddleware`
   bundles a turn's interrupted calls into one interrupt carrying N `action_requests`, and resume
   requires exactly N decisions — see `activity.pending_action_requests`, used by
   both the CLI prompt and the UI's approve/reject buttons. N of zero does **not** mean
   nothing is pending: that function skips a renamed payload key and a non-dict interrupt
   value without raising, so an empty list with the graph parked means the pending writes
   could not be read. Both frontends apply a floor of one decision to release the graph;
   the UI refuses a plain approve in that state, since there is nothing to have read.
6. **Server-profile Store keys are prefix-stripped and namespaced per route.** `CompositeBackend`
   strips the route prefix before delegating, so `_SEED_ROUTES` gives `/skills/` and `/memories/`
   separate namespaces; sharing one would let `ls /skills/` surface memory files.
7. **A `SKILL.md` without YAML frontmatter (`name:`, `description:`) is ignored silently.**
8. **`RubricMiddleware` is always installed but dormant** until a `rubric` key is present in
   invocation state (`cli._draft` puts it there from `--rubric`).
9. **Models must be built through `config.build_model`, never handed to deepagents as a
   bare spec string.** Left to the provider default, `ChatAnthropic` reads `max_tokens` from
   a profile registry bundled with `langchain-anthropic` and falls back to 4096 for any id
   it does not recognize — a valid id, an HTTP 200, no exception, and a narrative that stops
   mid-sentence. Opus 5 thinks by default and reasoning is billed against that same ceiling,
   so the cap bites sooner than the word count suggests. `build_model` sets
   `MAX_OUTPUT_TOKENS` explicitly, which is what makes a `GRANT_WRITER_*_MODEL` override to
   an unrecognized id safe — no test can enumerate those. `DEFAULT_MODELS` holds the shipped
   ids so `test_wiring` pins what the project ships rather than whatever the developer's
   environment overrides them to.
10. **The application id input must stay outside `st.form`.** `st.form` batches its
    widgets and sends them only when Submit is pressed. `streamlit_app.py` reads that
    id back to decide which application the file browser shows, so inside the form the
    browser could only ever show an id already submitted this session — reading an
    earlier application meant clicking Draft proposal and starting a fresh billed run
    on it. The page renders, the caption still says to enter an id, and nothing raises.
    The picker beside it is an *input method* for that box: `_pick_application` writes
    the box and clears the picker, so `browse_id` stays one expression. Two controls
    both deciding which application is on screen disagree the moment a run starts on
    one the picker is not showing, and whichever the code reads first is then wrong
    half the time.

    What the move costs, and what pays for it: `browse_id` follows a live widget while
    the graph runs on `active_app_id`, which only the submit handler assigns, so the two
    *can* differ. That is wanted — reading an old draft must not cost a billed turn — so
    the results block says which is which on screen rather than silently retargeting one
    of them. It is bounded on the other side by `turn_locked`, which is `busy or phase ==
    AWAITING`, not `busy` alone: on AWAITING the approval panel is asking a human to vet
    a submission-bound write for `active_app_id`, and the file browser directly under it
    describing a different application is the one moment that context has to be right.

11. **Starting a turn on a parked thread abandons the pending write, and `phase` cannot
    tell you the thread is parked.** A fresh `draft_request` or a plain message on a
    thread sitting on an interrupt does not resume it — the submission-bound write a
    human was asked to vet is discarded, the submit handler clears the feed that recorded
    it, and nothing raises. Two mechanisms, and both are needed:

    - `turn_locked` greys out every control that can start one — the sidebar, the id box,
      the browse picker, the run form, the follow-up input. Gate them on that one
      expression rather than on `busy` at each site, or a phase added later reaches only
      some of them. The sidebar belongs in that list because `get_agent` is keyed on
      `(profile, approve, search)`: flipping one on AWAITING hands Approve a *different*
      graph than the one that parked — with approve off, one built without the
      human-in-the-loop middleware whose interrupt is in the checkpoint.
    - That is a courtesy, not a guarantee, because `phase` is one browser session's
      memory. A reload starts a fresh session at IDLE, a second tab has its own, and
      STOPPED and FAILED are both *inferred from a pass that stopped* rather than from the
      graph, so either can be reached with an interrupt already committed. So **both**
      handlers that start a turn — the run form and the follow-up input — ask the
      checkpoint first and route back into AWAITING instead. That read is what actually
      holds; it is also the only path that puts the approval panel back after a reload.
      Guarding only the form leaves the hole open in the phase whose own banner points the
      reader at the follow-up box.

    Ask it with `activity.is_parked`, never `pending_action_requests(...)` being non-empty.
    Per invariant 5 that returns `[]` for a parked thread whose payload could not be read
    just as readily as for a thread with nothing pending, so a guard written on its
    truthiness fails open in precisely the case the blind-approval panel exists for.
    Reading a request is a display concern and may fail; whether the graph is parked is
    control flow and must not.

    That read can itself *fail*, and the failure has to land somewhere. The checkpoint is
    the SQLite file `grant-writer chat` also opens, so `database is locked` arrives as
    `sqlite3.OperationalError` — neither an `OSError` nor a `ValueError`, so the submit
    handler's except tuple did not cover the read later added above it, and the follow-up
    handler wrapped its call in nothing at all. Both now go through
    `streamlit_app.parked_state`, which owns the `try` (including `get_agent`, for the
    reason spelled out below), and both then **refuse the turn** rather than start one: a
    read that raised cannot rule a pending write out, so unknown has to be treated as
    pending. That is why it returns `bool | None` and not `bool` — `None` (unreadable)
    collapsing into `False` (not parked) starts the turn, which is this invariant's own
    fail-open one level up. Refusing is safe in a way starting is not: the run is still
    checkpointed and the same id picks it up.

    AWAITING is then the one phase with no enabled way out, which makes the approval panel
    the sole exit — so everything that draws it sits inside one `try`, **including
    `get_agent`**, which builds models and opens the SQLite connection and so raises on a
    read-only `.grant_writer/` or a dropped key. One line left outside that block produces
    the identical unrecoverable page: a traceback over controls that are all disabled,
    re-thrown every rerun. It falls through to the blind panel instead, whose Reject needs
    no readable request to send, and which offers a retry when the cause was an exception
    rather than a payload it could not parse.

    That makes **three** sites that build the agent, and all three must guard it — the two
    turn-starting reads via `parked_state`, the panel's own read, and the run block, which
    is the one it is easiest to forget because nothing there looks like a checkpoint read.
    It is reached from the blind panel: Reject sets `phase = RUNNING` and reruns, and the
    run block then calls `get_agent` with the payload already consumed. Unguarded, the same
    cause that produced the blind panel raises again and escapes — the resume `Command` is
    gone, the panel is no longer drawn because the phase is no longer AWAITING, and the next
    pass infers STOPPED, whose banner invites re-submitting the same id over an interrupt
    still committed. The sole exit from AWAITING then ends in exactly the abandonment this
    invariant exists to prevent.

12. **A scan id and an application id share a character set and one checkpoint file, but must
    never share a thread id.** Reusing the name across `discover --scan-id X` and
    `draft --app-id X` is the expected workflow — the CLI's closing line suggests it. Unprefixed,
    those are the same checkpoint row: two structurally different graphs resuming each other's
    conversation, nothing raised, and no symptom until someone notices the drafter answering as
    though it had been searching. Every caller goes through `config.discovery_thread_id`, never a
    literal. `_SAFE_ID` forbids `:`, so the prefix cannot collide with any valid application id
    *by construction* rather than by convention.

13. **The scout writes verdict words; the weights live only in `opportunities.py`.** `SCOUT_PROMPT`
    never shows a point value, so a fabricated total is not caught — it is unrepresentable, because
    the grammar has nowhere to put one. Keep it that way: adding weights to the prompt, or a
    `Total:` line to the format, reintroduces exactly the class of invented number that
    `COMPLIANCE_PROMPT` has to police with an arithmetic rule. `rubric_brief()` is rendered from
    `RUBRIC` for the same reason — a criterion named in the prompt but not in the parser is
    answered in good faith and scored zero, and the only symptom is a low total.

14. **`fit_percent` is `float | None`, and `None` must never collapse into `0.0`.** "We could not
    read this" and "this is a bad fit" are opposite instructions to whoever reads the shortlist.
    Collapsed, a candidate written in the wrong format is indistinguishable from one that was
    judged and rejected, and it sinks with no way to tell. Both frontends render `None` as
    "unscored". For the same reason the parser is tolerant *toward keeping a score*: an uncited
    verdict or an unrecognized citation source is flagged and still counted, because silently
    zeroing a real judgement buries an opportunity in a list nobody re-checks. Only an unrecognized
    *verdict word* scores zero — there, the alternative is guessing what the model meant.

15. **`disqualified` is independent of `total_points`.** A gating `NONE` on eligibility marks a
    candidate ineligible and sorts it below everything scored, without zeroing it. A surprising
    ineligibility call is the one most worth challenging, and the evidence for it has to stay on
    screen. `rank_opportunities` sorts in three tiers — scored, disqualified, unscorable — because
    "not at the top" has two different meanings and one sort key would interleave them.

16. **A citation is checked against the file it names, not trusted.** `opportunities.
    untraceable_citations` is pure and `workspace.unverifiable_citations` supplies the disk half;
    both frontends surface the result. The scout has no tools — it cannot search — so everything it
    knows comes from the two files it reads or from the orchestrator's delegation message, and that
    message can carry findings from the orchestrator's own web research. A scout handed those
    quotes them in good faith: true, and unverifiable, because the pane offering to show the source
    cannot show them. Asking the prompt not to is advisory; this is the part that holds.

    **The matching rule must stay whitespace-insensitive, and that is not a nicety.** An archived
    candidate is hard-wrapped, so a real quotation routinely spans a line break. A line-oriented
    comparison reports those as missing — which is exactly how a hand check of the first real scan
    concluded two of its twenty-four citations were unsupported when every one of them was
    genuine. A false positive is the expensive direction here: flagging honest citations teaches
    the reader to skim past the flag, which costs more than the unverifiable citation it exists to
    catch. `tests/test_opportunities.py` pins the wrapped-line, emphasis, case, and
    collapsed-whitespace cases as *not* reportable for that reason.

17. **Neither discovery subagent gets `skills`, and that is deliberate.** Invariant 3 says custom
    subagents do not inherit them, so a missing key usually *is* the bug. Here it is not: all six
    guides under `/skills/` are about drafting a section, and none has anything to say about
    whether to apply at all. `test_the_scout_is_deliberately_given_no_skills` inverts invariant 3's
    test so the next reader finds the reason rather than "fixing" it.

## Backend profiles

`local` (default) roots a `FilesystemBackend` at the project with `virtual_mode=True` — real files
you can open in an editor. `server` puts drafts in `StateBackend` (ephemeral, thread-scoped) and
routes `/skills/` + `/memories/` to a `Store` seeded by `seed_store_from_disk` at startup.
**Never use `local` inside a web server.** The server profile's `InMemoryStore` dies with the
process — swap for `PostgresStore` before deploying.

## Testing conventions

`tests/conftest.py` sets a dummy `ANTHROPIC_API_KEY`, forces `LANGSMITH_TRACING=false`, and
**blanks** `TAVILY_API_KEY` rather than popping it, so nothing hits the network. The distinction is
load-bearing: `config.py` calls `load_dotenv()` at import, which only skips keys already present in
`os.environ`, so popping handed a developer's real key straight back and the suite behaved one way
locally and another on a clean checkout. Every consumer tests it with `if not os.getenv(...)`, so an
empty string reads as absent. Tests that want a key set it with `monkeypatch`.

Tests assert on wiring by reaching into deepagents internals — `_check_fs_permission`,
`supports_execution`, `graph.nodes["tools"].bound.tools_by_name`, `get_graph().nodes`. That is
intentional (it is the only way to catch these failures offline) but brittle: a `deepagents` upgrade
may need these updated.

`test_wiring.py` covers structural invariants; `test_review_fixes.py` pins specific past code-review
findings and should gain a case whenever a review turns one up; `test_opportunities.py` is pure —
strings in, dataclasses out — and is where every fit-scoring case belongs, because none of it needs
a model or a network; `test_frontends.py` pins what the CLI and the UI must agree on — the stream parser, the approval decisions, the shared brief, and the
terminal output the refactor must not have moved. Its `AppTest` cases run the Streamlit script
headlessly, because a Streamlit app fails at run time rather than import time and nothing else would
catch a bad layout call. They `pytest.importorskip("streamlit")`, which only bites under `--no-dev`.
Pass `monkeypatch` to `_app_test` when a case needs the app in its normal
enabled state; otherwise the credential guard renders the disabled-button variant instead.
`_app_test` also clears `st.cache_resource`, whose store is global to the process rather than
per-`AppTest`: without it a case that patches `build_agent` is handed whichever fake an earlier
case cached under the same `(profile, approve, search)` key, and the suite passes or fails on
test order.

**`AppTest` does not model `st.form`, and this is the one place its green tick means less
than it looks like.** `ElementTree.get_widget_states` walks every widget and serialises it
regardless of `form_id`, so `set_value(...).run()` publishes a form widget immediately —
which a browser never does. An assertion about a form widget's effect on the rest of the
page is therefore only meaningful if the case also clicks the submit button; otherwise pin
structure (`widget.form_id`) rather than behaviour, as invariant 10's test does. Four
file-browser cases once passed against a browser where the pane stayed permanently empty.
**Select widgets by label, never by index.** `_button(app, "Draft proposal")` exists because
`app.button[0]` addresses whichever button the script emits first — a fact about page layout,
not about the button a case means. Adding the discovery form above the drafting one silently
retargeted eleven cases, which went on asserting confidently about the wrong run. A label
lookup raises instead.

`AppTest` also serialises a selectbox by *index* (`Selectbox.index` calls
`options.index(value)`), so `st.selectbox(accept_new_options=True)` cannot be driven here
at all — a typed value that is not already an option raises `ValueError`. That is why the
application id is a `st.text_input` with a picker beside it rather than one combined
widget: the combined version is neater and would have made the path-escape cases, the most
security-sensitive input in the app, impossible to test.

The `AppTest` cases run against the project's own `applications/`, because that is the only
tree `application_dir` resolves an id into — and it is gitignored, so its contents differ per
machine. Assert *membership*, never equality, on anything derived from that listing, and force
the empty case by patching `config.application_ids` rather than waiting for the directory to be
empty. A case that assumes empty passes on a clean CI checkout and fails on any machine that has
run the app once.

## Domain rules baked into the prompts

Fabricated preliminary data, personnel, or budget figures are misconduct, not a bad draft. The same
rule reaches discovery: an over-scored candidate costs a human the week they would have spent on a
real one, so `SCOUT_PROMPT` requires a quoted citation per verdict and says plainly that an honest
`NONE` beats a hopeful `STRONG`. When editing prompts or skills, preserve: unknowns become
`[NEEDS INPUT: <question>]` and collect in `review/gaps.md`; lengths come from `measure_text`, never from eyeballing; the compliance reviewer
writes only to `/applications/*/review/` and cannot edit what it reviews; `final/` files are written
once as a complete `write_file`, never built up by `edit_file` (each write there may be a separate
human approval).

An `execute` tool is advertised to the model but inert on `FilesystemBackend` (not a
`SandboxBackendProtocol`). `test_execute_tool_is_not_a_permission_bypass` guards that — if it ever
becomes live, the write permissions stop being a real boundary.
