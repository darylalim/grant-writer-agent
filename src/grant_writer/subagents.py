"""Subagent definitions.

Three specialists, split along the lines where context isolation actually pays:

- research burns a lot of tokens on search results the drafter never needs,
- drafting needs the funder-specific style guides loaded,
- compliance must judge the drafts without the drafter's rationalizations in
  context, and must not be able to edit what it reviews.

Note that custom subagents do **not** inherit `skills` from the parent, so the
drafter and reviewer are handed the skills directory explicitly. Forgetting
this is a silent failure: the agent still answers, just generically.

Every subagent here is therefore built through `_subagent`, which takes
`skills` as a required keyword argument. That is the point of the function:
a dict literal lets the key be forgotten, and a forgotten key and a
deliberately-withheld one look identical on the page. Required, the two are
the same decision written two ways -- `skills=[SKILLS_DIR]` or `skills=None`
-- and neither can be reached by accident.
"""

from __future__ import annotations

from typing import Any

from deepagents import FilesystemPermission, SubAgent

from grant_writer.backends import compliance_permissions, scouting_permissions
from grant_writer.config import (
    COMPLIANCE_MODEL,
    DISCOVERY_MODEL,
    DRAFTING_MODEL,
    RESEARCH_MODEL,
    SKILLS_DIR,
    Settings,
    build_model,
)
from grant_writer.prompts import (
    COMPLIANCE_PROMPT,
    DRAFTER_PROMPT,
    RESEARCHER_PROMPT,
    SCOUT_PROMPT,
)
from grant_writer.tools import build_search_tool, measure_text


def _subagent(
    *,
    name: str,
    description: str,
    system_prompt: str,
    model_spec: str,
    skills: list[str] | None,
    tools: list[Any] | None = None,
    permissions: list[FilesystemPermission] | None = None,
) -> SubAgent:
    """Assemble one subagent, with `skills` a decision rather than a default.

    `skills` is keyword-only and has **no default**, and that is the whole
    reason this function exists. Custom subagents do not inherit skills from
    the parent (invariant 3), so leaving the key off is a real bug with no
    symptom: the drafter still answers, just generically, with none of the
    funder-specific guidance. As a plain dict literal that omission looked
    exactly like the scout's *deliberate* refusal of the same key
    (invariant 17) -- same syntax, opposite meanings, which is why invariant 17
    had to exist as prose telling the next reader not to "fix" it.

    Required, the two collapse into one typed decision made at every call site.
    Forgetting it on the drafter is a `TypeError` before any model is reached;
    `skills=None` on the scout is a sentence the caller wrote on purpose.

    The key is dropped when `skills is None` rather than stored as `None`:
    `SubAgent` declares it `NotRequired[list[str]]`, and deepagents reads it
    with a truthiness check (`graph.py`: `if spec.get("skills")`), so an absent
    key and a `None` are already the same thing to the harness. Absent is the
    one that keeps the TypedDict honest.
    """
    subagent: SubAgent = {
        "name": name,
        "description": description,
        "system_prompt": system_prompt,
        "model": build_model(model_spec),
        "tools": tools or [],
    }
    if skills is not None:
        subagent["skills"] = skills
    if permissions is not None:
        subagent["permissions"] = permissions
    return subagent


def build_subagents(settings: Settings | None = None) -> list[SubAgent]:
    """Assemble the subagent roster for the current environment."""
    settings = settings or Settings()
    search = build_search_tool(enabled=settings.enable_search)

    researcher = _subagent(
        name="funder-researcher",
        description=(
            "Research a funder's priorities, past awards, review criteria, and "
            "vocabulary. Use before drafting. Give it the funder name, the "
            "program, and the exact file path to write findings to."
        ),
        system_prompt=RESEARCHER_PROMPT,
        model_spec=RESEARCH_MODEL,
        tools=[search] if search else [],
        # It writes prose into a file the drafter reads; the section guides are
        # for whoever turns that into a section, not for gathering it.
        skills=None,
    )

    drafter = _subagent(
        name="section-drafter",
        description=(
            "Draft or revise exactly one proposal section. Give it the section "
            "name, its page/word limit, which files to read, and the exact "
            "path to write to."
        ),
        system_prompt=DRAFTER_PROMPT,
        model_spec=DRAFTING_MODEL,
        tools=[measure_text],
        skills=[SKILLS_DIR],
    )

    reviewer = _subagent(
        name="compliance-checker",
        description=(
            "Audit drafts against the solicitation's requirements and report "
            "blocking issues. Give it the application directory and the path "
            "to write its report to. It cannot edit drafts, only report."
        ),
        system_prompt=COMPLIANCE_PROMPT,
        model_spec=COMPLIANCE_MODEL,
        tools=[measure_text],
        # It audits sections against the guides that produced them, so it needs
        # the same guides in hand to say what a section was supposed to contain.
        skills=[SKILLS_DIR],
        permissions=compliance_permissions(),
    )

    return [researcher, drafter, reviewer]


def build_discovery_subagents() -> list[SubAgent]:
    """The roster for the discovery graph -- one scorer.

    Separate from `build_subagents` because it belongs to a separate graph, not
    because the list is short. The drafting orchestrator must not be able to
    reach `opportunity-scout` (there is no application to score against yet)
    and the discovery orchestrator must not be able to reach `section-drafter`
    (there is nothing to draft). Two rosters is what makes that structural
    rather than a rule in a prompt.

    Takes no `Settings`, unlike `build_subagents`: the only setting that
    reaches a subagent there is `enable_search`, and the scout deliberately has
    no search tool to enable. A parameter accepted and ignored would suggest
    otherwise.
    """
    scout = _subagent(
        name="opportunity-scout",
        description=(
            "Fit-score exactly one candidate opportunity against the "
            "organization profile. Give it the candidate file to read and the "
            "exact path to write its scoring to. It states verdicts and "
            "citations, never a score or a total."
        ),
        system_prompt=SCOUT_PROMPT,
        model_spec=DISCOVERY_MODEL,
        # No tools. It reads two files and writes one, all of which the
        # filesystem middleware already provides -- and withholding search is
        # the point rather than an omission: a scorer that could go looking for
        # a more flattering source is no longer scoring the archived text the
        # citation claims to quote.
        tools=[],
        # Deliberately none, and now said rather than merely absent. All six
        # guides under /skills/ are for drafting a section of a proposal; none
        # has anything to say about whether to apply at all, and handing over
        # an irrelevant capability is the same context-isolation lapse in
        # miniature. `_subagent` requires this argument precisely so this line
        # cannot be confused with invariant 3's omission; a test pins it too.
        skills=None,
        permissions=scouting_permissions(),
    )

    return [scout]
