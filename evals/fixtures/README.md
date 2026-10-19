# Fixed inputs for live comparisons

Every live comparison in this repo (the upgrade smoke and performance runs, and
the graph-level evals) uses these files, never "a small RFP" picked on the day.
Two runs on different inputs measure the inputs, not the change.

This directory is tracked. **Never put real organization or personnel data
here.**

| File | What it is |
|---|---|
| `scmp-2026-nofo-excerpt.pdf` | A verbatim excerpt of a real, public federal solicitation (below). |
| `profile-fictional.md` | An invented organization profile, written to fit that solicitation. Some fields are deliberately `UNKNOWN`, so a correct run produces `[NEEDS INPUT]` markers for them. |

## The solicitation

USDA Agricultural Marketing Service, *Specialty Crop Multi-State Program,
Fiscal Year 2026 Notice of Funding Opportunity*, USDA-AMS-TM-SCMP-G-26-0020.
A US federal government work, so in the public domain.

- Source: <https://www.ams.usda.gov/sites/default/files/media/SCMP%20NOFO%20Approved%20Final.pdf>,
  linked from <https://www.ams.usda.gov/services/grants/scmp>
- Downloaded 2026-10-19. The original was 29 pages, SHA-256
  `382b545c190def30bbb9ac6ca0e94d44bae86ad44d25fd5a479aa75166d9cbaf`.
- Pages kept, copied whole with `pypdf` rather than retyped: 1 (title), 4–6
  (basic information, funding, award size), 8 (eligible applicants), 12 (project
  area types), 19 (project narrative and its page limit), 25–27 (review
  criteria).

## The standard runs

The org profile reaches the agent as `/memories/org/AGENTS.md`, so a run uses
its own root rather than the repo's: copy this profile there and link `skills/`
in. That also keeps the run's checkpoints out of the real
`.grant_writer/checkpoints.sqlite`.

- **draft:** `--rfp evals/fixtures/scmp-2026-nofo-excerpt.pdf --funder "USDA AMS" --no-search`
- **discover:** `--focus "specialty crop marketing and frost protection for small fruit growers" --no-search`

`--no-search` keeps web results, which change daily, out of the comparison.
Discovery still queries grants.gov live, so run a before/after pair close
together.

Outputs go to `evals/out/` (ignored), since they carry raw model output.
