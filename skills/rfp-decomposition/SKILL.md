---
name: rfp-decomposition
description: Turn a solicitation, RFP, NOFO, or funder guideline document into a structured requirements checklist covering required sections, page and word limits, review criteria and their point weights, eligibility rules, formatting constraints, and deadlines. Use before any drafting begins.
---

# RFP Decomposition

## Overview

Everything downstream is checked against the artifact you produce here. A
requirement you miss now becomes a rejection later, so completeness matters
more than speed.

## When to Use

Immediately after extracting the solicitation text, before writing a single
sentence of narrative.

## Instructions

Produce `requirements.md` in the application directory with these sections.

### 1. Administrative facts

Funder, program name, opportunity number, deadline (with time zone), award
ceiling and floor, expected number of awards, project period, and submission
portal. Quote the source and cite the page for each.

### 2. Eligibility

Every stated eligibility rule, each marked `MET`, `NOT MET`, or `UNKNOWN`
against the organization profile in `/memories/org/AGENTS.md`. A single
`NOT MET` is a stop-work condition — surface it immediately rather than
drafting a proposal that cannot be submitted.

### 3. Required components

A table with one row per required document or section:

| Section | Limit | Format notes | Source page |
|---|---|---|---|

Include attachments and forms, not just narrative — letters of support,
indirect cost agreements, IRB documentation, and audited financials are
routinely what a submission is missing.

### 4. Review criteria

The scoring rubric verbatim, with point values or weights. This is the single
most important part of the file: reviewers score against these words, not
against the narrative section headings. Where a criterion is not obviously
covered by any required section, say so explicitly — that gap is where
proposals quietly lose points.

### 5. Formatting rules

Page limits, margins, font family and size, line spacing, file naming
conventions, and whether limits count references and appendices.

### 6. Ambiguities

Anything genuinely unclear in the solicitation, phrased as a question for the
program officer. Do not resolve ambiguity by guessing.

## Rules

- Quote the solicitation directly. Paraphrase loses the exact wording that
  reviewers and compliance screens rely on.
- Cite a page number for every requirement.
- If a limit is stated in pages, record both the page limit and the formatting
  rules — a page means nothing without font and spacing.
- Never infer a requirement that is not stated. Mark unknowns as unknown.
