"""Prompt evals.

Deliberately outside `src/` and outside `tests/`.

Not in `src/` because nothing here ships in the wheel -- these are development
instruments, like the tests, and the installed console script has no use for
them.

Not in `tests/` because the suite is offline by contract: `tests/conftest.py`
blanks the credentials, CI configures no secrets, and CLAUDE.md is explicit that
a test needing a real key is a bug in the suite. These need a real key and cost
real money on every run, so they must not be collectable by `pytest tests/`.

The one exception is `tests/test_evals.py`, which exercises the *scorers* in
this package against canned model output. That stays offline, and it exists
because an eval whose scoring is wrong reports a prompt regression as green --
which is worse than having no eval at all.
"""
