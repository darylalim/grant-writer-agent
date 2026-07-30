"""Wiring tests. No model API calls, so these run offline and in CI.

They cover the things that fail *silently* in a Deep Agents setup: a subagent
that quietly lost its skills, a permission rule ordered so the catch-all deny
shadows the allow, or an interrupt configured without the checkpointer that
makes it work.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from deepagents.middleware.filesystem import _check_fs_permission, supports_execution

from grant_writer.agent import build_agent, build_discovery_agent
from grant_writer.backends import (
    build_backend,
    build_permissions,
    compliance_permissions,
    discovery_permissions,
    scouting_permissions,
)
from grant_writer.config import (
    CONTENT_DIRS,
    DEFAULT_MODELS,
    MAX_OUTPUT_TOKENS,
    PROJECT_ROOT,
    Settings,
    build_model,
    discovery_thread_id,
)
from grant_writer.subagents import build_discovery_subagents, build_subagents
from grant_writer.tools import (
    _DISCOVERY_WRITE_DIRS,
    _DRAFTING_WRITE_DIRS,
    _format_opportunity,
    _format_search_hits,
    _parse_page_spec,
    _resolve_output_path,
    extract_pdf_text,
    measure_text,
)


@pytest.mark.parametrize("profile", ["local", "server"])
def test_graph_builds(profile):
    assert build_agent(Settings(backend_profile=profile)) is not None


@pytest.mark.parametrize(
    "tool_name",
    [
        "task",
        "write_todos",
        "read_file",
        "write_file",
        "edit_file",
        "ls",
        "glob",
        "grep",
        "extract_pdf_text",
        "measure_text",
    ],
)
def test_expected_tools_registered(tool_name):
    graph = build_agent(Settings())
    registry = graph.nodes["tools"].bound.tools_by_name
    assert tool_name in registry


def test_subagent_roster():
    names = [s["name"] for s in build_subagents()]
    assert names == ["funder-researcher", "section-drafter", "compliance-checker"]


def test_drafting_subagents_receive_skills_explicitly():
    """Custom subagents do NOT inherit skills from the parent.

    Without this, the drafter still answers -- just generically, with none of
    the funder-specific guidance. That is the failure this test exists for.
    """
    for sub in build_subagents():
        if sub["name"] in {"section-drafter", "compliance-checker"}:
            assert sub.get("skills") == ["/skills/"], sub["name"]


@pytest.mark.parametrize(
    ("operation", "path", "expected"),
    [
        ("write", "/applications/nsf-26/sections/need.md", "allow"),
        ("write", "/memories/org/AGENTS.md", "allow"),
        ("write", "/opportunities/rural-2026/scored/x.md", "allow"),
        ("write", "/opportunities/rural-2026/candidates/x.md", "allow"),
        ("write", "/skills/statement-of-need/SKILL.md", "deny"),
        ("write", "/src/grant_writer/agent.py", "deny"),
        ("write", "/pyproject.toml", "deny"),
        ("read", "/skills/statement-of-need/SKILL.md", "allow"),
        ("read", "/src/grant_writer/agent.py", "allow"),
    ],
)
def test_main_agent_permissions(operation, path, expected):
    rules = build_permissions(Settings(approve_final=False))
    assert _check_fs_permission(rules, operation, path) == expected


def test_approve_final_interrupts_only_final_writes():
    rules = build_permissions(Settings(approve_final=True))
    assert (
        _check_fs_permission(rules, "write", "/applications/x/final/proposal.md")
        == "interrupt"
    )
    # Scratch work must stay friction-free, or approvals get rubber-stamped.
    assert (
        _check_fs_permission(rules, "write", "/applications/x/sections/need.md")
        == "allow"
    )


def test_approve_final_wires_human_in_the_loop():
    plain = set(build_agent(Settings(approve_final=False)).get_graph().nodes)
    gated = set(build_agent(Settings(approve_final=True)).get_graph().nodes)
    assert any("HumanInTheLoop" in node for node in gated - plain)


def test_reviewer_cannot_edit_what_it_reviews():
    rules = compliance_permissions()
    assert (
        _check_fs_permission(rules, "write", "/applications/x/review/report.md")
        == "allow"
    )
    assert (
        _check_fs_permission(rules, "write", "/applications/x/sections/need.md")
        == "deny"
    )
    assert (
        _check_fs_permission(rules, "read", "/applications/x/sections/need.md")
        == "allow"
    )


def test_execute_tool_is_not_a_permission_bypass():
    """`execute` is registered but inert on FilesystemBackend.

    It is offered to the model, so if this ever flips to True the write
    permissions above stop being a real boundary -- the agent could shell out.
    """
    backend = build_backend(Settings(backend_profile="local"))
    assert not callable(backend), "local profile should build a concrete backend"
    assert supports_execution(backend) is False


def test_measure_text_counts_accurately():
    out = measure_text.invoke({"text": "one two three four five"})
    assert "words=5" in out
    assert "est_pages_single_spaced" in out


def test_extract_pdf_text_reports_missing_file():
    assert extract_pdf_text.invoke({"pdf_path": "/nope/missing.pdf"}).startswith(
        "Error: no such file"
    )


@pytest.mark.parametrize(
    ("out_path", "allowed"),
    [
        ("/applications/x/rfp.md", _DRAFTING_WRITE_DIRS),
        ("applications/x/rfp.md", _DRAFTING_WRITE_DIRS),
        ("/memories/org/AGENTS.md", _DRAFTING_WRITE_DIRS),
        ("/opportunities/rural-2026/candidates/353201.md", _DISCOVERY_WRITE_DIRS),
        ("opportunities/rural-2026/scored/353201.md", _DISCOVERY_WRITE_DIRS),
    ],
)
def test_output_paths_inside_a_tools_own_dirs_are_allowed(out_path, allowed):
    assert _resolve_output_path(out_path, allowed).is_relative_to(PROJECT_ROOT)


@pytest.mark.parametrize(
    "out_path",
    [
        "/applications/nsf-26/final/proposal.md",
        "/applications/nsf-26/sections/need.md",
        "/memories/org/AGENTS.md",
    ],
)
def test_the_discovery_tool_cannot_write_into_an_application(out_path):
    """The bypass that `discovery_permissions` alone does not close.

    `fetch_grants_gov_opportunity` writes to real disk, so the permission rules
    never run for it -- and those rules are the only thing that denies
    `/applications/**` to the discovery graph, and the only thing that
    interrupts a write under `final/`. Handed the project-wide `CONTENT_DIRS`,
    one `out_path="/applications/x/final/proposal.md"` therefore overwrote a
    submission-bound file with grants.gov synopsis text: no approval prompt, no
    error, and both the permission test and the README still green, because
    neither exercises the writer.

    A tool gets its own graph's directories, not the union of everyone's.
    """
    with pytest.raises(ValueError, match=re.escape("refusing to write outside")):
        _resolve_output_path(out_path, _DISCOVERY_WRITE_DIRS)


def test_the_drafting_tool_cannot_write_into_a_scan():
    """The same rule in the other direction, so neither set is a superset.

    `extract_pdf_text` archives a solicitation beside the drafts; a scan
    directory is not its business, and letting it write there would put a tool
    the discovery graph does not carry into the tree that graph owns.
    """
    with pytest.raises(ValueError, match=re.escape("refusing to write outside")):
        _resolve_output_path("/opportunities/s/candidates/x.md", _DRAFTING_WRITE_DIRS)


@pytest.mark.parametrize(
    "out_path",
    [
        "/src/grant_writer/agent.py",
        "/skills/statement-of-need/SKILL.md",
        "/pyproject.toml",
        "/applications/../src/agent.py",
        "/opportunities/../src/agent.py",
        "/etc/passwd",
    ],
)
def test_output_paths_outside_content_dirs_are_refused(out_path):
    """The tools that write to real disk bypass FilesystemPermission.

    They therefore have to enforce the same boundary themselves, or they become
    a way around every write rule tested above.

    Match the refusal message literally. A looser pattern would keep passing if
    the guard were replaced by some *other* ValueError -- a path-parsing crash,
    say -- and report a boundary that is no longer enforced as still holding.

    The message names the *caller's* directories, not the project's. A tool
    permitted only `/opportunities/` must say so when it refuses, or the
    message invites a retry into a directory it will refuse again -- and a
    message built from the union would offer `/applications/` to a tool that,
    since the write-bypass fix, may not touch it.
    """
    with pytest.raises(
        ValueError,
        match=re.escape("refusing to write outside /applications/ or /memories/"),
    ):
        _resolve_output_path(out_path, _DRAFTING_WRITE_DIRS)


def test_content_dirs_agrees_with_the_settings_paths():
    """The constant and the `Settings` properties must name the same places.

    Nothing at runtime checks that `CONTENT_DIRS` says "opportunities" and
    `Settings.opportunities_path` ends in `opportunities/`. Let those drift and
    the permission rule allows a directory nothing writes to, while the
    directory that is written to matches no rule -- and "no match means allow"
    (invariant 1) makes that failure silent in the permissive direction.
    """
    settings = Settings()
    assert settings.applications_path.name in CONTENT_DIRS
    assert settings.opportunities_path.name in CONTENT_DIRS
    assert settings.memory_path.parent.parent.name in CONTENT_DIRS


@pytest.mark.parametrize(
    ("spec", "total", "expected"),
    [("1-3,7", 10, [0, 1, 2, 6]), ("", 3, [0, 1, 2]), ("9-12", 10, [8, 9])],
)
def test_page_spec_parsing(spec, total, expected):
    assert _parse_page_spec(spec, total) == expected


def test_skills_have_valid_frontmatter():
    """Skills without frontmatter are ignored, silently."""
    skills = sorted(Path(PROJECT_ROOT, "skills").glob("*/SKILL.md"))
    assert len(skills) >= 5
    for skill in skills:
        lines = skill.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "---", skill
        head = lines[:6]
        assert any(line.startswith("name:") for line in head), skill
        assert any(line.startswith("description:") for line in head), skill


@pytest.mark.parametrize(("role", "spec"), sorted(DEFAULT_MODELS.items()))
def test_default_model_resolves_to_a_usable_output_ceiling(role, spec):
    """Every shipped model id must resolve to a real output ceiling.

    Left to the provider default, an id the installed `langchain-anthropic` does
    not recognize silently gets 4096 `max_tokens` -- valid id, HTTP 200, no
    exception, and a narrative that stops mid-sentence. `build_model` sets the
    ceiling explicitly so that cannot happen; this pins that it keeps doing so,
    and that both halves of each spec still resolve.

    Parametrized over `DEFAULT_MODELS` rather than the module-level constants,
    which `GRANT_WRITER_*_MODEL` can redirect -- otherwise a developer with an
    override set would never test what the project ships.
    """
    max_tokens = getattr(build_model(spec), "max_tokens", None)
    assert max_tokens is not None, f"{role}: {spec!r} exposes no max_tokens"
    assert max_tokens >= MAX_OUTPUT_TOKENS, (
        f"{role}: {spec!r} resolves to max_tokens={max_tokens}, below the "
        f"{MAX_OUTPUT_TOKENS} a full section needs."
    )


# ---- the discovery graph ----------------------------------------------------


@pytest.mark.parametrize("profile", ["local", "server"])
def test_discovery_graph_builds(profile):
    assert build_discovery_agent(Settings(backend_profile=profile)) is not None


@pytest.mark.parametrize(
    "tool_name",
    ["task", "write_todos", "read_file", "write_file", "ls"],
)
def test_discovery_graph_has_the_harness_tools(tool_name):
    registry = build_discovery_agent(Settings()).nodes["tools"].bound.tools_by_name
    assert tool_name in registry


@pytest.mark.parametrize(
    "tool_name", ["search_grants_gov", "fetch_grants_gov_opportunity"]
)
def test_the_grants_gov_tools_reach_the_discovery_graph(tool_name):
    """Registered on the discovery orchestrator, which does the sweeping.

    Without them a scan degrades to whatever web search turns up, silently:
    the agent still answers, just from prose instead of stated fields.
    """
    registry = build_discovery_agent(Settings()).nodes["tools"].bound.tools_by_name
    assert tool_name in registry


def test_discovery_subagent_roster():
    assert [s["name"] for s in build_discovery_subagents()] == ["opportunity-scout"]


def test_the_two_rosters_do_not_overlap():
    """Neither graph can delegate into the other's work.

    This is what makes the split structural rather than a rule in a prompt: the
    drafting orchestrator cannot reach a scorer with no application to score,
    and the discovery orchestrator cannot reach a drafter with nothing to
    draft. Merge the rosters and both become possible, with a prompt as the
    only thing preventing it.
    """
    drafting = {s["name"] for s in build_subagents()}
    discovery = {s["name"] for s in build_discovery_subagents()}
    assert drafting.isdisjoint(discovery)


def test_the_scout_is_deliberately_given_no_skills():
    """The inverse of `test_drafting_subagents_receive_skills_explicitly`.

    Invariant 3 says custom subagents do not inherit skills, so a missing
    `skills` key usually is the bug. Here it is the intent: all six guides
    under /skills/ are about drafting a section, and none has anything to say
    about whether to apply at all. Pinned so the next reader, seeing what looks
    like invariant 3 being violated, finds this instead of "fixing" it.
    """
    for sub in build_discovery_subagents():
        assert sub.get("skills") is None, sub["name"]


def test_the_scout_cannot_write_outside_a_scan():
    """A scorer that could edit the drafts is not a scorer.

    Same argument as `test_reviewer_cannot_edit_what_it_reviews`, and the same
    shape of rule -- specific allow, then catch-all deny.
    """
    rules = scouting_permissions()
    assert (
        _check_fs_permission(rules, "write", "/opportunities/s/scored/a.md") == "allow"
    )
    assert (
        _check_fs_permission(rules, "write", "/applications/x/sections/need.md")
        == "deny"
    )
    assert _check_fs_permission(rules, "write", "/memories/org/AGENTS.md") == "deny"
    # It has to read the profile it scores against, and reads are unrestricted.
    assert _check_fs_permission(rules, "read", "/memories/org/AGENTS.md") == "allow"


def test_discovery_subagents_get_an_explicit_output_ceiling():
    for sub in build_discovery_subagents():
        assert getattr(sub["model"], "max_tokens", None) == MAX_OUTPUT_TOKENS, sub[
            "name"
        ]


@pytest.mark.parametrize(
    "path",
    [
        # The one that actually matters. `build_permissions` interrupts here,
        # and the discovery orchestrator has `write_file` like any other -- so
        # sharing that rule set let a scan park after all. `discovery_permissions`
        # denies the write instead, which is refusal rather than a pause.
        "/applications/nsf-26/final/proposal.md",
        "/applications/nsf-26/sections/need.md",
        "/opportunities/s/scored/a.md",
        "/opportunities/s/candidates/a.md",
        # A directory that merely looks like the protected one.
        "/opportunities/s/final/a.md",
        "/memories/org/AGENTS.md",
        "/src/grant_writer/agent.py",
    ],
)
def test_a_scan_can_never_park_on_an_approval(path):
    """The property the whole discovery design rests on.

    Both frontends start a scan without the `parked_state` check that guards a
    drafting turn, on the grounds that a scan has no pending write to abandon.
    That is only true if *no* path can interrupt on this graph -- and it very
    nearly was not: `build_permissions` interrupts on
    `/applications/*/final/**`, the discovery orchestrator has `write_file`,
    and `WORKSPACE_CONVENTIONS` describes that directory to it. One stray write
    there would have parked a thread nothing was watching, and the next scan
    would have discarded the pending write silently -- invariant 11's failure,
    reached through the graph built to be exempt from it.

    So this asserts the absence of `interrupt` across the whole surface, at the
    setting that produces it elsewhere. Anything but allow-or-deny here means
    the frontends' missing guard has become a hole.
    """
    verdict = _check_fs_permission(discovery_permissions(), "write", path)
    assert verdict != "interrupt", path
    assert verdict in {"allow", "deny"}, path


def test_the_discovery_graph_cannot_write_into_an_application():
    """Writes are confined to its own tree plus the org profile.

    `/memories/` stays open because the shared conventions text invites the
    agent to record durable facts there and no rule interrupts it.
    `/applications/` is denied outright: a scan has no business editing drafts,
    and denial is what keeps the no-interrupt guarantee above true.
    """
    rules = discovery_permissions()
    assert (
        _check_fs_permission(rules, "write", "/opportunities/s/scored/a.md") == "allow"
    )
    assert _check_fs_permission(rules, "write", "/memories/org/AGENTS.md") == "allow"
    assert _check_fs_permission(rules, "write", "/applications/x/final/p.md") == "deny"
    assert (
        _check_fs_permission(rules, "write", "/applications/x/sections/n.md") == "deny"
    )
    # Reads stay open -- it has to be able to look at an existing application.
    assert _check_fs_permission(rules, "read", "/applications/x/final/p.md") == "allow"


def test_a_scan_thread_id_cannot_collide_with_an_application_id():
    """Both are `_SAFE_ID`-shaped and share one checkpoint file.

    Reusing a name across `discover` and `draft` is the expected workflow, so
    an unprefixed thread id would put two structurally different graphs on the
    same checkpoint row -- each resuming the other's conversation, with nothing
    raised. The separator is the guarantee: `_SAFE_ID` forbids it, so no
    application id can ever spell one of these.
    """
    from grant_writer.config import _SAFE_ID

    thread = discovery_thread_id("rural-2026")
    assert thread != "rural-2026"
    assert not _SAFE_ID.match(thread)


# ---- the grants.gov tools ---------------------------------------------------


def test_search_hits_are_formatted_from_the_fields_search_returns():
    """`search2` returns `agency`; only the detail endpoint returns
    `agencyName`. Reading the detail spelling off a search hit is a silent `?`
    on every row of the sweep, not an error -- which is why the formatter is
    split out and pinned against a captured shape rather than only exercised
    through a live call the offline suite cannot make."""
    out = _format_search_hits(
        {
            "hitCount": 2,
            "oppHits": [
                {
                    "id": "334326",
                    "number": "21-595",
                    "title": "Tribal Colleges Program",
                    "agency": "U.S. National Science Foundation",
                    "agencyCode": "NSF",
                    "closeDate": "09/01/2026",
                    "oppStatus": "posted",
                }
            ],
        }
    )
    assert "334326" in out
    assert "21-595" in out
    assert "U.S. National Science Foundation" in out
    assert "09/01/2026" in out
    assert "?" not in out


def test_an_empty_search_says_so_rather_than_returning_nothing():
    out = _format_search_hits({"hitCount": 0, "oppHits": []})
    assert out.startswith("No grants.gov opportunities matched")


def test_the_opportunity_formatter_leads_with_what_a_fit_score_needs():
    """Eligibility, money, and the deadline decide whether an opportunity is
    worth reading about at all, so they precede the prose. Field names pinned
    against a captured response: every access is `.get`-defended, so a rename
    upstream degrades to `?` rather than raising, and nothing else would
    notice."""
    out = _format_opportunity(
        {
            "opportunityTitle": "Tribal Colleges Program",
            "opportunityNumber": "21-595",
            "synopsis": {
                "agencyName": "U.S. National Science Foundation",
                "responseDate": "Sep 01, 2026",
                # As grants.gov actually returns them: thousands separators,
                # no currency symbol. Verified against the live endpoint.
                "awardCeilingFormatted": "3,500,000",
                "awardFloorFormatted": "100,000",
                "numberOfAwards": "55",
                "costSharing": False,
                "applicantTypes": [{"description": "Others"}],
                "applicantEligibilityDesc": "Federally recognized Tribal Colleges",
                "fundingDescLinkUrl": "https://example.invalid/nsf21595",
                "synopsisDesc": "Improve STEM education.",
            },
        }
    )
    assert "U.S. National Science Foundation" in out
    # The unit is stated by the formatter, not left for the scout to supply:
    # grants.gov returns "3,500,000" with no symbol, and a citation reading
    # "$100,000" against a source that never said "$" is an invented figure.
    assert "USD 100,000 to USD 3,500,000" in out
    assert "Federally recognized Tribal Colleges" in out
    assert "https://example.invalid/nsf21595" in out
    assert out.index("## Eligibility") < out.index("## Synopsis")


def test_an_opportunity_that_does_not_exist_is_an_error_not_an_empty_document(
    monkeypatch,
):
    """grants.gov reports a missing record as a *success*.

    Verified live: a bogus opportunity id returns HTTP 200 with
    `errorcode: 0` and `msg: "Webservice Succeeds"`, putting the real failure
    only in `data.errorMessages`. Checking `errorcode` alone passes that
    straight to the formatter, which renders a complete, plausible, entirely
    empty document -- and the scout then archives it as a candidate and scores
    it. A funding opportunity that does not exist reaches the shortlist with
    citations quoting nothing: the exact fabrication this system refuses
    everywhere else, arriving by the one route where neither the model nor the
    prompt is at fault.
    """
    from grant_writer.tools import _grants_gov_post

    class _MissingRecord:
        """The live response shape, captured. No network: the suite is offline
        by design, so the contract is pinned as a fixture and re-checked by
        hand when the endpoint changes."""

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict:
            return {
                "errorcode": 0,
                "msg": "Webservice Succeeds",
                "data": {
                    "errorMessages": ["There is no record found for your search."]
                },
            }

    monkeypatch.setattr(
        "grant_writer.tools.httpx.post", lambda *_a, **_k: _MissingRecord()
    )
    result = _grants_gov_post("fetchOpportunity", {"opportunityId": 999999999})

    assert isinstance(result, str), "an absent record was passed on as data"
    assert result.startswith("Error:")
    assert "no record found" in result


def test_an_award_floor_of_zero_does_not_erase_the_whole_range():
    """A stated floor of 0 is a fact, and `or` reads it as absence.

    grants.gov reports a zero floor routinely. Testing truthiness dropped the
    entire range -- ceiling included -- so a $3.5M opportunity archived as
    "not stated", and the scout scored `award-size-fit` against a document that
    had withheld a number the funder actually published.
    """
    out = _format_opportunity(
        {"synopsis": {"awardCeilingFormatted": "3,500,000", "awardFloor": 0}}
    )
    assert "USD 0 to USD 3,500,000" in out

    # A ceiling with no floor at all is still worth stating.
    ceiling_only = _format_opportunity({"synopsis": {"awardCeiling": "500000"}})
    assert "up to USD 500000" in ceiling_only
    assert "not stated" not in ceiling_only.split("Award range:")[1].split("\n")[0]


@pytest.mark.parametrize(
    ("synopsis", "expected"),
    [
        ({}, "not stated"),
        ({"costSharing": False}, "no"),
        ({"costSharing": True}, "yes"),
    ],
)
def test_cost_sharing_never_renders_a_raw_none(synopsis, expected):
    """`{None}` formats to the literal "None", which reads as "not required".

    That is an affirmative claim the funder never made, in the one file the
    gating eligibility criterion is scored from -- and cost-sharing capacity is
    named in that criterion's own question. Every neighbouring line already
    said "not stated"; this was the only one that did not.
    """
    line = next(
        ln
        for ln in _format_opportunity({"synopsis": synopsis}).splitlines()
        if ln.startswith("- Cost sharing required:")
    )
    assert line == f"- Cost sharing required: {expected}"


def test_a_non_object_json_body_is_an_error_not_an_exception(monkeypatch):
    """Valid JSON need not be an object, and `.get` on a list raises.

    A captive portal or an intercepting proxy answers 200 with `[]`, which
    clears both `raise_for_status` and `response.json()` -- and the raise then
    escapes this function's "never raises" contract, killing the sweep instead
    of telling the model to try web search.
    """

    class _ListBody:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> list:
            return []

    from grant_writer.tools import _grants_gov_post

    monkeypatch.setattr("grant_writer.tools.httpx.post", lambda *_a, **_k: _ListBody())
    result = _grants_gov_post("search2", {})

    assert isinstance(result, str)
    assert result.startswith("Error:")


@pytest.mark.parametrize("bad_id", ["²", "353201²", "①", "3.5", "", "abc"])
def test_a_non_decimal_opportunity_id_is_refused_not_raised(bad_id):
    """`isdigit` admits superscripts that `int()` then rejects.

    `"²".isdigit()` is True, so the helpful guard was skipped and the
    conversion raised -- handing the model a framework-level traceback in place
    of the message this branch exists to give it.
    """
    from grant_writer.tools import fetch_grants_gov_opportunity

    result = fetch_grants_gov_opportunity.invoke({"opportunity_id": bad_id})
    assert result.startswith("Error: opportunity_id must be the numeric id")


def test_a_missing_synopsis_degrades_rather_than_raising():
    """A tool that raises reaches the model as a framework error it cannot act
    on; one that returns text can be read and worked around."""
    out = _format_opportunity({})
    assert "(untitled)" in out
    assert "(none stated)" in out


def test_subagents_get_an_explicit_output_ceiling():
    """The ceiling has to reach the subagents too -- `section-drafter` is what
    actually writes the narrative, so a 4096 cap there is the truncation this
    guards against, whatever the orchestrator was built with."""
    for sub in build_subagents(Settings()):
        max_tokens = getattr(sub["model"], "max_tokens", None)
        assert max_tokens == MAX_OUTPUT_TOKENS, sub["name"]
