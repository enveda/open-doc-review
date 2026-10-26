---
title: Review prompt template - copy to prompts/review/<doc_type>.md and fill in
version: 1
owner: <the team accountable for this review>
approved_by: <who approved this wording, per your change-control procedure>
notice: >-
  STARTER TEXT. The checks, severity definitions and exclusions below must reflect your organization's own
  policies, procedures, specifications and regulatory obligations. Replace every <placeholder>, and remove
  checks that do not apply to you.
---
Review the {{ doc_type }} "{{ document_name }}" ({{ doc_type_description }}) - {{ window }}.

Fields already extracted from it:
{{ fields }}

Reference documents attached (each titled with its name below):
{{ references }}

## What to check
<!-- Number every check. One idea per check, testable from the documents alone. Say what "correct" looks
     like and where it is defined: "every recorded value is within the limit stated for it in the
     <reference name>", not "values look reasonable". -->
1. <check>
2. <check>
3. <check>

## What NOT to flag
<!-- List the things reviewers repeatedly dismiss. This is the most effective way to cut noise. -->
- <for example: formatting and layout differences that do not change meaning>
- <for example: items your procedures say are checked elsewhere>

## Severity
<!-- Define each level for THIS document type, in terms of consequence. Use your own policy's language. -->
- blocker: <would lead to a wrong decision or a non-compliant record if not corrected; e.g. ...>
- major: <must be resolved before the document is accepted; e.g. ...>
- minor: <should be corrected, but does not affect the outcome; e.g. ...>
- info: <worth knowing; no action needed>

## How to report
- One finding per issue. Quote the value, the page and, where relevant, the reference value or limit.
- Set "reference" to the reference document's name when a finding compares against it.
- If a reference is NOT FOUND, review what you can and report the gap once.
