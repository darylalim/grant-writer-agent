# grant-writer-agent

A grant writing agent built on [Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview).

Give it a solicitation and an organization profile; it extracts the
requirements, researches the funder, plans and drafts each section, audits the
result against the solicitation, and assembles a submission-ready draft.

## Quick start

```bash
uv sync
cp .env.example .env        # add ANTHROPIC_API_KEY, TAVILY_API_KEY
$EDITOR memories/org/AGENTS.md   # fill in your organization
uv run grant-writer draft --app-id nsf-aisl-2026 --rfp ~/Downloads/solicitation.pdf --funder NSF
```

Output lands in `applications/nsf-aisl-2026/`. Resume or refine with the same
id:

```bash
uv run grant-writer chat --app-id nsf-aisl-2026
```

The `--app-id` doubles as the LangGraph `thread_id`, which is what makes a
later session pick up the same plan and history instead of starting cold.

### Options

| Flag | Effect |
|---|---|
| `--rubric FILE` | Grade the draft against the funder's review criteria and iterate until satisfied |
| `--approve` | Require human approval before writing anything under `final/` |
| `--no-search` | Run without web search (no `TAVILY_API_KEY` needed) |
| `--profile server` | Keep drafts in graph state instead of on disk |

## Architecture

```
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

`--profile server` routes `/memories/` to a `Store` and leaves everything else
in graph state, for deployments with no durable disk. **Never use `local`
inside a web server.**

`InMemoryStore` and `InMemorySaver` are wired in `agent.py`; swap them for
`PostgresStore` / `PostgresSaver` before deploying, or memory dies on restart.

### Permissions

The agent may write only to `/applications/` and `/memories/`; everything else,
including its own `skills/` and this source tree, is read-only. Rules are
evaluated **first-match-wins**, so specific allows must precede the catch-all
deny — see `backends.py`.

`--approve` adds an `interrupt` rule on `/applications/*/final/**`. That is
deliberately narrower than interrupting every write: gate the submission-bound
files and you read each prompt, gate every scratch note and you learn to
rubber-stamp.

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
uv run pytest tests/ -q     # 31 wiring tests, no API calls
uvx ruff check src/ tests/
```

The tests target the failures that are *silent* in a Deep Agents setup: a
subagent that lost its skills (custom subagents do not inherit them), a
permission rule ordered so the deny shadows the allow, or an interrupt
configured without the checkpointer that makes it work.

### Known quirks

- An `execute` tool is advertised to the model but is inert on
  `FilesystemBackend`, which is not a `SandboxBackendProtocol`. It returns a
  clear error rather than running anything, so it is not a permission bypass —
  `test_execute_tool_is_not_a_permission_bypass` guards that assumption.
- `RubricMiddleware` is marked beta upstream; its API may change.
- Adding `LANGSMITH_API_KEY` is strongly recommended. A run that fans out to
  three subagents is close to impossible to debug from stdout alone.
