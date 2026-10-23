# Grant Writer Agent

[![CI](https://github.com/darylalim/grant-writer-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/darylalim/grant-writer-agent/actions/workflows/ci.yml)

An AI grant-writing agent built on LangChain
[Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview). It finds
funding opportunities, fit-scores them with quoted evidence, and drafts proposals
with human approval of final files. Where a fact is unknown, it marks the gap for
you to fill instead of inventing data.

- **`discover`** searches grants.gov and the web for funding opportunities and
  scores how well each one fits your organization.
- **`draft`** turns a solicitation into a proposal: it extracts the
  requirements, researches the funder, drafts and audits each section, and
  assembles the result.

> **This drafts proposals; it does not submit them.** Every output needs human
> review before it goes to a funder. See [Guardrails](#guardrails).

## Quick start

Needs **uv 0.13.4** exactly: `pyproject.toml` pins it with `required-version`,
so any other uv refuses to run here. `uv self update 0.13.4` installs it.

```bash
uv sync
cp .env.example .env             # add ANTHROPIC_API_KEY and TAVILY_API_KEY
mkdir -p memories/org && cp templates/org-profile.md memories/org/AGENTS.md
$EDITOR memories/org/AGENTS.md   # describe your organization

# 1. Find opportunities worth applying to
uv run grant-writer discover --scan-id rural-health-2026 --focus "rural health education"

# 2. Draft a proposal
uv run grant-writer draft --app-id nsf-aisl-2026 --rfp ~/Downloads/solicitation.pdf --funder NSF

# 3. Refine it later
uv run grant-writer chat --app-id nsf-aisl-2026
```

The profile isn't tracked by git, because it holds your organization's data.
Skip the `cp` and the first run creates it from the template. That never
overwrites a profile that already exists.

> **Upgrading from 0.5.0 or earlier?** Back up `memories/org/AGENTS.md` before
> you pull. It used to be a tracked file, and the commit that untracks it
> deletes your copy. If you've edited it, git refuses the pull instead, and the
> usual way out, `git stash` or `git checkout -- memories/`, can throw your
> edits away. Copy the file aside, pull, then copy it back.

## Usage

### Commands

| Command | What it does |
|---|---|
| `discover --scan-id ID` | Search, triage, and score candidates; print a ranked shortlist |
| `draft --app-id ID` | Extract requirements, research, draft, audit, and assemble |
| `chat --app-id ID` | Resume an application's thread to refine it |
| `db-setup` | Create or migrate the Postgres tables named by `DATABASE_URL` (deployments only; bills nothing) |

State is saved to `.grant_writer/checkpoints.sqlite`, so running `chat` or
`draft` again with the same ID resumes the same plan and history. You can reuse
one name for a scan and an application; they are kept on separate threads.

### Flags

Flags go **after** the subcommand, e.g. `grant-writer draft --app-id X --approve`.

| Flag | Commands | Effect |
|---|---|---|
| `--focus TEXT` | `discover` | What to search for |
| `--agencies CODES` | `discover` | Pipe-separated grants.gov agency codes, e.g. `USDA\|NSF` |
| `--rfp FILE` | `draft` | The solicitation PDF, extracted to `rfp.md` before the run |
| `--funder NAME` | `draft` | Funder name, e.g. `NSF` |
| `--rubric FILE` | `draft` | Grade the draft against the funder's review criteria and iterate |
| `--notes TEXT` | `discover`, `draft` | Extra context for this run |
| `--approve` | all | Require approval before writing to `final/` (no effect on `discover`) |
| `--no-search` | all | Skip web search; no `TAVILY_API_KEY` needed |
| `--profile server` | all | Keep drafts in graph state instead of on disk |
| `--recursion-limit N` | all | Max graph steps per turn (default: `150`) |
| `--operator` | all | Allow a run while `DATABASE_URL` is set; without it the CLI refuses the shared database (logged) |

### Configuration

Set these in `.env` (see `.env.example`):

| Variable | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Model provider |
| `TAVILY_API_KEY` | Unless `--no-search` | Web search for funder research and non-federal opportunities |
| `LANGSMITH_API_KEY` | Recommended | Tracing; a multi-agent run is hard to debug from stdout alone |
| `GRANT_WRITER_*_MODEL` | No | Override the model for a role (`DRAFTING`, `RESEARCH`, `COMPLIANCE`, `GRADER`, `DISCOVERY`) |
| `GRANT_WRITER_*_EFFORT` | No | Override the reasoning effort for a role, same role names (`low`, `medium`, `high`, `xhigh`, `max`) |
| `GRANT_WRITER_ROOT` | No | Project root, when the console script is installed outside the repo |
| `DATABASE_URL` | Deployments only | Postgres for checkpoints and the `Store`. Unset means SQLite. While set, `draft`/`chat`/`discover` need `--operator` |
| `GRANT_WRITER_DB_POOL_MAX_SIZE` | No | Most Postgres connections per process (default 10) |
| `GRANT_WRITER_DB_POOL_TIMEOUT` | No | Seconds to wait for a free connection before failing (default 30) |

What those overrides replace:

| Role | Does | Default model | Default effort |
|---|---|---|---|
| `DRAFTING` | Orchestrates a draft; writes sections | Claude Opus 5.5 | `high` |
| `RESEARCH` | Researches the funder | Claude Sonnet 5.5 | `high` |
| `COMPLIANCE` | Reviews drafts against the RFP | Claude Sonnet 5.5 | `high` |
| `GRADER` | Scores a draft against `--rubric` | Claude Opus 5.5 | `high` |
| `DISCOVERY` | Orchestrates a scan; scores candidates | Claude Sonnet 5.5 | `medium` |

Only discovery runs at `medium`: it scores each candidate separately, and a person reads the
shortlist before acting on it. Funder research stays at `high` because every section is framed
on it, and it is a small part of a drafting run's cost.

## Finding opportunities

```bash
uv run grant-writer discover --scan-id rural-health-2026 \
    --focus "afterschool STEM in rural districts" --agencies "USDA|NSF"
```

Results are written to `opportunities/<scan-id>/`, and the ranked shortlist is
printed when the run ends.

| Path | Contents |
|---|---|
| `candidates/` | Full text of each candidate opportunity |
| `scored/` | One fit-scoring file per candidate |

grants.gov needs no API key, so `--no-search` still finds US federal
opportunities. Web search adds private foundations, state agencies, and non-US
funders.

### How scoring works

The agent rates each candidate on six criteria: eligibility, mission alignment,
program fit, track record, award size fit, and timeline feasibility. Each rating
is a verdict word (`STRONG` to `NONE`) backed by a quoted citation. The weights
live in `opportunities.py` and are never shown to the model, so the fit
percentage is computed from the verdicts, never asserted by the model.

- **Unknowns** become `[NEEDS INPUT: …]`, never a guess.
- **Unparseable** candidates show as *unscored*, not 0%.
- **Ineligible** candidates (`NONE` on eligibility) sink below the scored ones
  but keep their score and evidence, so the call can be checked.
- **Citations** are checked against the file they name. Any that can't be found
  there are flagged. The check ignores line wrapping, emphasis, and case.

## Drafting a proposal

```bash
uv run grant-writer draft --app-id nsf-aisl-2026 \
    --rfp ~/Downloads/solicitation.pdf --funder NSF --rubric criteria.md
```

Results are written to `applications/<app-id>/`:

| Path | Contents |
|---|---|
| `rfp.md` | Extracted solicitation text |
| `requirements.md` | Every required section, limit, review criterion, and deadline, with citations |
| `research/` | Funder priorities, recent awards, and program language |
| `sections/` | One file per narrative section; all revision happens here |
| `review/` | Compliance reports and `gaps.md`, which collects every `[NEEDS INPUT]` question |
| `final/` | Assembled submission-ready text, written once per file |

With `--rubric`, a grader scores the draft against the funder's published review
criteria each time the agent finishes, and sends it back for revision if it
falls short (up to 3 iterations).

## Web UI

An optional Streamlit front end runs both workflows on one page:

```bash
uv run streamlit run streamlit_app.py
```

It shares the CLI's checkpoint, so a run started in the browser can continue
with `grant-writer chat` and the other way round. Compared with the CLI, it adds:

- **Approval previews**: the full content of each `final/` write, not just its
  path. Approval is on by default.
- **Gap count**: how many `[NEEDS INPUT]` markers remain across the drafts.
- **Shortlist detail**: each candidate's six verdicts and citations, with the
  source text one click away.
- **File browser**: shows source by default, with a toggle for rendered markdown.
  Files under `final/` always open as source, so what you approve is exactly what
  gets written.

The follow-up chat box is disabled while a turn is running or waiting for
approval. Sending a message in either state would discard the write waiting
for approval.

The theme (`.streamlit/config.toml`) follows the desktop's light or dark mode, and
the settings menu can override it. Text, links, input outlines and focused
inputs meet WCAG AA contrast in both modes, and `tests/test_theme_config.py`
recomputes every figure. One known exception: Streamlit draws a focused
button's ring at half opacity, which reaches only 2.5:1 (light) and 1.8:1
(dark). A focused button also darkens, so the focus is still visible.

## Architecture

Two agent graphs share one set of infrastructure:

```
discovery (sonnet)        searches grants.gov + web, triages, delegates scoring
└── opportunity-scout     (sonnet, write-limited)  scores one candidate per call

orchestrator (opus)       reads RFP, extracts requirements, plans, delegates, assembles
├── funder-researcher     (sonnet + web search)    what this funder actually rewards
├── section-drafter       (opus + skills)          one section per call
└── compliance-checker    (sonnet, write-limited)  audits drafts, cannot edit them
```

Each subagent works in its own context. Research produces search results the
drafter doesn't need, drafting needs the style guides loaded, and compliance
judges the drafts without seeing the drafter's reasoning.

### Where state lives

| Path | Lifetime | Purpose |
|---|---|---|
| `memories/org/AGENTS.md` | Permanent, not tracked | Organization profile, loaded every turn. Created from the template on first run |
| `templates/org-profile.md` | Tracked | The blank profile that ships. No agent can write it |
| `skills/*/SKILL.md` | Permanent | Section drafting guides, loaded on demand |
| `opportunities/<scan-id>/` | One scan | Candidate texts and scores |
| `applications/<app-id>/` | One application | RFP, requirements, research, drafts, reviews |
| `.grant_writer/checkpoints.sqlite` | Until deleted | Conversation state, plans, and writes awaiting approval |

Back up the checkpoint file before upgrading dependencies. It is the one piece
of state a library upgrade can make unreadable. Close the CLI and the UI first,
and use SQLite's own backup rather than `cp`, which can miss rows still in the
write-ahead log:

```bash
sqlite3 .grant_writer/checkpoints.sqlite ".backup .grant_writer/checkpoints.pre-upgrade.sqlite"
```

### Backends

| Profile | Drafts | Skills and memory | Use for |
|---|---|---|---|
| `local` (default) | Real files on disk | Real files on disk | CLI and local UI |
| `server` | Graph state | `Store` seeded from disk at startup | Web deployments |

Never use `local` inside a web server. Without `DATABASE_URL`, the `server`
profile's Store is in memory and is lost when the process exits. With it set,
checkpoints and the Store both live in Postgres; run `grant-writer db-setup`
once per deploy, before the app starts, to create or migrate the tables.

On every start, skills are refreshed from disk, while the organization profile
is only seeded when the Store has none, so edits made through the app survive
a restart.

**SQLite history does not move to Postgres.** No tool copies checkpoints from
one to the other, so threads started locally stay local and a deployment starts
with none.

### Permissions

- **Drafting** can write only to `/applications/`, `/memories/`, and
  `/opportunities/`. Everything else, including `skills/` and the source tree,
  is read-only. Rules are first-match-wins, so specific allows must come before
  the catch-all deny (see `backends.py`).
- **`--approve`** pauses on writes to `/applications/*/final/**` and on any
  delete under `/applications/`. It doesn't pause on every write, because
  constant prompts get approved without being read.
- **Deletes** follow the write rules: a file the agent can write, it can delete.
  It can never delete a directory.
- **Discovery** cannot write to `/applications/` and never pauses for approval.
- **The organization profile** (`/memories/`) is shared by every user on the
  `server` profile, so there every drafting write to it pauses for approval,
  whether or not `--approve` is on, and discovery can't write it at all.
  On `local` it stays writable without a pause.
- **The real-disk tool** (`fetch_grants_gov_opportunity`) bypasses these
  rules, so it limits itself to the folder its own graph may write to.
- **Solicitations** are read before the run, never by the agent: the CLI and
  the UI extract the PDF's text (up to 50 MB and 500 pages, checked by its
  `%PDF-` signature, in a child process stopped after two minutes) and put it
  at `rfp.md`. The agent has no tool that opens a PDF or any other path on
  disk, and the UI keeps no copy of the uploaded file.

## Guardrails

Invented preliminary data, personnel, or budget figures are misconduct, not
just a bad draft. So:

- Prompts forbid inventing facts. Unknowns become `[NEEDS INPUT: <question>]`
  and are collected in `review/gaps.md`.
- Lengths come from the `measure_text` tool, run on the saved file after its last edit, never
  from the model's own estimate.
- The compliance reviewer cannot edit what it reviews.
- The scout must cite every verdict; an honest `NONE` beats a hopeful `STRONG`.
- A turn declined by the model's safety classifier is shown as declined, with
  the agent and the classifier's category, not as an empty reply. Health and
  biomedical topics can trigger false refusals; if one does, rephrase the
  request or retry later. There is no automatic fallback to another model.

Before submitting, check every `[NEEDS INPUT]` marker, every number, and every
citation.

## Development

```bash
uv sync                                              # includes the dev group (streamlit)
uv run pytest tests/ -q                              # offline, no API calls
uvx ruff@0.17.1 check src/ tests/ evals/ streamlit_app.py
uvx ruff@0.17.1 format --check src/ tests/ evals/ streamlit_app.py
uvx ty@0.0.89 check src/ tests/ evals/ streamlit_app.py   # 1 upstream diagnostic expected
uv run python -m evals.run_scout                     # prompt eval: live model, costs money
```

- **Don't use `--no-dev`.** The Streamlit `AppTest` cases would be skipped and
  the type check would report extra diagnostics.
- **Tests** cover wiring failures that raise no error: a subagent missing its
  skills, a misordered permission rule, or an approval step with no checkpointer.
  See the invariants in `CLAUDE.md`.
- **Evals** in `evals/` are not part of the suite. The tests can't check prompt
  quality, so run the evals by hand after editing a prompt. See `evals/README.md`.
- **CI** runs on Python 3.13 and 3.14, with pinned `ruff` and `ty` versions.
  The Claude Code hooks pin the same versions, so a local edit is formatted
  exactly as CI checks it.
- **uv is pinned too**, to 0.13.4 by `required-version` in `pyproject.toml`.
  CI reads the same setting, so local and CI builds run the same uv, and it
  moves together with the `uv_build` bound.
- **CI audits `uv.lock`** with `uv audit` and fails on any advisory not
  accepted by ID in `ci.yml`, with the reason next to it. Dependabot keeps the
  GitHub Actions current; Python dependencies are upgraded by hand.
- **Billed commands ask first in Claude Code.** `draft`, `discover`, `chat` and
  the live evals, plus any `rm` or `mv` of the checkpoint file, need a
  confirmation before they run.
- **Releases** are automatic: when a green push to `main` has a `version` in
  `pyproject.toml` with no `v<version>` tag yet, CI tags it and publishes the
  wheel and sdist. Bumping the version is all a release takes.

### Known quirks

- The model sees an `execute` tool, but it does nothing on `FilesystemBackend`:
  it returns an error without running anything.
  `test_execute_tool_is_not_a_permission_bypass` guards this.
- `RubricMiddleware` is beta upstream; its API may change.

## License

MIT. See [LICENSE](LICENSE).
