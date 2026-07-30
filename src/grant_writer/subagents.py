"""Subagent definitions.

Three specialists, split along the lines where context isolation actually pays:

- research burns a lot of tokens on search results the drafter never needs,
- drafting needs the funder-specific style guides loaded,
- compliance must judge the drafts without the drafter's rationalizations in
  context, and must not be able to edit what it reviews.

Note that custom subagents do **not** inherit `skills` from the parent, so the
drafter and reviewer are handed the skills directory explicitly. Forgetting
this is a silent failure: the agent still answers, just generically.
"""

from __future__ import annotations

from deepagents import SubAgent

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


def build_subagents(settings: Settings | None = None) -> list[SubAgent]:
    """Assemble the subagent roster for the current environment."""
    settings = settings or Settings()
    search = build_search_tool(enabled=settings.enable_search)

    researcher: SubAgent = {
        "name": "funder-researcher",
        "description": (
            "Research a funder's priorities, past awards, review criteria, and "
            "vocabulary. Use before drafting. Give it the funder name, the "
            "program, and the exact file path to write findings to."
        ),
        "system_prompt": RESEARCHER_PROMPT,
        "tools": [search] if search else [],
        "model": build_model(RESEARCH_MODEL),
    }

    drafter: SubAgent = {
        "name": "section-drafter",
        "description": (
            "Draft or revise exactly one proposal section. Give it the section "
            "name, its page/word limit, which files to read, and the exact "
            "path to write to."
        ),
        "system_prompt": DRAFTER_PROMPT,
        "tools": [measure_text],
        "model": build_model(DRAFTING_MODEL),
        # Custom subagents do not inherit skills -- pass them explicitly.
        "skills": [SKILLS_DIR],
    }

    reviewer: SubAgent = {
        "name": "compliance-checker",
        "description": (
            "Audit drafts against the solicitation's requirements and report "
            "blocking issues. Give it the application directory and the path "
            "to write its report to. It cannot edit drafts, only report."
        ),
        "system_prompt": COMPLIANCE_PROMPT,
        "tools": [measure_text],
        "model": build_model(COMPLIANCE_MODEL),
        "skills": [SKILLS_DIR],
        "permissions": compliance_permissions(),
    }

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
    scout: SubAgent = {
        "name": "opportunity-scout",
        "description": (
            "Fit-score exactly one candidate opportunity against the "
            "organization profile. Give it the candidate file to read and the "
            "exact path to write its scoring to. It states verdicts and "
            "citations, never a score or a total."
        ),
        "system_prompt": SCOUT_PROMPT,
        # No tools. It reads two files and writes one, all of which the
        # filesystem middleware already provides -- and withholding search is
        # the point rather than an omission: a scorer that could go looking for
        # a more flattering source is no longer scoring the archived text the
        # citation claims to quote.
        "tools": [],
        "model": build_model(DISCOVERY_MODEL),
        # Deliberately no `skills`. Invariant 3 says custom subagents do not
        # inherit them, which usually means a missing key is a bug -- here it
        # is not. All six guides under /skills/ are for drafting a section of a
        # proposal; none has anything to say about whether to apply at all, and
        # handing over an irrelevant capability is the same context-isolation
        # lapse in miniature. Pinned by a test so it is not "fixed" later.
        "permissions": scouting_permissions(),
    }

    return [scout]
