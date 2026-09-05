# Prompt evals

The suite in `tests/` covers wiring. It cannot cover the prompts, and the
prompts are the product — the anti-fabrication rules, `[NEEDS INPUT]` over
invention, an honest `NONE` over a hopeful `STRONG`. A prompt edit that weakens
any of those passes every test, every hook, and CI.

That is what this directory is for.

```bash
uv run python -m evals.run_scout                  # all cases, with the judge
uv run python -m evals.run_scout --no-judge       # programmatic scorers only
uv run python -m evals.run_scout --case leaky-brief
uv run python -m evals.run_scout --out results.json

uv run python -m evals.push_dataset --dry-run     # the plan only; creates nothing
uv run python -m evals.push_dataset               # push it, then verify the push
uv run python -m evals.push_dataset --out rows.json   # payloads only, no account
```

**`run_scout` calls a real model and costs real money; `push_dataset` writes to
a real workspace.** Neither is collected by `pytest tests/` and neither runs in
CI — deliberately, and the reason is the same one `tests/conftest.py` encodes:
the suite is offline by contract, CI configures no secrets, and a test that
needs a live credential is a bug in the suite.

## What is here

| File | |
|---|---|
| `scout_cases.py` | Four fixtures and what a correct answer to each looks like. Pure data. |
| `scorers.py` | Seven programmatic scorers and one LLM judge. Pure. |
| `run_scout.py` | The runner. Calls a model; needs `ANTHROPIC_API_KEY`. |
| `push_dataset.py` | Mirrors the fixtures into a LangSmith dataset. One direction, verified. Needs `LANGSMITH_API_KEY`, which it loads from the environment file itself. |
| `../tests/test_evals.py` | Offline tests **of the scorers and the mirror**, run on every push. |

That last row is the load-bearing one. An eval whose scoring is wrong reports a
prompt regression as green, with the authority of a number attached — worse than
having no eval. The scorer tests earned their place immediately: they caught a
regex in `states_no_total` where `%\b` could never match, so the one phrasing
the scorer existed to catch ("roughly 80% fit") slipped straight through it.

## Why the scoring is mostly not a model

The scout writes a rigid grammar that `grant_writer.opportunities` already
parses, and `untraceable_citations` already checks a quotation against the text
it claims to come from. So seven of the eight scorers are assertions over parsed
output, and the judge is reserved for the one question code cannot answer:
whether some claim about the organization is grounded in the profile at all.

The ratio is the point. Scoring that is itself a model call inherits that
model's failure modes, and the first confusing result teaches everyone to stop
reading the output.

## Why the cases are shaped this way

Two of the four are refusals — a plainly ineligible candidate, and a profile
with the answers deleted. A scout that answered `NONE` to everything and flagged
every criterion as missing would pass both of them perfectly.

`genuine-fit` is the control that kills that scout. Without it the eval rewards
timidity, which is its own way of costing someone a week: the opportunity they
never heard about. Any suite of refusal cases needs its positive twin, or it
measures caution rather than judgement.

`leaky-brief` probes invariant 16 from the prompt side. The delegation message
carries an award range and a funding priority that appear in neither document —
the way an orchestrator's own web research reaches a scout that cannot search.
`workspace.unverifiable_citations` catches that after the fact, on a real scan.
The question here is whether the prompt stops it happening.

The judge is shown that brief alongside the two files, and told it is an input
rather than a source: the scout may name its claims in order to set them aside,
and may not rely on them. Without the brief in the payload the judge cannot tell
those apart — it reads a correctly-excluded award range as a figure conjured out
of nothing, and the case fails for being right. That is not hypothetical; it is
what the first live run reported.

## Known limitations

**It evaluates the prompt, not the subagent.** The model is handed both
documents inline and asked for the scoring file directly, rather than being run
as a subagent that calls `read_file` twice and `write_file` once. Tool use,
permissions, and the orchestrator's delegation are all out of scope.

That is a deliberate first step rather than an oversight. Isolating the prompt
is what makes a failure readable: run through the full graph, a bad verdict
could come from the prompt, a missed `read_file`, a truncated context, or the
orchestrator's summary — and the run would tell you only that something was
wrong. Running the real graph is the natural next version, once this one passes.

**The drafting side is not covered at all.** `SCOUT_PROMPT` was first because it
is the cheapest component (sonnet, no tools, two reads and a write) and the one
whose output is machine-checkable. `DRAFTER_PROMPT` and `COMPLIANCE_PROMPT` carry
the same anti-fabrication rules over prose, where scoring needs a judge for
nearly everything — a much more expensive eval, and one worth building on top of
a scoring harness that has already been shown to work.

**The verdicts asserted are only the indefensible ones.** Where an honest scout
could reasonably answer two ways, the case asserts nothing (`expect_eligibility
= None`). Over-specifying turns an eval into a test of one person's taste, and
the first failure on a defensible answer is the last time anyone reads it.

## Tracing

If `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` are set — the `.env.example`
default — every call here is traced into `LANGSMITH_PROJECT`. LangChain
instruments itself, so that much was always true. What this directory adds is
the labelling, because unlabelled it was not worth opening.

Each case is one run named `scout:<case key>`, with the scout call and the judge
call as its children and the fixture's key on both. Before that a case was two
anonymous `ChatAnthropic` roots tied neither to each other nor to the fixture
they came from, which is most of why a traced eval run went unread.

Each scorer's verdict is attached to the case run as **feedback**, keyed by the
scorer's name, so what a prompt edit broke is a query rather than a reading
exercise:

```bash
langsmith trace list --project grant-writer \
  --filter 'and(eq(feedback_key, "citations"), eq(feedback_score, 0))'
```

A **skipped** scorer posts nothing at all rather than posting a pass. A case that
declines to assert on a dimension has not passed it, and collapsing those two is
the mistake invariant 14 forbids for `fit_percent` — averaged back, a run that
asserted almost nothing would read like one that asserted everything and was
right.

That feedback is the only write **`run_scout`** makes to a LangSmith workspace, it
happens only when tracing is already on, and a failure to post is printed and
ignored — a measurement, never a gate.

## The dataset mirror

`push_dataset.py` is the other write, and it is nothing like the first one: it
runs only when you run it, it does not care whether tracing is on, and a failure
exits non-zero. A push is an operation on the world, and a failed one is a fact.

```bash
uv run python -m evals.push_dataset --dry-run
```

That dry run creates nothing, and "nothing" has to include the dataset itself.
`main` turns `--create` on for the default name, so the flag documented to write
nothing used to make an empty dataset on a fresh workspace and print its URL
directly above the words "Nothing was written" — and the next real push was then
refused by the ownership check, for adopting a dataset the dry run had made. It
prints `(not created)` instead, which is also what makes it the safe way to
check whether somebody renamed the mirror.

The credential is read from the environment file rather than from the shell
alone: this module imports nothing from `grant_writer`, so unlike `run_scout` it
does not inherit `config.py`'s import-time `load_dotenv()` and calls one itself.
`--out` is the exception and reads no file at all — it needs no credential, and
proving that is what keeps the dump usable on a machine with no LangSmith
account.

`scout_cases.py` stays the source of truth and the dataset is a **push-only
mirror** of it. A hand-edit in the LangSmith UI is drift to overwrite, not a
second opinion to merge, and a row no case claims is deleted — two editable
copies of a fixture set is the drift this repo keeps writing tests against, and
the point of a mirror is that only one end holds a pen.

It proves that rather than promising it. Each case owns a `uuid5` example id
derived from its key, so a push overwrites rows instead of appending them; the
comparison is a digest **recomputed from what the dataset actually holds**,
never a `source_sha` the mirror wrote earlier and would find agreeing with
itself; and after writing, the push re-reads and re-plans. A second plan that is
not empty means the write did not take, and that exits 1 rather than printing a
count nobody checked.

The reconciliation is a pure function of the fixtures and one fetch, so all of
it is pinned offline in `tests/test_evals.py` — including the traps that would
otherwise look like success: a `tuple` that JSON returns as a `list` and so
differs from itself on every push, and the server-maintained `dataset_split` key
that would read as somebody's hand-edit forever.

Splits are the one thing the mirror carries rather than owns. `dataset_split` is
server-maintained, so it is excluded from the digest — counted, every row would
read as drift the moment anyone assigned a split. But an update replaces the
whole metadata document, so the plan copies the fetched row's server keys onto
the row it is about to write. Excluding a key from the comparison and omitting
it from the write are separate decisions, and letting the second follow from the
first destroys the assignment where neither the plan nor the read-back can see
it, since both strip the key on both sides.

Known limits, all in the module docstring: the dataset's own description is
write-once (this SDK has no `update_dataset`), a rename is undetectable and
leaves a stale fork, restoring a previously pruned case key reuses a
soft-deleted id and this SDK does not say what happens then, and pruning
destroys hand-added rows by design — soft delete, with the id and an excerpt
printed *before* the call rather than after it, and `--no-prune` to opt out.

A dataset that already existed and holds no row of the mirror's is refused,
including an empty one: adopting a colleague's freshly created dataset is the
wrong-name accident arriving through the door the ownership check was built to
hold. `--adopt` is the way to say you meant it.
