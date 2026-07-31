# Grant Writer Agent

[![CI](https://github.com/darylalim/grant-writer-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/darylalim/grant-writer-agent/actions/workflows/ci.yml)

A grant writing agent built on [Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview).

It covers both halves of the work. `discover` sweeps grants.gov and the web for
opportunities and fit-scores each against your organization profile, so you know
which are worth the week that drafting costs. `draft` then takes the
solicitation and writes the proposal — extracting the requirements, researching
the funder, drafting each section, auditing the result, and assembling a
submission-ready draft.

**This drafts proposals; it does not submit them.** Every output needs human
review before it goes to a funder — see [Guardrails](#guardrails).

## Quick start

```bash
uv sync
cp .env.example .env             # add ANTHROPIC_API_KEY, TAVILY_API_KEY
$EDITOR memories/org/AGENTS.md   # fill in your organization

# Find something worth applying to
uv run grant-writer discover --scan-id rural-health-2026 --focus "rural health education"

# Then write it
uv run grant-writer draft --app-id nsf-aisl-2026 --rfp ~/Downloads/solicitation.pdf --funder NSF
```

### Commands

| Command | What it does |
|---|---|
| `discover --scan-id ID` | Search, triage, and fit-score candidates; print a ranked shortlist |
| `draft --app-id ID` | Extract requirements, research, draft, audit, assemble |
| `chat --app-id ID` | Resume an application's thread to refine it |

The id doubles as the LangGraph `thread_id`, and the CLI persists its checkpoint
to `.grant_writer/checkpoints.sqlite` — so a later `chat` or `draft` with the
same id picks up the same plan, todos, and history, not just the files on disk.
A scan id is prefixed before it becomes a thread id, so reusing one name for
both — scan `rural-health-2026`, then draft it — is the expected workflow rather
than a collision.

### Flags

Every flag goes **after** the subcommand, e.g. `grant-writer draft --app-id X --approve`.

| Flag | Commands | Effect |
|---|---|---|
| `--focus TEXT` | discover | What to search for |
| `--agencies CODES` | discover | Pipe-separated grants.gov agency codes, e.g. `USDA\|NSF` |
| `--rfp FILE` | draft | The solicitation PDF |
| `--funder NAME` | draft | Funder name, e.g. `NSF` |
| `--rubric FILE` | draft | Grade the draft against the funder's review criteria and iterate |
| `--notes TEXT` | discover, draft | Extra context for this run |
| `--approve` | all | Require human approval before writing anything under `final/` |
| `--no-search` | all | Run without web search (no `TAVILY_API_KEY` needed) |
| `--profile server` | all | Keep drafts in graph state instead of on disk |
| `--recursion-limit N` | all | Max graph steps per turn (default: 150) |

`discover` accepts `--approve` but it does nothing there: a scan writes nothing
submission-bound, so that graph has nothing to pause on. See
[Permissions](#permissions).

## Finding opportunities

```bash
uv run grant-writer discover --scan-id rural-health-2026 \
    --focus "afterschool STEM in rural districts" --agencies "USDA|NSF"
```

Output lands in `opportunities/<scan-id>/` — the full text of each candidate
under `candidates/`, one fit-scoring file per candidate under `scored/` — and
the ranked shortlist prints when the run ends.

grants.gov needs no API key, so `discover --no-search` still covers US federal
opportunities. Web search is what adds private foundations, state agencies, and
non-US funders.

### How the scoring works

**The agent never states a score.** It answers six criteria — eligibility,
mission alignment, program fit, track record, award size fit, timeline
feasibility — with one of four verdict words, each carrying a quoted citation
from either the opportunity text or your profile. The weights live in
`opportunities.py` and are never shown to the model, so the percentage is
computed from the verdicts rather than asserted. A fabricated total is not
caught here; it is unrepresentable, because the format has nowhere to put one.

Three rules follow, and each exists because a shortlist is read once and acted on:

- Anything your profile does not answer becomes `[NEEDS INPUT: …]`, never a guess.
- A candidate whose file cannot be parsed reads as **unscored**, never as 0%.
  Those are opposite claims, and only one of them means the opportunity was judged.
- Eligibility is gating — a `NONE` there marks the candidate ineligible — but it
  does not zero the score. A surprising ineligibility call is exactly the one
  worth checking, and the evidence for it should still be on screen.

**Every citation is checked against the file it names**, and any that cannot be
found there is flagged in both frontends. The scout has no tools, so everything
it knows arrives from the two files it reads or from the orchestrator's
delegation message — and that message can carry the orchestrator's own web
research, which a scout then quotes in good faith: true, and unverifiable,
because the pane offering to show you the source cannot show it. The check
ignores line wrapping, emphasis, and case, since a real quotation routinely
spans a wrapped line and a flag on an honest citation costs more than the one
it was meant to catch.

## Drafting a proposal

```bash
uv run grant-writer draft --app-id nsf-aisl-2026 \
    --rfp ~/Downloads/solicitation.pdf --funder NSF --rubric criteria.md
```

Pass `--rubric` with the funder's published review criteria and the agent grades
itself against them and iterates — see
[Self-grading](#self-grading-against-review-criteria).

Output lands in `applications/<app-id>/`:

| Path | Contents |
|---|---|
| `rfp.md` | The extracted solicitation text, archived before anything reads it |
| `requirements.md` | Every required section, limit, review criterion, and deadline, quoted and cited |
| `research/` | Funder intelligence — priorities, recent awards, program language |
| `sections/` | One file per narrative section; all revision happens here |
| `review/` | Compliance reports and `gaps.md`, the collected `[NEEDS INPUT]` questions |
| `final/` | Assembled submission-ready text, written once per file |

Refine it later without starting over:
`uv run grant-writer chat --app-id nsf-aisl-2026`.

## Web UI

An optional Streamlit front end over the same two graphs:

```bash
uv run streamlit run streamlit_app.py
```

It shares the CLI's checkpoint, so a run started in the browser continues with
`grant-writer chat --app-id X` and back again. Both stages sit on one page in
the order the work happens — **Find opportunities** above, **Draft a proposal**
below — on separate threads, so browsing an old scan never disturbs a draft.

What it adds over the CLI:

- The **content** of each submission-bound file at the approval prompt, not just
  the path.
- A count of the unresolved `[NEEDS INPUT]` markers across the drafts — the
  number that says how much human input the proposal is still waiting on.
- A shortlist that expands each candidate to its six verdicts and their
  citations, with the source text one click away. The scoring is the product,
  but a citation is only worth what its source says.

The chat box under the activity feed sends what `grant-writer chat` sends — a
plain message on the same thread, not a second brief, so the agent keeps the
plan and todos it already has. It is disabled while a turn is streaming and
while an approval is pending: the graph is parked on the interrupt then, and a
message sent into that starts a new turn rather than answering it, abandoning
the pending submission-bound write instead of approving or rejecting it.

File previews default to **source**, with a toggle to read them rendered;
anything under `final/` always opens as source. What you approve has to be the
bytes that get written, and rendered markdown shows a citation's link text
rather than its URL — the one thing worth checking as you sign off.

Approvals default to **on** here. The CLI leaves `--approve` off because each
prompt costs you a context switch; in a UI it costs one click.

Notes on the implementation:

- The application id is validated before it is joined onto a path
  (`config.application_dir`). It names a directory the app writes your upload
  into, so an id like `../..` would otherwise read and write anywhere on disk —
  `Path("applications") / "/etc/x"` is just `/etc/x`.
- Appearance is entirely `.streamlit/config.toml`, a neutral zinc theme in light
  and dark that follows your desktop. Its own comments explain what must not
  move between sections; `tests/test_theme_config.py` pins the rest. The app
  injects no CSS and should not start — theme tokens are the supported way to
  restyle Streamlit, and `unsafe_allow_html` styling breaks silently when the
  class names it targets change.
- `streamlit_app.py` lives outside `src/`, so the package never imports
  streamlit and the console script pulls in no web dependencies. It is also not
  in the wheel, which is why streamlit is a **dev dependency** rather than an
  optional extra — an extra would have been installable by someone who then had
  no app to run.

## Architecture

Two graphs over shared infrastructure:

```
discovery (sonnet)   searches grants.gov + web, triages, delegates scoring
└── opportunity-scout (sonnet, write-limited)  scores one candidate per call

orchestrator (opus)  reads RFP, extracts requirements, plans, delegates, assembles
├── funder-researcher   (sonnet + web search)  what this funder actually rewards
├── section-drafter     (opus + skills)        one section per call
└── compliance-checker  (sonnet, write-limited) audits drafts, cannot edit them
```

Why these seams: research burns tokens on search results the drafter never
needs, drafting needs the funder-specific style guides loaded, and compliance
has to judge the drafts without the drafter's rationalizations sitting in
context. Subagents get a fresh context per call, which is exactly right for
"draft section N" and exactly wrong for "now revise what you just wrote" — so
every delegation names the files to read and the path to write.

Discovery is a separate graph rather than a mode of the drafting orchestrator
because nothing under `/opportunities/` is submission-bound: it never gets an
`interrupt` permission, so a scan structurally *cannot* pause for approval, and
neither frontend needs an approval path for it.

### Where state lives

| Path | Lifetime | Purpose |
|---|---|---|
| `memories/org/AGENTS.md` | Forever, every application | Org identity — loaded into context every turn |
| `skills/*/SKILL.md` | Forever, loaded on demand | Section-specific drafting craft |
| `opportunities/<scan-id>/` | One scan | Candidate texts and their fit scoring |
| `applications/<app-id>/` | One application | RFP text, requirements, research, drafts, reviews |

The split matters for context budget. Org identity is compact and always
relevant, so it belongs in memory. A funder's formatting rules are thousands of
tokens that matter for one section, so they belong in a skill that loads only
when needed.

### Backends

`--profile local` (default) uses `FilesystemBackend` rooted at the repo. On a
laptop the filesystem *is* the cross-session memory — drafts are real files you
can open in any editor, and they survive restarts for free.

`--profile server` keeps drafts in graph state and routes `/skills/` and
`/memories/` to a `Store` seeded from disk at startup (`seed_store_from_disk`),
so the drafter still gets its section guides and the org profile. Each route
lives in its own Store namespace with prefix-stripped keys, because
`CompositeBackend` strips the route prefix before delegating.

**Never use `local` inside a web server.** The local CLI persists its checkpoint
to SQLite (`.grant_writer/`), but the server profile's `InMemoryStore` lives
only for the process — swap it for `PostgresStore` before deploying, or seeded
skills and memory die on restart.

### Permissions

The drafting agent may write only to `/applications/`, `/memories/`, and
`/opportunities/` — the set is named once, as `config.CONTENT_DIRS`, and both
enforcement points read it. Everything else, including its own `skills/` and
this source tree, is read-only. Rules are evaluated **first-match-wins**, so
specific allows must precede the catch-all deny; see `backends.py`.

`--approve` adds an `interrupt` rule on `/applications/*/final/**`. That is
deliberately narrower than interrupting every write: gate the submission-bound
files and you read each prompt, gate every scratch note and you learn to
rubber-stamp.

The discovery graph gets a narrower set of its own (`discovery_permissions`),
which returns no interrupt rule at any setting and denies `/applications/`
outright. That is what makes "a scan cannot pause for approval" structural, and
both frontends rely on it: neither checks for a pending write before starting a
scan.

Those rules govern writes **through the backend**. The two tools that write to
real disk — `extract_pdf_text` and `fetch_grants_gov_opportunity` — bypass them
entirely, so each carries its own allowed directories rather than the
project-wide set: drafting archives beside the drafts, discovery only into a
scan, and neither set a superset of the other. A tool handed the union cannot
tell which graph is calling it, and a `final/` write from the discovery tool
would skip the approval gate the rules exist to enforce.

### Self-grading against review criteria

`RubricMiddleware` is included unconditionally and stays dormant until a
`rubric` is present in invocation state. Pass `--rubric criteria.md` with the
funder's published review criteria and each time the agent would finish, a
grader scores the transcript against them and sends it back if unsatisfied (up
to 3 iterations). The agent is then iterating against the same standard the
reviewers will apply.

### Running outside the repo

Paths resolve relative to the project root — the directory holding `skills/`,
`memories/`, and `applications/`. In a checkout that is found automatically. If
you install the console script elsewhere (`pipx`, `uv tool install`), point it
at your content with `GRANT_WRITER_ROOT=/path/to/project`.

## Guardrails

Fabrication is the failure mode that matters here — invented preliminary data,
personnel, or budget figures are misconduct, not a bad draft. So:

- Every prompt forbids inventing facts. Unknowns become
  `[NEEDS INPUT: <question>]` and are collected in `review/gaps.md`.
- `measure_text` exists because models cannot count words by inspection, and an
  over-length narrative is rejected without review.
- The compliance reviewer cannot edit what it reviews.
- The scout cites every verdict, and an over-scored candidate costs a human the
  week they would have spent on a real one — so an honest `NONE` beats a hopeful
  `STRONG`.

**This drafts proposals; it does not submit them.** Every output needs human
review before it goes to a funder — especially any `[NEEDS INPUT]` marker, every
number, and every citation.

## Development

```bash
uv sync                     # installs the dev group, streamlit included
uv run pytest tests/ -q     # the whole suite: offline, no API calls
uvx ruff check src/ tests/ evals/ streamlit_app.py
```

Don't develop with `--no-dev`. The `AppTest` cases in `test_frontends.py` run
the Streamlit script headlessly and `importorskip` out without streamlit, and ty
resolves imports statically — so `tests/` importing streamlit makes the type
check report an extra diagnostic and trip the baseline.

The tests target the failures that are *silent* in a Deep Agents setup: a
subagent that lost its skills (custom subagents do not inherit them), a
permission rule ordered so the deny shadows the allow, or an interrupt
configured without the checkpointer that makes it work. `test_review_fixes.py`
pins past code-review findings.

`evals/` is deliberately **not** part of the suite. The tests cover wiring; they
cannot cover the prompts, and the prompts are the product. Run them by hand
against a live model:

```bash
uv run python -m evals.run_scout   # costs money
```

CI runs the suite on every push and pull request against Python 3.13 and 3.14,
plus `ruff format --check` and `ty` held at its two-diagnostic baseline. ruff
and ty are version-pinned there so a new release cannot turn CI red on unchanged
code — bump them deliberately.

### Known quirks

- An `execute` tool is advertised to the model but is inert on
  `FilesystemBackend`, which is not a `SandboxBackendProtocol`. It returns a
  clear error rather than running anything, so it is not a permission bypass —
  `test_execute_tool_is_not_a_permission_bypass` guards that assumption.
- `RubricMiddleware` is marked beta upstream; its API may change.
- Adding `LANGSMITH_API_KEY` is strongly recommended. A run that fans out to
  three subagents is close to impossible to debug from stdout alone.

## License

MIT — see [LICENSE](LICENSE).
