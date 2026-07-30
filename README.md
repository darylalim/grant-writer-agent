# grant-writer-agent

[![CI](https://github.com/darylalim/grant-writer-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/darylalim/grant-writer-agent/actions/workflows/ci.yml)

A grant writing agent built on [Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview).

Give it a solicitation and an organization profile; it extracts the
requirements, researches the funder, plans and drafts each section, audits the
result against the solicitation, and assembles a submission-ready draft.

It also finds the solicitation. `grant-writer discover` sweeps grants.gov and
the web for opportunities, then scores each against your organization profile —
eligibility, mission, capacity, award size, deadline — so you know which are
worth the week that drafting costs.

## Quick start

```bash
uv sync
cp .env.example .env        # add ANTHROPIC_API_KEY, TAVILY_API_KEY
$EDITOR memories/org/AGENTS.md   # fill in your organization

# Find something worth applying to
uv run grant-writer discover --scan-id rural-health-2026 --focus "rural health education"

# Then write it
uv run grant-writer draft --app-id nsf-aisl-2026 --rfp ~/Downloads/solicitation.pdf --funder NSF
```

Output lands in `applications/nsf-aisl-2026/`. Resume or refine with the same
id:

```bash
uv run grant-writer chat --app-id nsf-aisl-2026
```

The `--app-id` doubles as the LangGraph `thread_id`. The CLI persists its
checkpoint to `.grant_writer/checkpoints.sqlite`, so a later `chat` or `draft`
with the same id picks up the same plan, todos, and history instead of starting
cold — not just the files on disk.

Common flags (`--approve`, `--no-search`, `--profile`, `--recursion-limit`) are
accepted after the subcommand, e.g. `grant-writer draft --app-id X --approve`.

### Options

| Flag | Effect |
|---|---|
| `--rubric FILE` | Grade the draft against the funder's review criteria and iterate until satisfied |
| `--approve` | Require human approval before writing anything under `final/` |
| `--no-search` | Run without web search (no `TAVILY_API_KEY` needed) |
| `--profile server` | Keep drafts in graph state instead of on disk |

## Web UI

An optional Streamlit front end over the same graph:

```bash
uv sync
uv run streamlit run streamlit_app.py
```

Upload a solicitation, watch the plan and delegations stream in, approve the
writes to `final/` with the drafted text in front of you, and read the results
next to the source PDF. It shares the CLI's checkpoint, so a run started in the
browser continues with `grant-writer chat --app-id X` and back again.

Both stages are on one page, in the order the work happens: **Find
opportunities** above, **Draft a proposal** below. The shortlist expands each
candidate to its six verdicts and their citations, with the source text one
click away — the scoring is the product, but a citation is only worth what its
source says. The two run on separate threads, so browsing an old scan never
disturbs a draft, and a scan and an application may safely share a name.

The chat box under the activity feed refines a run without leaving the browser.
It sends what `grant-writer chat` sends — a plain message on the same thread,
not a second brief, so the agent keeps the plan and todos it already has instead
of starting the whole process over. It is disabled while a turn is streaming,
and while an approval is pending: the graph is parked on the interrupt then, and
a message sent into that starts a new turn rather than answering it, abandoning
the pending submission-bound write instead of approving or rejecting it.

Two things it does that the CLI cannot: it shows you the *content* of each
submission-bound file at the approval prompt rather than just the path, and it
counts the unresolved `[NEEDS INPUT]` markers across the drafts — the number
that says how much human input the proposal is still waiting on.

That preview defaults to **source**, with a toggle to read it rendered. What
you approve has to be the bytes that get written: rendered markdown shows a
citation's link text and not its URL, which is the one thing worth checking at
the moment you are asked to sign off on a submission. The file browser applies
the same rule to the same files — anything under `final/` opens as source,
working drafts open rendered — because that is where a submission-bound file
gets re-read after the approval.

The app lives outside `src/`, so the package itself never imports streamlit —
installing the console script pulls in no web dependencies. The flip side is
that `streamlit_app.py` is not in the wheel either, which is why streamlit is a
**dev dependency** rather than an optional extra: an extra would have been
installable by someone who then had no app to run. Approvals default to **on**
here: the CLI leaves `--approve` off because each prompt costs you a context
switch, and in a UI it costs one click.

The application id is validated before it is joined onto a path
(`config.application_dir`). It names a directory and the app writes your upload
into it, so an id like `../..` or an absolute path would otherwise read and
write anywhere on disk — `Path("applications") / "/etc/x"` is just `/etc/x`.

Appearance is entirely `.streamlit/config.toml` (a neutral zinc theme, light and
dark). The mode follows your desktop and is overridable from the settings menu.
A single `[theme]` block *locks* an app to one mode rather than merely defaulting
to it, which is why both halves are spelled out. The switch appears as soon as
*either* half exists, though, so deleting `[theme.dark]` does not undo the split
— it leaves the switch in place backed by Streamlit's stock dark palette instead
of these surfaces. Rolling back means folding `[theme.light]` into `[theme]` and
dropping both dark sections. `tests/test_theme_config.py` pins this and the rest
of the theme's silent failures.

The app injects no CSS and should not start: theme tokens are the supported way
to restyle Streamlit, and `st.markdown(..., unsafe_allow_html=True)` styling
breaks silently across upgrades when the class names it targets change.

## Finding opportunities

```bash
uv run grant-writer discover --scan-id rural-health-2026 \
    --focus "afterschool STEM in rural districts" --agencies "USDA|NSF"
```

Output lands in `opportunities/rural-health-2026/` — the full text of each
candidate under `candidates/`, one fit-scoring file per candidate under
`scored/` — and the ranked shortlist prints when the run ends.

grants.gov needs no API key, so `discover --no-search` still covers US federal
opportunities; web search is what adds private foundations, state agencies, and
non-US funders.

**The agent never states a score.** It answers six criteria with one of four
verdict words, each carrying a quoted citation from either the opportunity text
or your profile. The weights live in `opportunities.py` and are never shown to
the model, so the percentage is computed from the verdicts rather than asserted
— a fabricated total is not caught here, it is unrepresentable. Anything the
profile does not answer becomes `[NEEDS INPUT: …]` rather than a guess, and a
candidate whose file cannot be parsed reads as **unscored**, never as 0%: those
are opposite claims, and only one of them means the opportunity was judged.

Eligibility is gating — a `NONE` there marks the candidate ineligible — but it
does not zero the score, because a surprising ineligibility call is exactly the
one worth checking, and the evidence for it should still be on screen.

## Architecture

Two graphs over shared infrastructure. Discovery is separate rather than a mode
of the drafting orchestrator because nothing under `/opportunities/` is
submission-bound: it never gets an `interrupt` permission, so a scan
structurally *cannot* pause for approval, and neither frontend needs an
approval path for it.

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

### Where state lives

| Path | Lifetime | Purpose |
|---|---|---|
| `memories/org/AGENTS.md` | Forever, every application | Org identity — loaded into context every turn |
| `skills/*/SKILL.md` | Forever, loaded on demand | Section-specific drafting craft |
| `applications/<id>/` | One application | RFP text, requirements, research, drafts, reviews |

The split matters for context budget. Org identity is compact and always
relevant, so it belongs in memory. A funder's formatting rules are thousands of
tokens that matter for one section, so they belong in a skill that loads only
when needed.

### Backends

`--profile local` (default) uses `FilesystemBackend` rooted at the repo. On a
laptop the filesystem *is* the cross-session memory — drafts are real files you
can open in any editor, and they survive restarts for free.

`--profile server` keeps drafts in graph state and routes `/skills/` and
`/memories/` to a `Store` that is seeded from disk at startup
(`seed_store_from_disk`), so the drafter still gets its section guides and the
org profile. Each route lives in its own Store namespace with prefix-stripped
keys, because `CompositeBackend` strips the route prefix before delegating.
**Never use `local` inside a web server.**

The local CLI persists its checkpoint to SQLite (`.grant_writer/`); the server
profile's `InMemoryStore` still lives only for the process, so swap it for
`PostgresStore` before deploying, or seeded skills/memory die on restart.

### Running outside the repo

Paths resolve relative to the project root — the directory holding `skills/`,
`memories/`, and `applications/`. In a checkout that is found automatically; if
you install the console script elsewhere (`pipx`, `uv tool install`), point it
at your content with `GRANT_WRITER_ROOT=/path/to/project`.

### Permissions

The drafting agent may write only to `/applications/`, `/memories/`, and
`/opportunities/` — the set is named once, as `config.CONTENT_DIRS`, and both
enforcement points read it. Everything else, including its own `skills/` and
this source tree, is read-only. Rules are evaluated **first-match-wins**, so
specific allows must precede the catch-all deny — see `backends.py`.

`--approve` adds an `interrupt` rule on `/applications/*/final/**`. That is
deliberately narrower than interrupting every write: gate the submission-bound
files and you read each prompt, gate every scratch note and you learn to
rubber-stamp.

The discovery graph gets a **narrower set of its own** (`discovery_permissions`)
which returns no interrupt rule at any setting and denies `/applications/`
outright. That is what makes "a scan cannot pause for approval" structural, and
both frontends rely on it: neither checks for a pending write before starting a
scan. Sharing the drafting rules looked equivalent and was not — the discovery
orchestrator has `write_file` like any other, so one write under `final/` would
have parked a thread nothing was watching.

Those rules govern writes **through the backend**. The two tools that write to
real disk — `extract_pdf_text` and `fetch_grants_gov_opportunity` — bypass them
entirely, so each carries its own allowed directories rather than the
project-wide set: drafting archives beside the drafts, discovery only into a
scan. Neither is a superset of the other. A tool handed the union can't tell
which graph is calling it, and a `final/` write from the discovery tool would
skip the approval gate the rules exist to enforce.

### Self-grading against review criteria

`RubricMiddleware` is included unconditionally and stays dormant until a
`rubric` is present in invocation state. Pass `--rubric criteria.md` with the
funder's published review criteria and each time the agent would finish, a
grader scores the transcript against them and sends it back if unsatisfied (up
to 3 iterations). The agent is then iterating against the same standard the
reviewers will apply.

## Guardrails

Fabrication is the failure mode that matters here — invented preliminary data,
personnel, or budget figures are misconduct, not a bad draft. So:

- Every prompt forbids inventing facts; unknowns become
  `[NEEDS INPUT: <question>]` and are collected in `review/gaps.md`.
- `measure_text` exists because models cannot count words by inspection, and an
  over-length narrative is rejected without review.
- The compliance reviewer cannot edit what it reviews.

**This drafts proposals; it does not submit them.** Every output needs human
review before it goes to a funder — especially any `[NEEDS INPUT]` marker,
every number, and every citation.

## Development

```bash
uv sync                     # installs the dev group, streamlit included
uv run pytest tests/ -q     # the whole suite: offline, no API calls
uvx ruff check src/ tests/ streamlit_app.py
```

Don't develop with `--no-dev`. The `AppTest` cases in `test_frontends.py` run
the Streamlit script headlessly and `importorskip` out without streamlit, and
ty resolves imports statically — so `tests/` importing streamlit makes the type
check report an extra diagnostic and trip the baseline.

CI runs both on every push and pull request, against Python 3.13 and 3.14, plus
`ruff format --check` and `ty` held at its two-diagnostic baseline. ruff and ty
are version-pinned in the workflow so a new release cannot turn CI red on
unchanged code — bump them there deliberately.

The tests target the failures that are *silent* in a Deep Agents setup: a
subagent that lost its skills (custom subagents do not inherit them), a
permission rule ordered so the deny shadows the allow, or an interrupt
configured without the checkpointer that makes it work. `test_review_fixes.py`
pins the code-review findings — including that server-profile store keys are
prefix-stripped and namespaced, so `ls /skills/` cannot leak memory files.

### Known quirks

- An `execute` tool is advertised to the model but is inert on
  `FilesystemBackend`, which is not a `SandboxBackendProtocol`. It returns a
  clear error rather than running anything, so it is not a permission bypass —
  `test_execute_tool_is_not_a_permission_bypass` guards that assumption.
- `RubricMiddleware` is marked beta upstream; its API may change.
- Adding `LANGSMITH_API_KEY` is strongly recommended. A run that fans out to
  three subagents is close to impossible to debug from stdout alone.
