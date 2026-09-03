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
```

**These call a real model and cost real money.** They are not collected by
`pytest tests/` and do not run in CI — deliberately, and the reason is the same
one `tests/conftest.py` encodes: the suite is offline by contract, CI configures
no secrets, and a test that needs a live credential is a bug in the suite.

## What is here

| File | |
|---|---|
| `scout_cases.py` | Four fixtures and what a correct answer to each looks like. Pure data. |
| `scorers.py` | Seven programmatic scorers and one LLM judge. Pure. |
| `run_scout.py` | The runner. The only file that makes a network call. |
| `../tests/test_evals.py` | Offline tests **of the scorers**, run on every push. |

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

That feedback is the only write this directory makes to a LangSmith workspace, it
happens only when tracing is already on, and a failure to post is printed and
ignored — a measurement, never a gate. Nothing here creates a LangSmith dataset;
promoting these cases to a managed one with `langsmith.evaluate` is a deliberate
next step, not something a run does behind you. If it ever happens,
`scout_cases.py` stays the source of truth and the dataset is a push-only mirror
of it — two editable copies of a fixture set is the drift this repo keeps writing
tests against.
