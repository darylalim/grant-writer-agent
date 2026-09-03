"""Offline tests for the eval scorers.

An eval whose scoring is wrong is worse than no eval: it reports a prompt
regression as green, and it does so with the authority of a number. So the
scorers get the same treatment as the rest of the package -- canned scout output
in, `Score` objects out, no model and no network.

These live in `tests/` while the eval itself lives in `evals/` and is never
collected here, because these cost nothing and must run on every push. The
distinction is the credential: `evals/run_scout.py` needs a real key, and
CLAUDE.md is explicit that a test needing one is a bug in the suite.

Each case below is a scout output that is *wrong in one specific way*, checked
against the scorer that has to notice. A scorer is only worth having if it fails
on something, and the cheapest way to be sure of that is to hand it the failure.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from langsmith.run_trees import RunTree

from evals import run_scout
from evals.scorers import (
    JUDGE_PROMPT,
    Score,
    build_judge_payload,
    read_judge_verdict,
    score_programmatically,
)
from evals.scout_cases import CASES, ScoutCase

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
