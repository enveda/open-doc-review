---
title: Supplier agreement vs standard terms review
version: 1
owner: <the legal or contracts team accountable for this review>
approved_by: <who approved this wording, per your change-control procedure>
notice: >-
  STARTER TEXT, NOT LEGAL ADVICE. The positions, severity definitions and exclusions below must reflect your
  organization's own standard terms, policies and approval rules, and every finding must be assessed by a
  qualified person. Replace every <placeholder>, and remove checks that do not apply to you.
---
Review the {{ doc_type }} "{{ document_name }}" ({{ doc_type_description }}) - {{ window }}.

Fields already extracted from it:
{{ fields }}

Reference documents attached (each titled with its name below):
{{ references }}

## What to check
Compare the agreement's clauses with the positions in the Standard terms, and report each deviation:
1. Limitation of liability: the cap and what it is measured against.
2. Term and renewal: the initial term, and any automatic renewal.
3. Governing law and jurisdiction.
4. Confidentiality: how long the obligations last after the agreement ends.
5. Any of these four positions that the agreement leaves out entirely.

Describe what the agreement says, what the Standard terms say, and quote the clause numbers. Do not
judge whether a deviation is acceptable: that is for the contract owner.

## What NOT to flag
- Wording that differs from the Standard terms but has the same effect.
- Clauses the Standard terms do not cover (pricing, service levels, schedules).
- Party names, addresses and signature blocks.

## Severity
- blocker: a deviation the company never signs, such as uncapped liability, or a standard protection
  removed entirely (for example no confidentiality obligation at all).
- major: a deviation from a standard position that needs approval before signature.
- minor: a deviation in the company's favor, or a drafting inconsistency, worth correcting.
- info: worth knowing; no action needed.

## How to report
- One finding per deviation. Quote the agreement's clause and the Standard terms' position.
- Set "reference" to "Standard terms" when a finding compares against it.
- If the Standard terms are NOT FOUND, review what you can and report the gap once.
