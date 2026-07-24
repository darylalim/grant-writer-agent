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

from grant_writer.backends import compliance_permissions
from grant_writer.config import (
    COMPLIANCE_MODEL,
    DRAFTING_MODEL,
    RESEARCH_MODEL,
    SKILLS_DIR,
)
from grant_writer.prompts import COMPLIANCE_PROMPT, DRAFTER_PROMPT, RESEARCHER_PROMPT
from grant_writer.tools import build_search_tool, measure_text


def build_subagents() -> list[SubAgent]:
    """Assemble the subagent roster for the current environment."""
    search = build_search_tool()

    researcher: SubAgent = {
        "name": "funder-researcher",
        "description": (
            "Research a funder's priorities, past awards, review criteria, and "
            "vocabulary. Use before drafting. Give it the funder name, the "
            "program, and the exact file path to write findings to."
        ),
        "system_prompt": RESEARCHER_PROMPT,
        "tools": [search] if search else [],
        "model": RESEARCH_MODEL,
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
        "model": DRAFTING_MODEL,
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
        "model": COMPLIANCE_MODEL,
        "skills": [SKILLS_DIR],
        "permissions": compliance_permissions(),
    }

    return [researcher, drafter, reviewer]
