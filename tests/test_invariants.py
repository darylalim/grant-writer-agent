"""The guard on the invariant list itself.

`CLAUDE.md` catalogues the failures that are *silent* -- each still imports,
still runs, and still produces plausible output when broken. That catalogue is
the reason `tests/` exists. Nothing, until this file, guarded the catalogue.

It can rot in two directions, and both are the same kind of quiet the list is
about:

- An entry is added to `CLAUDE.md` and the test is never written. The document
  now promises a guarantee nothing checks, and reads exactly like the sixteen
  beside it that are real.
- A test is renamed or deleted in a refactor while the numbered entry stays.
  Same end state, reached from the other side. Green suite either way.

So each pinning test declares what it pins, on its own line in its own
docstring::

    Pins invariant 8.
    Pins invariants 3 and 17.

and this file checks the two directions against each other: every documented
number has a test claiming it, and every claim names a number that still
exists. The declaration lives on the test rather than in a registry here on
purpose -- a registry is a third place to forget, and deleting the test has to
delete the claim with it.

**What this does not do.** A marker is a claim, not a proof: a test can say
`Pins invariant 8.` and check something else entirely, and nothing here would
notice. It closes the gap where *no* test claims an invariant at all, which is
the one that arises by omission rather than by intent. Read the marker as a
pointer to where the argument lives, not as evidence the argument is right.

The parse is deliberately anchored to the exact heading and the exact
`N. **...**` entry shape `CLAUDE.md` uses. Reformatting that section is
allowed; doing it without updating `_SECTION_HEADING` or `_ENTRY_RE` fails
`test_the_invariant_section_is_still_parseable` rather than silently finding
nothing and passing everything.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path

from grant_writer.config import PROJECT_ROOT

CLAUDE_MD = Path(PROJECT_ROOT, "CLAUDE.md")
TESTS_DIR = Path(__file__).parent

_SECTION_HEADING = "## Invariants that fail silently"

# The numbered entries in that section: `1. **Permission rules are ...**`.
_ENTRY_RE = re.compile(r"^(\d+)\. \*\*", re.MULTILINE)

# The marker, at the start of a line in a test's docstring. Capitalised and
# anchored so it cannot be satisfied by prose -- several docstrings already
# discuss "invariant 11" while pinning something else, and those must not read
# as claims.
_PIN_RE = re.compile(
    r"^Pins invariants?\s+(?P<numbers>\d+(?:\s*(?:,|and)\s*\d+)*)\s*\.",
    re.MULTILINE,
)


def _declared_invariants() -> list[int]:
    """The invariant numbers `CLAUDE.md` documents, in the order it lists them.

    Returns `[]` if the heading is gone, which is a distinct failure from "the
    list is empty" and is reported as one by the parseability test below.
    """
    text = CLAUDE_MD.read_text(encoding="utf-8")
    start = text.find(_SECTION_HEADING)
    if start == -1:
        return []
    rest = text[start + len(_SECTION_HEADING) :]
    end = rest.find("\n## ")
    section = rest if end == -1 else rest[:end]
    return [int(match.group(1)) for match in _ENTRY_RE.finditer(section)]


def _pins_by_invariant() -> dict[int, list[str]]:
    """Map invariant number -> the tests declaring they pin it.

    Walks the AST rather than grepping so a marker only counts when it is the
    docstring of an actual `test_` function. A marker in a module docstring or
    a comment does not survive the test being deleted, which is precisely the
    rot this file exists to catch.
    """
    found: dict[int, list[str]] = defaultdict(list)
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not node.name.startswith("test_"):
                continue
            docstring = ast.get_docstring(node) or ""
            for match in _PIN_RE.finditer(docstring):
                for number in re.findall(r"\d+", match.group("numbers")):
                    found[int(number)].append(f"{path.name}::{node.name}")
    return dict(found)


def test_the_invariant_section_is_still_parseable():
    """Without this, every other test in this file passes vacuously.

    A renamed heading or a reformatted entry makes `_declared_invariants`
    return `[]`, and an empty set is trivially covered -- the guard on the
    silent-failure list would itself have failed silently.
    """
    declared = _declared_invariants()
    assert declared, (
        f"Found no numbered invariants under {_SECTION_HEADING!r} in "
        f"{CLAUDE_MD}. Either the heading moved or the `N. **...**` entry "
        f"shape changed; update _SECTION_HEADING / _ENTRY_RE to match."
    )
    assert declared == list(range(1, len(declared) + 1)), (
        f"Invariant numbers must run 1..N with no gaps, duplicates, or "
        f"reordering -- they are cited by number from {len(_pins_by_invariant())} "
        f"test docstrings and from comments throughout src/. Found: {declared}"
    )


def test_every_documented_invariant_is_pinned_by_a_named_test():
    """An entry with no test reads exactly like an entry with one."""
    declared = set(_declared_invariants())
    missing = sorted(declared - set(_pins_by_invariant()))
    assert not missing, (
        "CLAUDE.md documents invariant(s) "
        + ", ".join(str(n) for n in missing)
        + " that no test claims to pin. Write the test and give its docstring "
        "a `Pins invariant N.` line. If it genuinely cannot be checked offline "
        "-- a prompt's wording, say -- pin the nearest structural consequence "
        "and say in the CLAUDE.md entry what is left unchecked."
    )


def test_every_pin_names_an_invariant_that_still_exists():
    """The other direction: a claim outliving the entry it refers to.

    Renumbering the list, or deleting an entry, leaves markers pointing at
    numbers that mean something else now or nothing at all -- and a marker is
    read by whoever next has to decide whether a change is safe.
    """
    declared = set(_declared_invariants())
    orphaned = {
        number: tests
        for number, tests in sorted(_pins_by_invariant().items())
        if number not in declared
    }
    assert not orphaned, (
        "These tests pin invariant numbers CLAUDE.md no longer documents: "
        + "; ".join(f"{n} ({', '.join(tests)})" for n, tests in orphaned.items())
        + ". If the list was renumbered, update the markers with it; if the "
        "invariant is genuinely gone, delete the test rather than leaving it "
        "pinning nothing."
    )
