"""Offline tests for the eval scorers and the dataset mirror.

An eval whose scoring is wrong is worse than no eval: it reports a prompt
regression as green, and it does so with the authority of a number. So the
scorers get the same treatment as the rest of the package -- canned scout output
in, `Score` objects out, no model and no network.

These live in `tests/` while the eval itself lives in `evals/` and is never
collected here, because these cost nothing and must run on every push. The
distinction is the credential: `evals/run_scout.py` needs a real key, and
CLAUDE.md is explicit that a test needing one is a bug in the suite.

`evals/push_dataset.py` gets the same treatment for the same reason, in the
second section below: the network half of a mirror is two reads and three
writes, and everything that *decides* what those are is a pure function of the
fixture set and one fetch. A mirror that is quietly wrong overwrites the source
of truth with a lossy copy of itself and reports success doing it.

`evals/evaluate_scout.py` closes the file, and it is the thinnest of the three
by design: almost all of it is `evaluate()` itself, which builds a client and
posts an experiment. What is left is the part that decides *what gets written
down* -- which rows are scored at all, which verdicts are omitted rather than
posted as passes, and what a broken evaluator says instead of nothing. Every
one of those fails silently, and every one of them is reachable with no
credential.

Each case below is a scout output that is *wrong in one specific way*, checked
against the scorer that has to notice. A scorer is only worth having if it fails
on something, and the cheapest way to be sure of that is to hand it the failure.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import re
import uuid
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from langsmith.evaluation import EvaluationResult
from langsmith.evaluation.evaluator import run_evaluator
from langsmith.run_trees import RunTree
from langsmith.utils import LangSmithConflictError, LangSmithNotFoundError

from evals import evaluate_scout, push_dataset, run_scout
from evals.scorers import (
    JUDGE_PROMPT,
    Score,
    build_judge_payload,
    posting_scores,
    read_judge_verdict,
    score_programmatically,
)
from evals.scout_cases import CASES, ScoutCase
from grant_writer.prompts import SCOUT_PROMPT

# A well-formed scoring file for the `genuine-fit` fixture, quoting only text
# that is genuinely in the two documents. Every other constant here is a
# mutation of this one.
GOOD = """\
# Opportunity: Rural Out-of-School STEM Partnerships

- Number: ED-26-OSS-04
- Agency: U.S. Department of Education
- Close date: 2026-11-30
- Award range: USD 250,000 to USD 800,000

## eligibility
- Verdict: STRONG
- Citation (opportunity): "Eligible applicants are community-based nonprofit \
organizations with 501(c)(3) status."
- Citation (org profile): "Rural Futures Collective, a 501(c)(3) nonprofit \
incorporated in Montana in 2014."

## mission-alignment
- Verdict: STRONG
- Citation (opportunity): "This program supports out-of-school-time science, \
technology, engineering, and mathematics programming for students in rural \
districts."
- Citation (org profile): "Out-of-school STEM learning for students in rural \
districts across Montana and eastern Idaho."

## program-fit
- Verdict: STRONG
- Citation (org profile): "Afterschool robotics clubs in 11 rural districts, \
roughly 600 students a year."

## track-record
- Verdict: MODERATE
- Citation (org profile): "A summer field-science residency, 40 students a \
year, running since 2019."

## award-size-fit
- Verdict: STRONG
- Citation (org profile): "Largest grant managed to date: USD 750,000 over \
three years."

## timeline-feasibility
- Verdict: MODERATE
- Citation (opportunity): "Close date: 2026-11-30"
"""


def _case(key: str) -> ScoutCase:
    return next(case for case in CASES if case.key == key)


def _score(case: ScoutCase, output: str) -> dict[str, bool]:
    return {s.name: s.passed for s in score_programmatically(case, output)}


def _detail(case: ScoutCase, output: str, name: str) -> str:
    return next(
        s.detail for s in score_programmatically(case, output) if s.name == name
    )


def test_a_correct_answer_passes_every_scorer():
    """The control, and the one that matters most.

    A scorer suite that fails nothing is useless; one that fails *everything*
    is worse, because it looks like the prompt is broken and the prompt is
    where people will go looking. Every mutation case below is only meaningful
    against this baseline.
    """
    assert all(_score(_case("genuine-fit"), GOOD).values())


def test_an_unparseable_file_is_caught_rather_than_scored():
    """Reported as a parse failure, not as a low score.

    Invariant 14's distinction, one level up: a scout whose output stopped
    conforming would otherwise read as a scout that judged everything harshly,
    and the fix for those two is not the same.
    """
    scores = _score(_case("genuine-fit"), "I think this looks like a decent fit!")
    assert scores["parses"] is False


def test_a_wrong_gating_verdict_is_caught():
    hopeful = GOOD.replace(
        "## eligibility\n- Verdict: STRONG", "## eligibility\n- Verdict: STRONG"
    )
    scores = _score(_case("plainly-ineligible"), hopeful)
    assert scores["eligibility"] is False
    assert scores["disqualified"] is False


def test_a_quote_that_is_in_neither_document_is_caught():
    """The `leaky-brief` failure, reproduced by hand.

    The quoted range is in the delegation message and in neither file, which is
    exactly what a scout handed the orchestrator's web research produces.
    """
    leaky = GOOD.replace(
        '- Citation (opportunity): "Close date: 2026-11-30"',
        '- Citation (opportunity): "awards USD 400,000 to USD 1,200,000"',
    )
    scores = _score(_case("genuine-fit"), leaky)
    assert scores["citations"] is False
    assert "1,200,000" in _detail(_case("genuine-fit"), leaky, "citations")


def test_naming_a_leaked_figure_in_order_to_reject_it_is_not_invention():
    """The second eval run's finding, pinned so it cannot come back.

    Handed a brief carrying figures that appear in neither document, the scout
    refused them and said so in its closing report. A substring search over the
    whole output scored that as invention -- marking the model down for doing
    the right thing and explaining it, which makes correct behaviour look like
    the bug and is the worst thing an eval can do.

    The scorer now reads citation text only, because a citation is where a
    figure becomes load-bearing. Prose that names a claim to reject it is the
    judge's problem, not a string match's.
    """
    case = _case("leaky-brief")
    refusing = (
        GOOD + "\nFlagged as missing: the searcher-supplied award range "
        "(USD 400,000 to USD 1,200,000) and the persistent-poverty priority "
        "were not used as citations, since neither appears in the file.\n"
    )
    assert _score(case, refusing)["no-invention"] is True

    # ...and a figure that reaches a citation is still caught, which is the
    # half that must survive the narrowing.
    relying = GOOD.replace(
        '- Citation (opportunity): "Close date: 2026-11-30"',
        '- Citation (opportunity): "awards USD 400,000 to USD 1,200,000"',
    )
    assert _score(case, relying)["no-invention"] is False


def test_the_judge_is_told_that_rejecting_a_claim_is_not_asserting_it():
    """The same finding, on the judge's side of the line.

    Both scorers flagged that closing report, so fixing only the programmatic
    one would have left the run still reporting a failure for it -- and the
    next reader would conclude the prompt had regressed.
    """
    from evals.scorers import JUDGE_PROMPT

    lowered = JUDGE_PROMPT.lower()
    assert "reject" in lowered
    assert "rel" in lowered, "the judge must ask whether the claim is relied on"


def test_a_filled_in_silence_is_caught_two_ways():
    """`silent-profile` asserts both halves, and they are different failures.

    A scout can invent without dropping the marker (it answers the question
    *and* flags it), and it can drop the marker without inventing (it scores
    the criterion WEAK on no evidence). Only checking both catches both.
    """
    case = _case("silent-profile")
    # GOOD cites a track record and an award size the silent profile deleted.
    scores = _score(case, GOOD)
    assert scores["gaps"] is False, "no [NEEDS INPUT] marker anywhere"
    assert scores["no-invention"] is False, "cites facts the profile no longer has"


def test_a_marked_gap_satisfies_the_gap_scorer():
    marked = GOOD.replace(
        '- Citation (org profile): "Largest grant managed to date: USD 750,000 '
        'over three years."',
        '- Citation (org profile): "[NEEDS INPUT: what is the largest grant '
        'the organization has managed?]"',
    )
    assert _score(_case("silent-profile"), marked)["gaps"] is True


def test_a_stated_percentage_is_caught_but_a_quoted_number_is_not():
    """Invariant 13's live counterpart, and the false positive it must avoid.

    A funder's award range is a number the scout is *required* to quote, so a
    naive digit check would fail every correct answer. Citations are stripped
    before the search for exactly that reason -- and this pins both directions,
    because a check that fires on honest output gets deleted, not fixed.
    """
    case = _case("genuine-fit")
    assert _score(case, GOOD)["no-total"] is True, "quoted award ranges are fine"

    stated = GOOD + "\n- Note: overall this is roughly 80% fit.\n"
    assert _score(case, stated)["no-total"] is False


def test_the_judge_verdict_reader_handles_all_three_replies():
    """An unreadable judge reply must not read as either outcome.

    Folded into PASS it hides a real finding; folded into FAIL it blames the
    scout for the judge's malfunction. It is reported as skipped instead, which
    is the only honest option and the one that shows up in the run summary as
    a check that did not happen.
    """
    assert read_judge_verdict("PASS\nnothing unsupported").passed is True
    assert read_judge_verdict("PASS").skipped is False

    failed = read_judge_verdict('FAIL\n"nine full-time staff" is not in the profile')
    assert failed.passed is False
    assert "nine full-time staff" in failed.detail

    confused = read_judge_verdict("I'm not sure what you're asking.")
    assert confused.skipped is True


def test_the_judge_sees_every_input_the_scout_had():
    """A judge shown only the assessment cannot check grounding at all.

    It would have nothing to check the claims *against*, so it would answer
    from plausibility -- and a fabricated figure in a grant assessment is
    plausible by construction.

    The brief is here because leaving it out cost a false FAIL on the first
    live run: a scout that correctly named the brief's unverifiable award
    range in order to exclude it looked, to a judge holding only the two
    files, like a scout inventing a source. Three of these four assertions
    were already here; the missing one was never considered.
    """
    case = _case("leaky-brief")
    payload = build_judge_payload(case, GOOD)

    assert case.brief in payload
    assert case.candidate in payload
    assert case.profile in payload
    assert GOOD in payload


def test_the_judge_prompt_and_its_payload_name_the_same_documents():
    """The drift that produced the false FAIL, pinned in both directions.

    `JUDGE_PROMPT` used a delegation brief by name in its worked example while
    `build_judge_payload` never sent one. Neither file was wrong on its own
    terms, which is why it survived review and a passing suite -- the same
    shape `rubric_brief()` exists to prevent one directory over, where a
    criterion named in the prompt but absent from the parser is answered in
    good faith and scored zero.
    """
    payload = build_judge_payload(_case("genuine-fit"), GOOD)

    # Tags are on their own lines, so this cannot match `<question>` inside a
    # `[NEEDS INPUT: ...]` marker in the assessment.
    sent = {
        tag.replace("-", " ").upper()
        for tag in re.findall(r"^<([a-z-]+)>$", payload, re.MULTILINE)
    }
    listed = re.search(r"Below are \w+ documents:(.*?)\n\n", JUDGE_PROMPT, re.DOTALL)
    assert listed, (
        "JUDGE_PROMPT no longer opens with the document list this parses. "
        "Without it the comparison below is vacuous."
    )
    # Collapsed before matching, because the prompt is hard-wrapped and
    # "ORGANIZATION PROFILE" straddles a line break -- a line-oriented match
    # reports it as two documents named ORGANIZATION and PROFILE. Invariant
    # 16's rule about wrapped citations, met again one directory over.
    block = " ".join(listed.group(1).split())
    named = set(re.findall(r"[A-Z]{2,}(?: [A-Z]{2,})*", block))

    assert sent, "no document tags found in the payload"
    assert named == sent, (
        f"JUDGE_PROMPT names {sorted(named)} but the payload sends "
        f"{sorted(sent)}. A document named to the judge and not supplied is "
        f"reasoned about from its absence; one supplied and not named is read "
        f"as a source it was never meant to be."
    )


def test_a_truncated_verdict_says_that_it_is_truncated():
    """An unmarked cut reads as a complete sentence that makes no sense.

    The first live run ended a real verdict at `: "T`, and the reasoning that
    identified it as a scorer bug rather than a prompt regression was past the
    cut. Marking it is what tells the reader to open the trace.
    """
    short = read_judge_verdict("FAIL\nIt invented a figure.")
    assert short.detail == "It invented a figure."
    assert "[...]" not in short.detail

    long = read_judge_verdict("FAIL\n" + "x" * 400)
    assert long.detail.endswith(" [...]")
    assert len(long.detail) == 206


def test_every_case_declares_why_it_exists():
    """A fixture nobody can explain is a fixture nobody will maintain.

    When one of these fails, `why` is what tells the reader whether the prompt
    regressed or the case was always arguable.
    """
    for case in CASES:
        assert case.why.strip(), case.key
        assert case.brief.strip(), case.key


# ---- the labels a traced eval run carries -----------------------------------
#
# `run_scout` needs a live key, so nothing here can run it. What is checkable
# offline is the part that decides what a run is *called* -- and that is the
# part whose failure is silent, since an unlabelled trace is still a trace and
# still costs the same money to produce.


@pytest.fixture(autouse=True)
def _empty_session_id_cache():
    """`run_scout._SESSION_IDS` caches a project uuid for the whole process.

    The same hazard `_app_test` clears `st.cache_resource` for in
    `test_frontends.py`: a cache outliving the case that filled it makes a
    later case's branch depend on collection order. Here it costs coverage
    rather than a flake -- once any case has resolved "proj", the next case's
    fake client is never asked, and a test written to exercise the lookup
    exercises the cached hit instead, passing either way.

    Autouse rather than a line per case, because a line per case is what this
    already was: two of the four cases reset it and two did not, and the
    omission does not fail -- it makes some *other* case pass for the wrong
    reason. `.clear()` on the live dict rather than rebinding it, so the object
    `session_id` actually reads is the one that ends up empty, whatever a
    previous case rebound and in whatever order teardowns ran.
    """
    run_scout._SESSION_IDS.clear()


def _run_tree(client=None) -> RunTree:
    """A real `RunTree`, which is what `trace()` hands `post_scores`.

    A `SimpleNamespace` with an `id` would satisfy the code and read as a
    lighter fixture, but it also satisfies a `post_scores` that later grows a
    second attribute it never had -- and constructing the real thing costs
    nothing offline: no client, no credential, no network.

    `client` rides on the run rather than being monkeypatched onto the module,
    because that is now how `post_scores` gets one: it uses `run.client`, so a
    stand-in belongs on the run for the same reason the real one does.
    """
    run = RunTree(name="scout:test", run_type="chain", session_name="proj")
    if client is not None:
        run.ls_client = client
    return run


def test_each_model_call_is_labelled_with_the_case_it_came_from():
    """Two calls per case, and neither used to say which case."""
    case = _case("genuine-fit")
    config = run_scout.call_config("scout", case, "anthropic:claude-sonnet-5")

    assert config["run_name"] == "scout"
    assert f"case:{case.key}" in config["tags"]
    assert config["metadata"] == {
        "case": case.key,
        "role": "scout",
        "model": "anthropic:claude-sonnet-5",
    }


def test_the_scout_and_the_judge_are_told_apart():
    """They are different prompts on different models scoring the same output.

    Undistinguished, the judge's verdict reads as a second opinion from the
    scout -- and the judge exists precisely because it is not one.
    """
    case = _case("genuine-fit")
    scout = run_scout.call_config("scout", case, "anthropic:claude-sonnet-5")
    judge = run_scout.call_config("judge", case, "anthropic:claude-opus-5")

    assert scout["run_name"] != judge["run_name"]
    assert scout["metadata"]["model"] != judge["metadata"]["model"]
    assert set(scout["tags"]) & set(judge["tags"]) == {
        "eval",
        "scout-prompt",
        f"case:{case.key}",
    }


def test_feedback_is_gated_on_the_switch_the_suite_forces_off(monkeypatch):
    """`trace()` hands back a real RunTree even with tracing disabled.

    So "we are inside a run" is not on its own permission to write to someone's
    workspace -- and the suite runs inside no run at all, which would have
    hidden this. The guard is `tracing_is_enabled()`, the same predicate
    invariant 19 pins to false across all four of its env spellings, so the
    eval's one write is disarmed by the guard that already keeps the suite
    offline rather than by a second rule that could drift from it.
    """
    from langsmith.utils import tracing_is_enabled

    assert not tracing_is_enabled(), "conftest should have forced this off"

    del monkeypatch  # the gate must hold with nothing patched out
    client = _client()

    run_scout.post_scores(_run_tree(client), [Score(name="parses", passed=True)])

    assert client.calls == [], "posted feedback from a suite that must stay offline"
    assert client.seen == [], "resolved a project id from a suite that is offline"


def test_posting_scores_without_a_run_is_a_no_op():
    """Nothing to attach feedback to, and no credential needed to find out."""
    run_scout.post_scores(
        None, [Score(name="parses", passed=True), Score(name="cited", passed=False)]
    )


def _client(**behaviour):
    """A stand-in LangSmith client that records what it was asked to write.

    Built here rather than per case because the shape is the thing these pin:
    `post_scores` calls `read_project` once and `create_feedback` per score, and
    a fake that quietly accepts a different shape is how a rewrite passes while
    posting nothing.
    """
    posted: list[dict] = []
    lookups: list[str] = []

    class _Client:
        calls = posted
        seen = lookups

        def read_project(self, *, project_name):
            lookups.append(project_name)
            if "read_project" in behaviour:
                raise behaviour["read_project"]
            return SimpleNamespace(id="proj-uuid")

        def create_feedback(self, _run_id, **kwargs):
            if kwargs["key"] in behaviour.get("fail_keys", ()):
                raise ConnectionError("no route to host")
            posted.append(kwargs)

    return _Client()


def test_feedback_goes_through_the_runs_own_client(monkeypatch):
    """`Client()` re-resolves endpoint and key from the environment.

    So a client built per case can address a different workspace than the one
    that created the run it is attaching to, and the disagreement is silent:
    the feedback is simply absent from the trace someone opens. `run.client` is
    the client the run was made with. Pinned by handing the run a stand-in and
    asserting the write went there -- a `post_scores` that built its own would
    post nothing here and fail loudly.
    """
    monkeypatch.setattr(run_scout, "tracing_is_enabled", lambda: True)
    client = _client()

    run_scout.post_scores(_run_tree(client), [Score(name="parses", passed=True)])

    assert [call["key"] for call in client.calls] == ["parses"]


def test_a_skipped_scorer_posts_nothing_rather_than_a_pass(monkeypatch):
    """ "Not checked" and "checked and fine" are opposite readings of a number.

    `Score.skipped` exists because a case may decline to assert on a
    dimension -- the same distinction `fit_percent` keeps for `None` under
    invariant 14. Posted as `1.0`, a run that asserted almost nothing averages
    out looking like one that asserted everything and was right, and the
    quietest failure of an eval is one reporting health it never measured.
    """
    monkeypatch.setattr(run_scout, "tracing_is_enabled", lambda: True)
    client = _client()

    run_scout.post_scores(
        _run_tree(client),
        [
            Score(name="parses", passed=True, detail="fit 72%"),
            Score(
                name="gaps", passed=True, detail="case asserts nothing", skipped=True
            ),
            Score(name="total", passed=False, detail="stated a total"),
        ],
    )

    assert [call["key"] for call in client.calls] == ["parses", "total"]
    assert [call["score"] for call in client.calls] == [1.0, 0.0]


def test_a_failure_on_one_score_does_not_drop_the_rest(monkeypatch, capsys):
    """One `try` around the loop lost every score after the first failure.

    A transient 502 on score two of seven left LangSmith holding a case run
    with one feedback key -- which reads as "one dimension was checked", the
    same collapse the skipped rule above exists to prevent, reached by another
    route. The stderr line has to name the case and the key, because it is
    printed before `_render` emits any case heading.
    """
    monkeypatch.setattr(run_scout, "tracing_is_enabled", lambda: True)
    client = _client(fail_keys=("citations",))

    run_scout.post_scores(
        _run_tree(client),
        [
            Score(name="parses", passed=True),
            Score(name="citations", passed=True),
            Score(name="grounded", passed=False),
        ],
    )

    assert [call["key"] for call in client.calls] == ["parses", "grounded"]
    err = capsys.readouterr().err
    assert "scout:test" in err and "'citations'" in err
    assert "feedback not recorded" in err


def test_an_unresolvable_project_costs_the_label_not_the_scores(monkeypatch, capsys):
    """Omitting `session_id` is deprecated, not broken.

    The lookup ran inside the same `try` as the writes, so a LANGSMITH_PROJECT
    typo or a key without project-read scope cost the case every score it had
    computed -- to avoid a deprecation. The scores are the expensive half and
    are gone once the process exits, so the label is what gives way.

    The stderr line deliberately does not say "feedback not recorded": nothing
    was lost, and a reader grepping a run's stderr for dropped scores must not
    land on it.
    """
    monkeypatch.setattr(run_scout, "tracing_is_enabled", lambda: True)
    client = _client(read_project=ValueError("no project named 'proj'"))

    run_scout.post_scores(_run_tree(client), [Score(name="parses", passed=True)])

    assert [call["key"] for call in client.calls] == ["parses"]
    assert client.calls[0]["session_id"] is None
    err = capsys.readouterr().err
    assert "project id unresolved" in err
    assert "feedback not recorded" not in err


def test_feedback_names_the_project_rather_than_the_run_alone(monkeypatch):
    """Posting against a `run_id` alone is deprecated and will stop working.

    A `RunTree` carries only `session_name`, so the uuid has to be resolved --
    and once per process, not once per score: four cases times seven scorers is
    twenty-eight chances to turn one lookup into twenty-eight.
    """
    monkeypatch.setattr(run_scout, "tracing_is_enabled", lambda: True)
    client = _client()

    run = _run_tree(client)
    for _ in range(3):
        run_scout.post_scores(run, [Score(name="parses", passed=True)])

    assert client.seen == ["proj"], "the project uuid was looked up more than once"
    assert [call["session_id"] for call in client.calls] == ["proj-uuid"] * 3
    assert all(call["trace_id"] == run.trace_id for call in client.calls)


# ---- the dataset mirror ------------------------------------------------------
#
# `push_dataset` needs a live workspace, so nothing here pushes. What is
# checkable offline is every decision it makes: the id, the digest and the plan
# are pure functions, and the network is two reads and three writes that carry
# a plan out. The failure these exist for is not a traceback -- it is a mirror
# that overwrites `scout_cases.py`'s four fixtures with a lossy or duplicated
# copy of themselves and prints "4 unchanged" while doing it.


def _case(key: str) -> ScoutCase:
    return next(case for case in CASES if case.key == key)


def _row(key: str) -> dict:
    return push_dataset.mirror_row(_case(key))


def _remote(*rows: dict) -> dict[str, dict]:
    """Rows as `fetch` would hand them back: keyed by id, no `id` inside."""
    return {
        row["id"]: {k: json.loads(json.dumps(row[k])) for k in push_dataset._ROW_KEYS}
        for row in rows
    }


def _all_rows() -> list[dict]:
    return [push_dataset.mirror_row(case) for case in CASES]


def _fake_client(*, rows=None, missing=False, racing=False, swallow_updates=False):
    """A stand-in LangSmith client, with the real methods' real shapes.

    The signatures are copied deliberately rather than loosened to `**kwargs`:
    `create_examples` and `update_examples` are keyword-only in this SDK and
    `delete_examples` takes its ids positionally, so a fake that accepts
    anything is how a rewrite passes here while calling nothing that exists.
    The same hazard `_client` above names for `create_feedback`.

    `rows` is mutated by the writes, so a *working* fake converges on the
    second read and a swallowing one does not -- which is the whole of what
    `push`'s verification pass is for.
    """
    store: dict[str, dict] = dict(rows or {})
    calls: list[tuple] = []
    dataset = SimpleNamespace(
        id=uuid.UUID("11111111-2222-3333-4444-555555555555"),
        url="https://smith.langchain.com/o/t/datasets/1111",
    )

    class _Client:
        seen = calls
        rows = store
        dataset_obj = dataset

        #: The methods below that only look. `_writes` counts every *other*
        #: recorded call as a write, so a mutating method added here is caught
        #: with no second edit -- which is the direction that matters: the
        #: defect this replaced was a write-detector that reported nothing,
        #: and an over-reporting one cannot reproduce it. Add a *reading*
        #: method and the first `_writes(client) == []` assertion it reaches
        #: fails loudly, one line from the method that caused it.
        #:
        #: A class attribute rather than a marker on each function, because
        #: two cases rebind `delete_examples` on the instance with a plain
        #: stub; a detector reading marks off bound methods would stop seeing
        #: those, and this one is unmoved by them.
        reads = frozenset({"has_dataset", "read_dataset", "list_examples"})

        def has_dataset(self, *, dataset_name=None, dataset_id=None):
            calls.append(("has_dataset", dataset_name))
            return not missing

        def read_dataset(self, *, dataset_name=None, dataset_id=None):
            calls.append(("read_dataset", dataset_name))
            if missing and not any(c[0] == "create_dataset" for c in calls):
                raise LangSmithNotFoundError("no such dataset")
            return dataset

        def create_dataset(self, dataset_name, *, description=None, **kwargs):
            calls.append(("create_dataset", dataset_name))
            if racing:
                # Somebody else won: the name now exists, and the next read
                # is what this client is supposed to fall back to.
                raise LangSmithConflictError("already exists")
            return dataset

        def list_examples(self, dataset_id=None, dataset_name=None, **kwargs):
            calls.append(("list_examples", dataset_id))
            return [
                SimpleNamespace(id=key, **value) for key, value in sorted(store.items())
            ]

        def create_examples(self, *, dataset_name=None, dataset_id=None, examples=None):
            calls.append(("create_examples", tuple(e["id"] for e in examples or ())))
            for row in examples or ():
                store[row["id"]] = {k: row[k] for k in push_dataset._ROW_KEYS}

        def update_examples(self, *, dataset_name=None, dataset_id=None, updates=None):
            calls.append(("update_examples", tuple(u["id"] for u in updates or ())))
            if swallow_updates:
                return
            for row in updates or ():
                store[row["id"]] = {k: row[k] for k in push_dataset._ROW_KEYS}

        def delete_examples(self, example_ids, *, hard_delete=False):
            calls.append(("delete_examples", tuple(example_ids)))
            for key in example_ids:
                store.pop(key, None)

    return _Client()


def _writes(client) -> list[tuple]:
    """Every call that changes the workspace, the dataset itself included.

    Derived from the fake rather than restated beside it: `_Client.reads` names
    the three methods that only look, and every other call recorded counts as a
    write. The hand-maintained allowlist this replaces had the shape of the
    defect it was meant to catch -- `create_dataset` was absent from it, so a
    dry run that created the dataset it was only planning against satisfied
    every "nothing was written" assertion in this file, because the one write
    it made writes no example. A write-detector blind to a write is worse than
    none: it is the assertion the next reader trusts.

    `client.reads` with no fallback, because the other fake in this file
    records bare strings in `.seen` -- handed one, this has to raise rather
    than report that nothing was written.
    """
    return [call for call in client.seen if call[0] not in client.reads]


def _no_dotenv(monkeypatch) -> None:
    """Stand `push_dataset`'s loader down, for any test that drives `main`.

    `main` calls `load_dotenv()` below the `--out` early return, so every case
    that gets past that return reads the developer's own environment file for
    real. python-dotenv assigns straight into `os.environ`, which `monkeypatch`
    never saw happen and so never undoes: any name `conftest.py` does not
    pre-blank -- `LANGSMITH_ENDPOINT`, an uncommented `GRANT_WRITER_*_MODEL`
    that `test_wiring` pins -- is then set for the rest of the session, and
    which later cases see it depends on collection order. The assertion here
    would depend on whose machine it runs on too, which is the machine-
    dependent green this suite is written to avoid everywhere else.
    """
    monkeypatch.setattr(push_dataset, "load_dotenv", lambda *a, **k: None)


def test_the_same_case_always_lands_on_the_same_example_id():
    """The id is the row, so the namespace is frozen forever.

    Change it and the next push does not rewrite four rows -- it orphans four
    and creates four more, and prints "4 created" while doing it. Deriving the
    id is what makes "overwrite" mean overwrite: minted fresh each run, the
    only way to avoid duplicates is to empty the dataset first, which churns
    every id an experiment referenced.
    """
    assert uuid.UUID("fabd0b39-bf93-57c2-874b-627d03a18a4a") == push_dataset._NAMESPACE
    assert {case.key: str(push_dataset.example_id(case.key)) for case in CASES} == {
        "plainly-ineligible": "910551de-5a5b-5caf-ab36-7d7147604d45",
        "genuine-fit": "609c31b1-ee49-52fd-a185-ffefb29d4dc8",
        "silent-profile": "1658cebc-10ec-5d82-932f-f46403f8b6ca",
        "leaky-brief": "7ea87426-4447-593e-a22f-814002023f65",
    }


def test_a_renamed_case_key_becomes_a_new_row_and_the_old_one_is_pruned():
    """Intended, and pinned so the next reader does not "fix" it.

    A key is an identity, not a label. Renaming one is renaming the fixture,
    and carrying the old row's experiment history onto it would attach results
    to a case that no longer produced them.
    """
    assert push_dataset.example_id("genuine-fit") != push_dataset.example_id(
        "genuine-fit-v2"
    )
    renamed = replace(_case("genuine-fit"), key="genuine-fit-v2")
    todo = push_dataset.plan((renamed,), _remote(_row("genuine-fit")))

    assert todo.prune == (str(push_dataset.example_id("genuine-fit")),)
    assert [row["metadata"]["key"] for row in todo.create] == ["genuine-fit-v2"]


def test_a_row_that_has_been_through_json_is_not_read_as_changed():
    """`forbidden` is a tuple here and a list there.

    Uncanonicalised, every row differs from itself: the mirror rewrites all
    four on every push, forever, reporting "4 updated" each time and never
    converging. The one bug in this module that would look like it was working.
    """
    for row in _all_rows():
        assert push_dataset.digest(row) == push_dataset.digest(
            json.loads(json.dumps(row))
        )


def test_an_unchanged_dataset_is_planned_for_no_writes_at_all():
    """The idempotency claim, asserted rather than described."""
    todo = push_dataset.plan(CASES, _remote(*_all_rows()))

    assert todo.counts() == (0, 0, len(CASES), 0)
    assert todo.settled(prune=True)


def test_a_hand_edited_verdict_is_planned_for_overwrite():
    """The README's own scenario, and the reason the digest is recomputed.

    Stored as a `source_sha` in metadata, a hand-edit leaves the hash agreeing
    with itself and the push skips the row -- the mirror preserving the drift
    it exists to erase.
    """
    remote = _remote(*_all_rows())
    remote[str(push_dataset.example_id("genuine-fit"))]["outputs"][
        "expect_eligibility"
    ] = "MODERATE"

    todo = push_dataset.plan(CASES, remote)

    assert [row["metadata"]["key"] for row in todo.update] == ["genuine-fit"]
    assert todo.update[0]["outputs"]["expect_eligibility"] == "STRONG"


def test_a_hand_added_metadata_key_is_drift_and_is_not_preserved():
    """A mirror with two pens is not a mirror."""
    remote = _remote(*_all_rows())
    remote[str(push_dataset.example_id("leaky-brief"))]["metadata"]["reviewed_by"] = (
        "someone"
    )

    todo = push_dataset.plan(CASES, remote)

    assert [row["metadata"]["key"] for row in todo.update] == ["leaky-brief"]
    assert set(todo.update[0]["metadata"]) == {"key", "why", "mirror_source"}


def test_a_server_maintained_split_key_is_not_read_as_drift():
    """`Example` carries no `split` field, so a split comes back in metadata.

    Counted as drift, it rewrites every row on every push and the verification
    re-read plans them again -- a push that can never converge, on a key nobody
    here wrote.
    """
    remote = _remote(*_all_rows())
    remote[str(push_dataset.example_id("genuine-fit"))]["metadata"]["dataset_split"] = [
        "base"
    ]

    assert push_dataset.plan(CASES, remote).counts() == (0, 0, len(CASES), 0)


def test_a_row_that_no_case_claims_is_planned_for_deletion():
    """Pruning is what stops a renamed or deleted fixture living on."""
    stray = "8f14e45f-ceea-4670-94ab-8f0f1e2f3a4b"
    remote = _remote(*_all_rows()) | {
        stray: {"inputs": {"brief": "added by hand"}, "outputs": {}, "metadata": {}}
    }

    todo = push_dataset.plan(CASES, remote)

    assert todo.prune == (stray,)
    assert todo.counts() == (0, 0, len(CASES), 1)


def _shape(name: str) -> dict[str, dict]:
    """Built in the body, never in a `parametrize` argument list.

    Those are evaluated at collection, and `_all_rows` raises `PushRefused` the
    moment `ScoutCase` grows a field the mirror does not carry -- so the very
    change two tests here exist to report clearly would instead abort
    collection of the whole module, scorer tests included, with a traceback
    from a decorator and neither of those tests ever running.
    """
    rows = _all_rows()
    return {
        "empty": {},
        "full": _remote(*rows),
        "partial": _remote(*rows[:2]),
        "with-stray": _remote(*rows) | {"8f14e45f-ceea-4670-94ab-8f0f1e2f3a4b": {}},
    }[name]


@pytest.mark.parametrize("shape", ["empty", "full", "partial", "with-stray"])
def test_every_case_is_planned_exactly_once(shape):
    """A plan can never both write and delete the same row.

    The partition is the invariant: every fixture lands in exactly one of
    create/update/unchanged, and `prune` touches none of them. Overlap here
    means a push that deletes what it just wrote, order-dependently.
    """
    todo = push_dataset.plan(CASES, _shape(shape))
    groups = [
        {row["id"] for row in todo.create},
        {row["id"] for row in todo.update},
        {row["id"] for row in todo.unchanged},
    ]

    assert set().union(*groups) == {
        str(push_dataset.example_id(case.key)) for case in CASES
    }
    assert sum(len(group) for group in groups) == len(CASES)
    assert set(todo.prune).isdisjoint(set().union(*groups))


def test_every_scout_case_field_reaches_the_dataset():
    """A field added to the fixture and not mirrored is scored as a pass.

    The dataset would assert less than the file does, and the dimension it
    stopped checking reads exactly like one that was checked and satisfied --
    the collapse invariant 14 forbids for `fit_percent`, arriving in a new
    medium.
    """
    assert {
        *push_dataset._INPUT_FIELDS,
        *push_dataset._OUTPUT_FIELDS,
        *push_dataset._METADATA_FIELDS,
    } == {field.name for field in fields(ScoutCase)}


def test_a_field_the_mirror_does_not_carry_refuses_the_push(monkeypatch):
    """The guard itself, not merely the partition it checks today."""
    monkeypatch.setattr(
        push_dataset,
        "_OUTPUT_FIELDS",
        ("expect_eligibility", "expect_disqualified", "expect_gap_markers"),
    )

    with pytest.raises(push_dataset.PushRefused, match="forbidden"):
        push_dataset.mirror_row(_case("genuine-fit"))


def test_an_example_round_trips_back_into_the_case_it_came_from():
    """The offline proof that a dataset-driven run needs no second scorer.

    `score_programmatically` takes a `ScoutCase`, and the scorers are what
    `tests/` already covers. A translation layer built later on the other side
    of the network is a second scoring path to keep right.
    """
    for case, row in zip(CASES, _all_rows(), strict=True):
        rebuilt = push_dataset.case_from_example(
            row["inputs"], row["outputs"], row["metadata"]
        )
        assert rebuilt == case
        assert isinstance(rebuilt.forbidden, tuple)


def test_an_unasserted_expectation_is_null_and_not_missing():
    """ "The eval declines to assert here" is not "the field does not exist".

    `silent-profile` asserts no eligibility verdict and `leaky-brief` asserts
    no disqualification, both on purpose. Stripped rather than nulled, a case
    that deliberately checks nothing on a dimension is indistinguishable from
    one whose expectation was lost in transit.
    """
    rows = {row["metadata"]["key"]: row for row in _all_rows()}

    assert rows["silent-profile"]["outputs"]["expect_eligibility"] is None
    assert "expect_disqualified" in rows["leaky-brief"]["outputs"]
    assert rows["leaky-brief"]["outputs"]["expect_disqualified"] is None


def test_a_row_is_a_pure_function_of_its_case():
    """No clock and no fresh uuid, or every push differs from every other."""
    assert all(set(row) == {"id", *push_dataset._ROW_KEYS} for row in _all_rows())
    assert _all_rows() == _all_rows()


def test_the_prompt_under_test_is_not_baked_into_the_dataset():
    """Frozen at push time, `SCOUT_PROMPT` becomes invisible to the eval.

    The dataset would then grade last month's prompt against this month's
    fixtures and report on neither -- the drift `prompts.rubric_brief()`
    exists to prevent one directory over, in the other direction.
    """
    assert SCOUT_PROMPT not in json.dumps(_all_rows())


def test_every_row_says_that_it_is_a_mirror():
    """The only warning a UI editor gets is the one on the row in front of them."""
    assert all(
        row["metadata"]["mirror_source"] == "evals/scout_cases.py"
        for row in _all_rows()
    )
    assert "scout_cases.py" in push_dataset.NOTICE
    assert "overwritten" in push_dataset.NOTICE


def test_pushing_into_a_dataset_this_mirror_never_wrote_is_refused():
    """Refusal comes before the first write, not after it.

    `--dataset` naming somebody's real dataset would otherwise overwrite four
    rows in it and delete every other one, and the prune is not undone by
    noticing afterwards.
    """
    foreign = {
        "3f2504e0-4f89-41d3-9a0c-0305e82c3301": {
            "inputs": {"question": "unrelated"},
            "outputs": {},
            "metadata": {"owner": "someone else"},
        }
    }
    client = _fake_client(rows=foreign)

    with pytest.raises(push_dataset.PushRefused, match="mirror_source"):
        push_dataset.push(client, name="scout-regressions")

    assert _writes(client) == []


def test_a_dataset_that_does_not_exist_is_not_created_without_being_asked():
    """A typo in `--dataset` creates a second mirror and reports success.

    Gated on the existence check rather than on the create call, so the
    refusal happens before anything is written -- which is what `PushRefused`
    claims about itself.
    """
    client = _fake_client(missing=True)

    with pytest.raises(push_dataset.PushRefused, match="--create"):
        push_dataset.push(client, name="grant-writer-scout-cses", create=False)

    assert ("create_dataset", "grant-writer-scout-cses") not in client.seen


def test_a_missing_dataset_is_created_and_a_racing_push_is_not_an_error():
    """Names are unique per workspace, so a concurrent create 409s.

    Read-create-read rather than a lock: two people pushing the same fixtures
    at once should both succeed, because they are pushing the same bytes.
    """
    client = _fake_client(rows={}, missing=True, racing=True)

    dataset, existed = push_dataset.ensure_dataset(
        client, "grant-writer-scout-cases", create=True
    )

    assert dataset is client.dataset_obj
    assert existed, "the racing create means somebody else got there first"
    assert [call[0] for call in client.seen] == [
        "read_dataset",
        "create_dataset",
        "read_dataset",
    ]


def test_a_push_with_nothing_to_do_writes_nothing_and_reads_twice():
    """Two reads, zero writes, and no new dataset version.

    The verification re-read is skipped when the first read already proved
    there was nothing to verify -- a no-op push that cut a dataset version
    every time would make the version history unreadable.
    """
    client = _fake_client(rows=_remote(*_all_rows()))

    result = push_dataset.push(client)

    assert _writes(client) == []
    assert [call[0] for call in client.seen].count("list_examples") == 1
    assert result.converged
    assert result.planned.counts() == (0, 0, len(CASES), 0)


def test_every_write_carries_every_mirrored_field():
    """`update_examples` overwrites what it is given and leaves the rest.

    So a diff-and-patch optimisation sending only the changed field would
    silently preserve every hand-edit it did not happen to notice. Supplying
    all three is what turns a partial API into a full overwrite.
    """
    remote = _remote(*_all_rows())
    remote[str(push_dataset.example_id("genuine-fit"))]["outputs"] = {}
    client = _fake_client(rows=remote)

    push_dataset.push(client)

    sent = [call for call in client.seen if call[0] == "update_examples"]
    assert len(sent) == 1
    updated = client.rows[str(push_dataset.example_id("genuine-fit"))]
    assert set(updated) == set(push_dataset._ROW_KEYS)
    assert updated["outputs"]["expect_eligibility"] == "STRONG"


def test_a_write_that_did_not_take_is_reported_rather_than_assumed(monkeypatch):
    """The read-back is what makes the idempotency claim checked, not written.

    Whether `update_examples` replaces the `outputs` document or merges into
    it is not settled by the SDK source. Rather than assert which, the push
    re-plans against what came back and fails loudly when a write vanished --
    the failure mode that would otherwise be a mirror reporting "1 updated"
    forever while the row never changed.
    """
    _no_dotenv(monkeypatch)
    monkeypatch.setenv("LANGSMITH_API_KEY", "dummy")
    monkeypatch.setattr("sys.argv", ["evals.push_dataset"])
    remote = _remote(*_all_rows())
    remote[str(push_dataset.example_id("silent-profile"))]["outputs"] = {}
    client = _fake_client(rows=remote, swallow_updates=True)
    monkeypatch.setattr(push_dataset, "build_client", lambda: client)

    assert push_dataset.main() == 1

    assert [call[0] for call in client.seen].count("update_examples") == 1, (
        "the push path was never reached, so the read-back was never exercised"
    )
    result = push_dataset.push(client)
    assert result.converged is False
    assert result.residual is not None
    assert [row["metadata"]["key"] for row in result.residual.update] == [
        "silent-profile"
    ]


def test_a_dry_run_plans_and_writes_nothing(monkeypatch):
    """A plan is worth reading before a deletion, not after it."""
    _no_dotenv(monkeypatch)
    monkeypatch.setenv("LANGSMITH_API_KEY", "dummy")
    monkeypatch.setattr("sys.argv", ["evals.push_dataset", "--dry-run"])
    remote = _remote(*_all_rows())
    remote[str(push_dataset.example_id("genuine-fit"))]["outputs"] = {}
    client = _fake_client(rows=remote)
    monkeypatch.setattr(push_dataset, "build_client", lambda: client)

    assert push_dataset.main() == 0

    assert _writes(client) == []
    result = push_dataset.push(client, dry_run=True)
    assert [row["metadata"]["key"] for row in result.planned.update] == ["genuine-fit"]
    assert result.live is False
    assert result.residual is None


def test_a_dry_run_does_not_create_the_dataset_it_is_planning_against(
    monkeypatch, capsys
):
    """ "Write nothing" has to include the dataset itself.

    `main` turns `create` on for the default name, so the one flag documented
    to write nothing called `create_dataset` on a fresh workspace and printed
    the new dataset's URL directly above "Nothing was written." What it left
    behind carries no `mirror_source`, so `assert_ours` then refused the *next*
    real push -- the tool asking for `--adopt` on a dataset its own dry run had
    made, and the two commands `evals/README.md` lists in that order unable to
    follow each other on a clean workspace.

    The create bought nothing, which is what makes it pure side effect: a
    dataset that has just been created is as empty as the `{}` the refusal path
    plans against, so both plan the same four rows. What the refusal path has
    to add is the sentence it swallowed -- `ensure_dataset` raised "no dataset
    named X", `push` catches it to keep the question answerable without
    creating, and `(not created)` on its own reads the same on a workspace that
    has never held the mirror as on one where somebody renamed it. The case
    above pairs with this one and cannot replace it -- it runs against a
    dataset that already exists, so it never reaches the branch that creates.
    """
    _no_dotenv(monkeypatch)
    monkeypatch.setenv("LANGSMITH_API_KEY", "dummy")
    monkeypatch.setattr("sys.argv", ["evals.push_dataset", "--dry-run"])
    client = _fake_client(missing=True)
    monkeypatch.setattr(push_dataset, "build_client", lambda: client)

    assert push_dataset.main() == 0

    assert _writes(client) == []
    assert [call[0] for call in client.seen] == ["read_dataset"], (
        "a dry run against a missing dataset reads once and stops; anything "
        "more means it created one and then listed what it had just made"
    )
    out = capsys.readouterr().out
    assert "(not created)" in out
    assert re.findall(r"^  create\s+(\S+)$", out, re.MULTILINE) == [
        case.key for case in CASES
    ], "the plan a first push would make has to survive not creating anything"
    assert [
        line
        for line in out.splitlines()
        if push_dataset.DATASET_NAME in line and "(not created)" not in line
    ], (
        "the header token alone is not a rename signal -- it says the same "
        "thing on a first push -- so the swallowed refusal has to be spoken "
        "somewhere below it, or --dry-run is not what evals/README.md offers"
    )


def test_a_first_push_creates_the_dataset_the_dry_run_would_not():
    """The other leg of `create and not dry_run`, and the one nothing drove.

    Forcing `create` off under `dry_run` is a conjunction, and a suite that
    only ever exercises the false leg cannot tell it from the constant
    `create=False`. Mutated to that, every case in this file still passes while
    the documented first push -- `--create`, or the default name, against a
    workspace that has never held the mirror -- is refused and exits 1. The
    call list below is the assertion that dies: `["read_dataset"]` and nothing
    after it.

    `adopt` is deliberately not passed. `ensure_dataset` reports `existed=False`
    on the create branch and `assert_ours` short-circuits on that, so a dataset
    this run made is ours by construction; a regression there would demand
    `--adopt` for a dataset the tool had created seconds earlier.
    """
    client = _fake_client(missing=True)

    result = push_dataset.push(client, create=True)

    assert [call[0] for call in client.seen] == [
        "read_dataset",
        "create_dataset",
        "list_examples",
        "create_examples",
        "list_examples",
    ], "a first push reads, creates, then pushes into what it just made"
    assert [call[0] for call in _writes(client)] == [
        "create_dataset",
        "create_examples",
    ]
    assert sorted(client.rows) == sorted(
        str(push_dataset.example_id(case.key)) for case in CASES
    )
    assert result.live is True
    assert result.planned.counts() == (len(CASES), 0, 0, 0)
    assert result.converged is True
    assert result.residual is not None
    assert result.residual.counts() == (0, 0, len(CASES), 0)


def test_nothing_is_deleted_when_pruning_is_off():
    """A stray row left in place on purpose is not unfinished work.

    `settled` must agree, or every `--no-prune` push would re-plan the same
    stray, fail its own verification, and exit non-zero forever.
    """
    stray = "8f14e45f-ceea-4670-94ab-8f0f1e2f3a4b"
    remote = _remote(*_all_rows()) | {
        stray: {"inputs": {"brief": "added by hand"}, "outputs": {}, "metadata": {}}
    }
    client = _fake_client(rows=remote)

    result = push_dataset.push(client, prune=False)

    assert not [call for call in client.seen if call[0] == "delete_examples"]
    assert result.planned.prune == (stray,)
    assert result.converged


def test_the_payload_dump_needs_no_credential_and_builds_no_client(
    monkeypatch, tmp_path
):
    """The inspection path, and the only one a test can drive end to end.

    Pins invariant 19.
    It has to work on a machine with no LangSmith account at all, or the first
    thing a reader does to find out what this pushes is push it -- and the
    `delenv` pair below is the honest way to say so, since the suite elsewhere
    *blanks* both names rather than popping them and `load_dotenv` fills only
    names absent from `os.environ`. Deleting them is therefore exactly what a
    `load_dotenv()` sited one line higher would undo: a live credential inside
    a suite that is offline by contract, through a door `conftest.py` cannot
    hold shut, opened by the fix to a different defect. So the loader is stood
    in for too, and the two stubs are kept apart -- which of them fired is the
    difference between "this path built a client" and "this path read an
    environment file", and a shared one would name neither.

    This case cannot see a `load_dotenv()` moved to *module* scope, which runs
    long before `monkeypatch` reaches this binding;
    `test_the_module_reads_no_credential_and_builds_no_client_at_import`
    patches the source binding before its reload and covers that.
    """

    def boom_client():
        raise AssertionError("a client was built for a payload dump")

    def boom_loader(*args, **kwargs):
        raise AssertionError("the payload dump read an environment file")

    monkeypatch.setattr(push_dataset, "build_client", boom_client)
    monkeypatch.setattr(push_dataset, "load_dotenv", boom_loader)
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
    out = tmp_path / "rows.json"
    monkeypatch.setattr("sys.argv", ["evals.push_dataset", "--out", str(out)])

    assert push_dataset.main() == 0

    assert [row["metadata"]["key"] for row in json.loads(out.read_text())] == [
        case.key for case in CASES
    ]


def test_the_credential_may_come_from_the_dotenv_file(monkeypatch):
    """`main` read `os.environ` for a key nothing on this path had loaded.

    `config.py` calls `load_dotenv()` at import and `run_scout` inherits that
    by importing it for `build_model`. This module imports nothing from
    `grant_writer` on purpose, so it was the one entry point in the repo where
    a key sitting in the file `.env.example` names read as absent: the
    documented `--dry-run` exited 1 saying the credential was not set, on a
    machine where it was.

    Both halves are driven with the loader stood in for, because the real one
    reads the developer's own file -- asserting the refusal against it would
    pass on a clean checkout and fail on the machine that has the key, which is
    the machine-dependent green this suite is written to avoid everywhere else.
    """
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
    monkeypatch.setattr("sys.argv", ["evals.push_dataset", "--dry-run"])
    client = _fake_client(rows=_remote(*_all_rows()))
    monkeypatch.setattr(push_dataset, "build_client", lambda: client)

    monkeypatch.setattr(push_dataset, "load_dotenv", lambda *a, **k: None)
    assert push_dataset.main() == 1, "a file carrying no key is still no key"
    assert client.seen == [], "a client was built with no credential to build it on"

    monkeypatch.setattr(
        push_dataset,
        "load_dotenv",
        lambda *a, **k: monkeypatch.setenv("LANGSMITH_API_KEY", "from-the-file"),
    )

    assert push_dataset.main() == 0
    assert [call[0] for call in client.seen] == ["read_dataset", "list_examples"]


def test_the_module_reads_no_credential_and_builds_no_client_at_import(monkeypatch):
    """What keeps this file importable under a suite that blanks the key.

    Pins invariant 19.
    `Client()` resolves endpoint, key and workspace at construction, so one
    built at module scope would make `tests/test_evals.py` uncollectable on a
    clean checkout -- the offline contract broken by the import line alone.

    The loader is patched at its *source*, `dotenv.load_dotenv`, and before the
    reload: `from dotenv import load_dotenv` re-executes on a reload and binds
    whatever the package holds then, so this is the one stub a module-scope
    call would actually hit. Patching `push_dataset.load_dotenv` cannot -- that
    binding is replaced long after import -- which is why the payload-dump case
    above passes unchanged when the call is hoisted to module scope, and why
    the claim it makes needed a second home rather than a second wording.
    Hoisted, the environment file is read on import: before `conftest.py` can
    blank anything on a fresh interpreter, and on every reload after.
    """

    def boom(*args, **kwargs):
        raise AssertionError("a client was constructed at import")

    def boom_loader(*args, **kwargs):
        raise AssertionError("an environment file was read at import")

    monkeypatch.setattr("langsmith.Client", boom)
    monkeypatch.setattr("dotenv.load_dotenv", boom_loader)
    monkeypatch.setenv("LANGSMITH_API_KEY", "")

    try:
        reloaded = importlib.reload(push_dataset)
        assert reloaded.plan(CASES, {}).counts() == (len(CASES), 0, 0, 0)
    finally:
        # `monkeypatch` has no hook for a module reload, and a re-executed
        # module rebinds `PushRefused` to a new class object. Nothing captures
        # one today, which is the only reason leaving it would stay green --
        # the first case that does would fail `pytest.raises` with no
        # explanation, at a distance, depending on collection order.
        #
        # Undone first, or the restoring reload runs with the stubs still in
        # and a failing case leaves the module holding them: the real failure
        # arrives chained behind a second one, and every later case reads a
        # `push_dataset` that never finished re-executing.
        monkeypatch.undo()
        importlib.reload(push_dataset)


def test_a_pruned_row_is_named_before_it_is_actually_deleted(capsys):
    """The one place this destroys someone's work by design.

    Soft deletion and dataset versioning make it recoverable, but only for a
    reader who knows it happened. Asserted against the *call order* rather than
    against the text alone: rendering the plan after `push` returned printed
    the same characters and was a receipt rather than a warning, since the
    delete had already gone out -- and if the run died mid-apply the id was
    never printed at all.
    """
    stray = "8f14e45f-ceea-4670-94ab-8f0f1e2f3a4b"
    remote = _remote(*_all_rows()) | {
        stray: {
            "inputs": {"brief": "a note somebody added by hand in the UI"},
            "outputs": {},
            "metadata": {},
        }
    }
    client = _fake_client(rows=remote)
    order: list[str] = []

    def announce(result):
        push_dataset.render_plan(result)
        order.append("announced")

    def delete_examples(example_ids, *, hard_delete=False):
        order.append("deleted")

    client.delete_examples = delete_examples
    push_dataset.push(client, announce=announce)

    assert order == ["announced", "deleted"]
    out = capsys.readouterr().out
    assert stray in out
    assert "a note somebody added by hand" in out


def test_a_row_the_server_owns_a_split_on_keeps_it_through_an_overwrite():
    """Excluded from the digest is not the same as omitted from the write.

    `dataset_split` is server-maintained, so counting it as drift would rewrite
    every row on every push -- but an update replaces the whole metadata
    document, and dropping the key destroys the split assignment. Neither the
    plan nor the verification re-read can see that happen: both strip the key
    on both sides, which is exactly what makes it worth a test rather than a
    read-through.
    """
    remote = _remote(*_all_rows())
    row_id = str(push_dataset.example_id("genuine-fit"))
    remote[row_id]["metadata"]["dataset_split"] = ["base"]
    remote[row_id]["outputs"]["expect_eligibility"] = "MODERATE"
    client = _fake_client(rows=remote)

    push_dataset.push(client)

    written = client.rows[row_id]["metadata"]
    assert written["dataset_split"] == ["base"]
    assert written["mirror_source"] == "evals/scout_cases.py"
    assert client.rows[row_id]["outputs"]["expect_eligibility"] == "STRONG"


def test_an_empty_dataset_somebody_else_made_is_not_adopted_in_silence():
    """An empty dataset carries no marker either.

    Short-circuiting the ownership check on emptiness is the wrong-name
    accident arriving through the one door it was built to hold: a colleague's
    freshly created dataset takes the four fixtures, gets the marker written
    into it, and passes the check forever after while every later push deletes
    whatever they add.
    """
    client = _fake_client(rows={})

    with pytest.raises(push_dataset.PushRefused, match="--adopt"):
        push_dataset.push(client, name="team-regressions")

    assert _writes(client) == []
    assert push_dataset.push(client, name="team-regressions", adopt=True).live


def test_a_delete_that_did_not_take_is_not_reported_as_a_failed_update():
    """Which operation did not take is the whole diagnostic value.

    Collapsed into one count, a residual that is entirely prunes printed "0
    example(s) still differ" and then pointed the reader at `update_examples` --
    a non-zero exit with a report that reads like success, naming the wrong
    call.
    """
    stray = "8f14e45f-ceea-4670-94ab-8f0f1e2f3a4b"
    remote = _remote(*_all_rows()) | {
        stray: {"inputs": {"brief": "left behind"}, "outputs": {}, "metadata": {}}
    }
    client = _fake_client(rows=remote)
    client.delete_examples = lambda example_ids, **kwargs: None

    result = push_dataset.push(client)
    push_dataset.render(result)

    assert result.converged is False
    assert result.residual is not None
    assert result.residual.prune == (stray,)


def test_the_report_names_the_dataset_that_was_actually_pushed_to(capsys):
    """The name and the URL have to agree, or neither is a rename signal.

    Printed from the module constant, `--dataset scratch` put
    `grant-writer-scout-cases` above a URL pointing somewhere else -- and in a
    dry run against a dataset that does not exist yet, above `(not created)`,
    asserting that the real mirror is missing.
    """
    client = _fake_client(rows={})

    push_dataset.render_plan(
        push_dataset.push(client, name="scratch", adopt=True, dry_run=True)
    )

    assert "scratch" in capsys.readouterr().out


def test_a_field_mirrored_into_two_groups_is_refused(monkeypatch):
    """A union cannot see a duplicate.

    Put `key` in `_OUTPUT_FIELDS` while it is still in `_METADATA_FIELDS` and
    the set still matches the dataclass, so the lossy-mirror guard passes while
    the field is written into two places and `case_from_example` resolves it by
    later-wins.
    """
    monkeypatch.setattr(
        push_dataset, "_OUTPUT_FIELDS", (*push_dataset._OUTPUT_FIELDS, "key")
    )

    with pytest.raises(push_dataset.PushRefused, match="more than once"):
        push_dataset.mirror_row(_case("genuine-fit"))


# ---- the dataset-driven evaluate() harness ----------------------------------
#
# `evaluate()` itself is not reachable here: it constructs a `Client`, resolves
# the dataset by name, and POSTs an experiment before the first row runs. What
# these cover is everything it calls -- the target, the two evaluators, and the
# metadata the experiment carries -- because that is where this harness can be
# wrong without saying so. An evaluator cannot fail *usefully*: LangSmith
# infers the feedback keys it would report an error under from literal
# `{"key": ...}` dicts in the source and, finding none, falls back to the
# function's own name -- so an uncaught exception posts one score-less row
# keyed `programmatic_scores`, on a table whose other columns are named for
# scorers. A row that scored nothing, a row whose model call died, and a row
# whose scorer raised are three different facts, and only the last of them
# arrives with a name on it.


def _example(key: str):
    """A dataset row as `evaluate` hands it to an evaluator.

    Built through `_row` and `_remote` rather than by hand, so it is the same
    JSON round trip the mirror tests use -- which is the one that turns
    `forbidden` back into a list. A hand-built namespace would hold the tuple
    `scout_cases.py` declares and never exercise `case_from_example`'s job of
    putting it back, since a list makes the frozen dataclass unhashable.
    """
    row = _row(key)
    return SimpleNamespace(id=row["id"], **_remote(row)[row["id"]])


def _run(output, error=None):
    """The `Run` an evaluator reads.

    The evaluators themselves touch only `outputs` and `error`. `id` is here
    because `run_evaluator(...).evaluate_run` stamps it onto the feedback it
    builds, and that is the path the shape case below runs -- a stand-in
    without one raises inside the SDK rather than in anything of ours. Fixed
    rather than minted, so no case here depends on a clock.
    """
    return SimpleNamespace(
        id=uuid.UUID("00000000-0000-5000-8000-000000000001"),
        outputs={"output": output},
        error=error,
    )


def _keys(results) -> list[str]:
    return [result.key for result in results["results"]]


def test_a_skipped_scorer_is_omitted_rather_than_posted_as_a_pass():
    """A case that declines to assert a dimension has not passed it.

    The same rule `post_scores` holds for feedback, arriving through the other
    runner: `genuine-fit` expects nothing unanswerable, so `gaps` skips, and a
    `1.0` sitting in its column would be indistinguishable from a scout that
    was asked and got it right. `leaky-brief` asserts less still -- no expected
    verdict, no disqualification, no marker -- and four columns is what an
    honest row for it looks like.

    Asserted against a non-empty result set on purpose. `{"results": []}` posts
    nothing, so "gaps is not among the keys" is satisfied by a batch that lost
    every score, and a subset assertion alone would pass on the bug.
    """
    scored = evaluate_scout.programmatic_scores(_run(GOOD), _example("genuine-fit"))
    assert len(scored["results"]) == 6
    assert "gaps" not in _keys(scored)

    sparse = evaluate_scout.programmatic_scores(_run(GOOD), _example("leaky-brief"))
    assert set(_keys(sparse)) == {"parses", "citations", "no-invention", "no-total"}


def test_an_example_reaches_the_scorers_as_the_case_it_was_pushed_from():
    """The `case_from_example` round trip, through the evaluator that needs it.

    `push_dataset` has carried that function with no production caller since it
    was written, on the argument that a dataset-driven run must reuse
    `score_programmatically` rather than grow a second scoring path. This is
    that caller, and the claim is equality: what the evaluator posts for a row
    is what the scorers say about the case the row was mirrored from, verdict
    for verdict and comment for comment.

    Anything less than equality would let the two paths agree on which columns
    exist while disagreeing about what is in them.
    """
    through = evaluate_scout.programmatic_scores(_run(GOOD), _example("genuine-fit"))
    direct = posting_scores(score_programmatically(_case("genuine-fit"), GOOD))

    assert [(r.key, r.score, r.comment) for r in through["results"]] == [
        (s.name, float(s.passed), s.detail or None) for s in direct
    ]


def test_the_target_hands_the_dataset_row_straight_to_the_shared_scout_call(
    monkeypatch,
):
    """One payload, two runners, and the signature that keeps them honest.

    `scout_target` splats the row in, so `push_dataset._INPUT_FIELDS` is the
    single contract on both ends: the mirror writes those three keys and
    `ask_scout` takes exactly those three keywords. Pinning the signature is
    what makes the splat safe -- rename a field on one side only and this fails
    here, rather than at the first billed run with a `TypeError` per row.
    """
    seen = {}

    def recorder(**kwargs):
        seen.update(kwargs)
        return "scored file"

    monkeypatch.setattr(evaluate_scout, "ask_scout", recorder)
    case = _case("genuine-fit")

    assert evaluate_scout.scout_target(_row("genuine-fit")["inputs"]) == {
        "output": "scored file"
    }
    assert seen == {
        "brief": case.brief,
        "candidate": case.candidate,
        "profile": case.profile,
    }

    taken = set(inspect.signature(run_scout.ask_scout).parameters) - {"config"}
    assert taken == set(push_dataset._INPUT_FIELDS)


def test_the_scout_payload_is_spelled_out_in_exactly_one_place():
    """Two copies of it would measure two prompts and report one number.

    The eval's whole claim is that `evaluate_scout` and `run_scout` put the
    same question to the model. A second f-string in the second runner keeps
    that true only until somebody edits one of them, and nothing downstream
    could tell: both runs parse, both score, and the tables differ for a reason
    no column carries.
    """
    assert "<opportunity-file>" in inspect.getsource(run_scout.ask_scout)
    assert "<opportunity-file>" not in inspect.getsource(evaluate_scout)

    # Every module in the package, found rather than listed, and read as text
    # rather than imported: a fourth runner added later is scanned with no edit
    # here, which a hard-coded tuple is exactly how you fail to do. Counted, so
    # a second copy *inside* `run_scout` fails too -- membership would call
    # that one place.
    sources = sorted(Path(run_scout.__file__).parent.glob("*.py"))
    assert {"run_scout.py", "evaluate_scout.py", "push_dataset.py"} <= {
        path.name for path in sources
    }, "the package moved and this scan found nothing to be right about"

    spelled = {
        path.name: path.read_text(encoding="utf-8").count("<org-profile-file>")
        for path in sources
    }
    assert {name: n for name, n in spelled.items() if n} == {"run_scout.py": 1}


def test_a_row_carrying_a_field_the_scout_call_does_not_take_is_refused():
    """`**inputs` is a contract, so something has to check it.

    Two of the three ways a stray field can arrive are safe on their own: an
    unknown name raises `TypeError` out of `ask_scout`, and a missing one
    raises too. `config` is the third, and it is not safe -- `ask_scout` takes
    it, `run_scout` passes it to label its calls, and a row carrying that key
    would be handed to `.invoke()` as runtime configuration. A hand-edited
    dataset row steering the model call it is supposed to be an input to.
    """
    row = _row("genuine-fit")["inputs"]

    for stray in ("config", "rubric"):
        with pytest.raises(TypeError, match="non-input field"):
            evaluate_scout.scout_target({**row, stray: "anything"})


def test_the_table_distinguishes_a_row_that_posted_nothing(capsys):
    """Three outcomes, and the one that must not read as a pass.

    `render` is the only thing a person actually looks at after a run, and the
    row that posted nothing is the one worth printing rather than skipping: it
    is either a case that asserted nothing or a model call that died, and a
    table that simply omits it reads as a table of everything that happened.

    The failure count is the other half. It counts posted verdicts that failed,
    so an empty row contributes nothing to it -- the same rule `posting_scores`
    holds one layer down, arriving where a human reads it.
    """
    rows = [
        {
            "example": SimpleNamespace(id="a", metadata={"key": "genuine-fit"}),
            "evaluation_results": {
                "results": [
                    EvaluationResult(key="parses", score=1.0, comment="82% fit"),
                    EvaluationResult(key="citations", score=0.0, comment="2 unfound"),
                ]
            },
        },
        {
            "example": SimpleNamespace(id="b", metadata={"key": "leaky-brief"}),
            "evaluation_results": {"results": []},
        },
        {
            "example": SimpleNamespace(id="row-c", metadata=None),
            "evaluation_results": {
                "results": [EvaluationResult(key="harness-error", score=0.0)]
            },
        },
    ]

    assert evaluate_scout.render(rows) == 2

    printed = capsys.readouterr().out
    assert "nothing posted" in printed
    assert "genuine-fit" in printed and "leaky-brief" in printed
    # No `key` in metadata, so the row is named by its id rather than dropped.
    assert "row-c" in printed


def test_a_target_that_never_answered_is_not_scored_as_a_bad_draft(monkeypatch):
    """A dead API must not arrive on screen as a prompt regression.

    `evaluate` catches everything the target raises and logs it, so a rate
    limit does not fail the row -- the evaluator is handed `run.error` and no
    output. Scored anyway that is `parses: 0.0` and a row of zeros beside it,
    with a number attached and nothing saying the model was never reached.

    The scorers are replaced with a stub that raises, so this cannot pass by
    them happening to return zeros: reaching them at all is the failure.

    The first row is the one that earned the `run.error` check its own line.
    Every other row here has no usable output, so the type-and-emptiness test
    alone would turn them all away and the error test would be dead code that
    reads like a guard. A row carrying *both* text and an error is what tells
    them apart -- a partially traced call, where something came back and the
    run still failed -- and scoring that is the same collapse as scoring
    nothing at all, arriving with enough output to look like a verdict.
    """

    def boom(*args, **kwargs):
        raise AssertionError("scored a row the model never answered")

    monkeypatch.setattr(evaluate_scout, "score_programmatically", boom)
    monkeypatch.setattr(evaluate_scout, "ask_judge", boom)
    example = _example("genuine-fit")

    for run in (
        _run(GOOD, error="RuntimeError('overloaded_error')"),
        _run(None, error="RuntimeError('overloaded_error')"),
        _run(None),
        _run(""),
        _run("   "),
        _run({"not": "a string"}),
    ):
        assert evaluate_scout.programmatic_scores(run, example) == {"results": []}
        assert evaluate_scout.grounding_judge(run, example) == {"results": []}


def test_an_evaluator_with_no_reference_row_posts_nothing(monkeypatch):
    """These score against the row, so without one there is nothing to say.

    The SDK's evaluator contract makes `example` optional, because an evaluator
    attached to a *project* runs on live traces and is handed no reference row.
    Both of these rebuild the case they score against from that row, so the
    absence has to be answered rather than indexed into -- otherwise the first
    person to attach one to a project gets an attribute error per trace, from
    inside a callback whose failures LangSmith logs and swallows.
    """

    def boom(*args, **kwargs):
        raise AssertionError("scored a run with no reference row")

    monkeypatch.setattr(evaluate_scout, "case_from_example", boom)

    assert evaluate_scout.programmatic_scores(_run(GOOD), None) == {"results": []}
    assert evaluate_scout.grounding_judge(_run(GOOD), None) == {"results": []}


def test_a_judge_reply_it_cannot_read_is_not_counted_as_a_pass(monkeypatch):
    """The judge's own malfunction is not a verdict about the scout.

    `read_judge_verdict` already marks an unreadable reply skipped; what this
    pins is that the skip survives the trip through the evaluator instead of
    being flattened into the `1.0` that `Score.passed` carries on it. That
    default is deliberate -- a skipped score must not read as a failure either
    -- which is exactly why it has to be dropped rather than posted.
    """
    example, run = _example("genuine-fit"), _run(GOOD)

    for reply, expected in (("hello?", []), ("PASS", [1.0]), ("FAIL\nquoted", [0.0])):
        monkeypatch.setattr(
            evaluate_scout, "ask_judge", lambda *a, r=reply, **k: read_judge_verdict(r)
        )
        posted = evaluate_scout.grounding_judge(run, example)
        assert [result.score for result in posted["results"]] == expected
        assert _keys(posted) == ["grounded"] * len(expected)


def test_an_evaluator_that_raises_says_so_rather_than_scoring_nothing(monkeypatch):
    """An eval that broke must not read like an eval that had nothing to say.

    LangSmith reports an evaluator's exception as one error result per feedback
    key it infers from the source, and it infers them from literal
    `{"key": "..."}` dicts. Both evaluators here build their results in a
    comprehension, so it finds none and falls back to the function's own name:
    an uncaught raise posts a score-less `programmatic_scores` row, which is
    not any scorer's name and carries no exception, no case, and no half of the
    harness. Catching it is what turns that into something a reader can act on.
    """

    def boom(*args, **kwargs):
        raise ValueError("row is not a case")

    monkeypatch.setattr(evaluate_scout, "case_from_example", boom)
    example, run = _example("genuine-fit"), _run(GOOD)

    for evaluator, where in (
        (evaluate_scout.programmatic_scores, "programmatic"),
        (evaluate_scout.grounding_judge, "judge"),
    ):
        posted = evaluator(run, example)
        assert _keys(posted) == ["harness-error"]
        result = posted["results"][0]
        assert result.score == 0.0
        assert where in (result.comment or "")
        assert "ValueError" in (result.comment or "")


def test_the_batched_results_are_a_shape_the_sdk_will_accept(monkeypatch):
    """Run the real coercion, because "nothing to post" is easy to get wrong.

    `None` and `{}` both raise inside `evaluate`, so the empty batch has to be
    spelled `{"results": []}` and cannot be defaulted into. A refactor that
    reaches for the obvious `return None` fails here rather than on the first
    billed run, where four rows are already paid for.

    The judge is stubbed, and that is not tidiness. Left real, this case is the
    one thing in an offline-by-contract suite that opens a socket -- and worse,
    it passes either way: `grounding_judge` catches the connection error and
    returns a `harness-error` batch, which is still a list, so the success
    shape it exists to coerce is never reached. A billed call and a vacuous
    assertion, from the same missing line.
    """
    monkeypatch.setattr(
        evaluate_scout, "ask_judge", lambda *a, **k: read_judge_verdict("PASS")
    )
    example = _example("genuine-fit")

    for evaluator, populated in (
        (evaluate_scout.programmatic_scores, 6),
        (evaluate_scout.grounding_judge, 1),
    ):
        scored = run_evaluator(evaluator).evaluate_run(_run(GOOD), example)
        assert len(scored["results"]) == populated
        assert all(result.key for result in scored["results"])

        empty = run_evaluator(evaluator).evaluate_run(_run(None, error="boom"), example)
        assert empty["results"] == []


def test_the_experiment_records_which_prompt_it_measured():
    """The dataset holds the fixtures; only the experiment can hold the prompt.

    Deliberately so -- a prompt baked into the rows would have to be pushed
    before it could be measured, and the mirror would then own a copy of the
    product. The cost is that an experiment carries no evidence of what it
    tested unless it says: two tables of numbers, no way to tell which prompt
    produced which, and a regression that cannot be attributed to the edit that
    caused it.
    """
    labelled = evaluate_scout.experiment_metadata(dataset="scratch", judge=True)

    assert (
        labelled["scout_prompt_sha"]
        == (hashlib.sha256(SCOUT_PROMPT.encode("utf-8")).hexdigest()[:12])
    )
    # Recomputed, not merely asserted different from the scout's. Unequal is
    # satisfied by the digest of anything at all, including a copy-paste that
    # hashed the wrong constant.
    assert (
        labelled["judge_prompt_sha"]
        == (hashlib.sha256(JUDGE_PROMPT.encode("utf-8")).hexdigest()[:12])
    )
    # The dataset actually run against, not the module default: `--dataset`
    # exists, and a constant here labels every scratch run as the mirror.
    assert labelled["dataset"] == "scratch"

    unjudged = evaluate_scout.experiment_metadata(
        dataset=push_dataset.DATASET_NAME, judge=False
    )
    assert unjudged["judge"] is False
    assert unjudged["judge_model"] is None and unjudged["judge_prompt_sha"] is None
    assert unjudged["scout_prompt_sha"] == labelled["scout_prompt_sha"]
    assert unjudged["dataset"] == push_dataset.DATASET_NAME


def test_the_run_needs_both_credentials_before_it_spends_either(monkeypatch, capsys):
    """Two guards, each pinned alone, and a run that gets past both.

    This is the only runner that needs both: it calls a model per row and
    writes an experiment to a workspace. Both refuse before `evaluate`, which
    POSTs a project ahead of the first billed call -- so a missing credential
    costs nothing rather than costing four model calls and then failing to
    record them.

    Every case gives the *other* credential, and reads the message back. Two
    bare `main() == 1` assertions are satisfied by a runner that refuses
    everything, which is the shape a guard collapses into when it is wrong:
    `if True:` passes them both, and so does `and` where `or` is written.

    The last case is the one that costs the others their vacuity. Truthiness
    rather than `is None` because `conftest.py` blanks these names rather than
    popping them, and `or` rather than `and` because LangSmith itself falls
    through an empty value to the `LANGCHAIN_` spelling -- so a developer with
    only that one exported must get a run, not a lecture about a key they set.
    """
    started = []

    class _Results(list):
        """What `evaluate` returns: rows to iterate, and a name to print."""

        experiment_name = "scout-prompt-0000"

    def fake_evaluate(*args, **kwargs):
        started.append(kwargs)
        return _Results()

    monkeypatch.setattr(evaluate_scout, "evaluate", fake_evaluate)
    monkeypatch.setattr("sys.argv", ["evals.evaluate_scout"])

    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert evaluate_scout.main() == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert started == []

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("LANGSMITH_API_KEY", "")
    monkeypatch.setenv("LANGCHAIN_API_KEY", "")
    assert evaluate_scout.main() == 1
    assert "LANGSMITH_API_KEY" in capsys.readouterr().err
    assert started == []

    # The positive case, and the `LANGCHAIN_` spelling at the same time: this
    # is the run a wrongly written guard refuses.
    monkeypatch.setenv("LANGCHAIN_API_KEY", "lc-test-key")
    assert evaluate_scout.main() == 0
    assert len(started) == 1
    assert started[0]["data"] == push_dataset.DATASET_NAME


def test_the_harness_builds_no_client_and_calls_no_loader_of_its_own(monkeypatch):
    """What keeps this module importable under a suite that blanks the key.

    Pins invariant 19.
    Named for what a reload can actually see. Importing this module *does*
    read the environment file -- `grant_writer.config` does it, through
    `run_scout` -- and that is the arrangement wanted: one loader on the import
    path, already covered where it lives. What must not appear is a second call
    here, which would be a second thing to keep honest for no credential
    gained. The absence is asserted rather than left to read as an oversight.

    The loader is patched at its source and before the reload, for the reason
    the mirror's own case gives: a module-scope call binds whatever `dotenv`
    holds when the import line runs, and that is the only stub it can hit.
    """

    def boom(*args, **kwargs):
        raise AssertionError("a client was constructed at import")

    def boom_loader(*args, **kwargs):
        raise AssertionError("an environment file was read at import")

    monkeypatch.setattr("langsmith.Client", boom)
    monkeypatch.setattr("dotenv.load_dotenv", boom_loader)
    monkeypatch.setenv("LANGSMITH_API_KEY", "")

    try:
        reloaded = importlib.reload(evaluate_scout)
        assert not hasattr(reloaded, "load_dotenv")
        assert reloaded.experiment_metadata(dataset="d", judge=False)["eval"] == (
            "scout-prompt"
        )
    finally:
        # Undone before the restoring reload, exactly as the mirror's case is:
        # a failure here would otherwise leave every later case importing a
        # module that never finished re-executing.
        monkeypatch.undo()
        importlib.reload(evaluate_scout)
