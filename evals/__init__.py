"""Prompt evals.

Deliberately outside `src/` and outside `tests/`.

Not in `src/` because nothing here ships in the wheel -- these are development
instruments, like the tests, and the installed console script has no use for
them.

Not in `tests/` because the suite is offline by contract: `tests/conftest.py`
blanks the credentials, CI configures no secrets, and CLAUDE.md is explicit that
a test needing a real key is a bug in the suite. These need a real key, so they
must not be collectable by `pytest tests/`.

Which key depends on the runner, and they are not interchangeable: `run_scout`
needs `ANTHROPIC_API_KEY` and spends money per run, while `push_dataset` needs
`LANGSMITH_API_KEY` and spends none but writes to a workspace. Its `--out` path
needs neither and reaches no network -- that is the one way to see what this
directory would push without having an account to push it to.

`evaluate_scout` needs both, being the two halves at once: it calls a model per
row and scores the answers into an experiment on the dataset the mirror pushed.
It checks for both before starting, and in that order -- `evaluate` posts a
project before the first billed call, so a missing workspace credential costs
nothing rather than costing four model calls and then failing to record them.

The one exception is `tests/test_evals.py`, which exercises the *scorers* in
this package against canned model output, the mirror's reconciliation against a
fetch it fakes, and the evaluate harness against a row and a run it builds by
hand. That stays offline, and it exists because an eval whose scoring is wrong
reports a prompt regression as green -- which is worse than having no eval at
all.
"""
