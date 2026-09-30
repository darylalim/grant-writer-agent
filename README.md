# Grant Writer Agent

[![CI](https://github.com/darylalim/grant-writer-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/darylalim/grant-writer-agent/actions/workflows/ci.yml)

A grant writing agent built on [Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview).

- **`discover`** searches grants.gov and the web for funding opportunities and
  scores how well each one fits your organization.
- **`draft`** turns a solicitation into a proposal: it extracts the
  requirements, researches the funder, drafts and audits each section, and
  assembles the result.

> **This drafts proposals; it does not submit them.** Every output needs human
> review before it goes to a funder. See [Guardrails](#guardrails).

## Quick start

```bash
uv sync
cp .env.example .env             # add ANTHROPIC_API_KEY and TAVILY_API_KEY
$EDITOR memories/org/AGENTS.md   # describe your organization

# 1. Find opportunities worth applying to
uv run grant-writer discover --scan-id rural-health-2026 --focus "rural health education"

# 2. Draft a proposal
uv run grant-writer draft --app-id nsf-aisl-2026 --rfp ~/Downloads/solicitation.pdf --funder NSF

# 3. Refine it later
uv run grant-writer chat --app-id nsf-aisl-2026
```

## Usage

### Commands

| Command | What it does |
|---|---|
| `discover --scan-id ID` | Search, triage, and score candidates; print a ranked shortlist |
| `draft --app-id ID` | Extract requirements, research, draft, audit, and assemble |
| `chat --app-id ID` | Resume an application's thread to refine it |

State is saved to `.grant_writer/checkpoints.sqlite`, so running `chat` or
`draft` again with the same ID resumes the same plan and history. You can reuse
one name for a scan and an application; they are kept on separate threads.

### Flags

Flags go **after** the subcommand, e.g. `grant-writer draft --app-id X --approve`.

| Flag | Commands | Effect |
|---|---|---|
| `--focus TEXT` | `discover` | What to search for |
| `--agencies CODES` | `discover` | Pipe-separated grants.gov agency codes, e.g. `USDA\|NSF` |
| `--rfp FILE` | `draft` | The solicitation PDF |
| `--funder NAME` | `draft` | Funder name, e.g. `NSF` |
| `--rubric FILE` | `draft` | Grade the draft against the funder's review criteria and iterate |
| `--notes TEXT` | `discover`, `draft` | Extra context for this run |
| `--approve` | all | Require approval before writing to `final/` (no effect on `discover`) |
| `--no-search` | all | Skip web search; no `TAVILY_API_KEY` needed |
| `--profile server` | all | Keep drafts in graph state instead of on disk |
| `--recursion-limit N` | all | Max graph steps per turn (default: `150`) |

### Configuration

Set these in `.env` (see `.env.example`):

| Variable | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Model provider |
| `TAVILY_API_KEY` | Unless `--no-search` | Web search for funder research and non-federal opportunities |
| `LANGSMITH_API_KEY` | Recommended | Tracing; a multi-agent run is hard to debug from stdout alone |
| `GRANT_WRITER_*_MODEL` | No | Override the model for a role (`DRAFTING`, `RESEARCH`, `COMPLIANCE`, `GRADER`, `DISCOVERY`) |
| `GRANT_WRITER_ROOT` | No | Project root, when the console script is installed outside the repo |

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
| `memories/org/AGENTS.md` | Permanent | Organization profile, loaded every turn |
| `skills/*/SKILL.md` | Permanent | Section drafting guides, loaded on demand |
| `opportunities/<scan-id>/` | One scan | Candidate texts and scores |
| `applications/<app-id>/` | One application | RFP, requirements, research, drafts, reviews |

### Backends

| Profile | Drafts | Skills and memory | Use for |
|---|---|---|---|
| `local` (default) | Real files on disk | Real files on disk | CLI and local UI |
| `server` | Graph state | `Store` seeded from disk at startup | Web deployments |

Never use `local` inside a web server. The `server` profile's `InMemoryStore` is
lost when the process exits; replace it with `PostgresStore` before deploying.

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
- **Real-disk tools** (`extract_pdf_text`, `fetch_grants_gov_opportunity`)
  bypass these rules, so each one limits itself to the folders its own graph
  may write to.

## Guardrails

Invented preliminary data, personnel, or budget figures are misconduct, not
just a bad draft. So:

- Prompts forbid inventing facts. Unknowns become `[NEEDS INPUT: <question>]`
  and are collected in `review/gaps.md`.
- Lengths come from the `measure_text` tool, never from the model's own estimate.
- The compliance reviewer cannot edit what it reviews.
- The scout must cite every verdict; an honest `NONE` beats a hopeful `STRONG`.

Before submitting, check every `[NEEDS INPUT]` marker, every number, and every
citation.

## Development

```bash
uv sync                                              # includes the dev group (streamlit)
uv run pytest tests/ -q                              # offline, no API calls
uvx ruff check src/ tests/ evals/ streamlit_app.py
uvx ruff format --check src/ tests/ evals/ streamlit_app.py
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
