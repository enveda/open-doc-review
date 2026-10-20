---
title: Procedure document-control checklist
version: 1
owner: <the document-control or quality team accountable for this review>
approved_by: <who approved this wording, per your change-control procedure>
notice: >-
  STARTER TEXT. The required sections, review cycle, severity definitions and exclusions below must reflect
  your organization's own policies, in particular its document-control procedure. Replace every <placeholder>, and remove checks that do
  not apply to you.
---
Review the {{ doc_type }} "{{ document_name }}" ({{ doc_type_description }}) - {{ window }}.

Fields already extracted from it:
{{ fields }}

Reference documents: none. This is a standalone review; every check below is made from the document
itself.
{{ references }}

## What to check
1. Required sections are present, each with a heading: Purpose, Scope, Responsibilities, Definitions,
   Procedure, Records, Revision history.
2. A document owner (a person or a role) is named.
3. An effective date is stated.
4. The next review date is after the effective date and no more than 24 months after it (the
   standard review cycle). A review date on or before the effective date is already past.
5. Every term defined in the Definitions section is used with that exact wording in the rest of the
   document; a different word for a defined term (a synonym) is a finding.
6. The version in the header matches the newest entry in the Revision history.

## What NOT to flag
- Writing style, grammar and formatting that do not change the meaning.
- Whether the procedure itself is the right way to do the task: subject-matter review happens elsewhere.
- Optional sections (for example References or Appendices) that are absent.

## Severity
- blocker: the document cannot be used as a controlled document as it stands, for example it has no
  effective date.
- major: must be corrected before the document is approved, for example a missing required section,
  a missing owner, or a review date already past.
- minor: should be corrected, for example a defined term used inconsistently.
- info: worth knowing; no action needed.

## How to report
- One finding per issue. Name the section and quote the text concerned.
- Leave "reference" empty: there are no reference documents.
