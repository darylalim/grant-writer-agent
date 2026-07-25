# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                                       # install (Python >=3.13, uv_build backend)
uv run pytest tests/ -q                       # full suite: offline, no API calls
uv run pytest tests/test_wiring.py::test_subagent_roster -q   # single test
uv run pytest tests/ -q -k permission         # by keyword
uvx ruff check src/ tests/                    # lint (select list in [tool.ruff.lint])
uvx ruff format --check src/ tests/           # formatting is ruff's default 88-col

uv run grant-writer draft --app-id X --rfp path.pdf --funder NSF
uv run grant-writer chat --app-id X           # resume the same thread
```

Common flags (`--profile`, `--approve`, `--no-search`, `--recursion-limit`) live on a shared
parent parser and must be passed **after** the subcommand. `--app-id` is also the LangGraph
`thread_id`; the CLI persists to `.grant_writer/checkpoints.sqlite`, so state resumes across
processes. `applications/` and `.grant_writer/` are gitignored.

## Architecture

A [Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview) orchestrator plus three
subagents. Module dependencies flow one way:

```
config.py  →  tools.py, prompts.py  →  backends.py, subagents.py  →  agent.py  →  cli.py
```

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
2. **`extract_pdf_text` bypasses `FilesystemPermission` entirely** — it writes to the real
   filesystem, not through the backend. `tools.py::_resolve_output_path` re-implements the same
   boundary and must be kept in sync with `build_permissions`, including resolving `..` *before*
   the prefix check.
3. **Custom subagents do not inherit `skills` from the parent.** `subagents.py` passes
   `"skills": [SKILLS_DIR]` explicitly to `section-drafter` and `compliance-checker`. Omit it and
   the drafter still answers — just generically.
4. **`--approve` needs a checkpointer.** Interrupts are a no-op without one; `_build_checkpointer`
   returns SQLite when `Settings.checkpoint_db` is set, in-memory otherwise (tests, library use).
5. **Approval decisions are per action request, not per interrupt.** `HumanInTheLoopMiddleware`
   bundles a turn's interrupted calls into one interrupt carrying N `action_requests`, and resume
   requires exactly N decisions — see `cli._pending_action_requests`.
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

## Backend profiles

`local` (default) roots a `FilesystemBackend` at the project with `virtual_mode=True` — real files
you can open in an editor. `server` puts drafts in `StateBackend` (ephemeral, thread-scoped) and
routes `/skills/` + `/memories/` to a `Store` seeded by `seed_store_from_disk` at startup.
**Never use `local` inside a web server.** The server profile's `InMemoryStore` dies with the
process — swap for `PostgresStore` before deploying.

## Testing conventions

`tests/conftest.py` sets a dummy `ANTHROPIC_API_KEY`, forces `LANGSMITH_TRACING=false`, and pops
`TAVILY_API_KEY`, so nothing hits the network. Tests assert on wiring by reaching into deepagents
internals — `_check_fs_permission`, `supports_execution`,
`graph.nodes["tools"].bound.tools_by_name`, `get_graph().nodes`. That is intentional (it is the only
way to catch these failures offline) but brittle: a `deepagents` upgrade may need these updated.
`test_wiring.py` covers structural invariants; `test_review_fixes.py` pins specific past code-review
findings and should gain a case whenever a review turns one up.

## Domain rules baked into the prompts

Fabricated preliminary data, personnel, or budget figures are misconduct, not a bad draft. When
editing prompts or skills, preserve: unknowns become `[NEEDS INPUT: <question>]` and collect in
`review/gaps.md`; lengths come from `measure_text`, never from eyeballing; the compliance reviewer
writes only to `/applications/*/review/` and cannot edit what it reviews; `final/` files are written
once as a complete `write_file`, never built up by `edit_file` (each write there may be a separate
human approval).

An `execute` tool is advertised to the model but inert on `FilesystemBackend` (not a
`SandboxBackendProtocol`). `test_execute_tool_is_not_a_permission_bypass` guards that — if it ever
becomes live, the write permissions stop being a real boundary.
