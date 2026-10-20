---
title: Certificate of analysis vs specification review
version: 1
owner: <the quality team accountable for this review>
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
1. Every test listed in the Specification is reported on the certificate, with a result.
2. Each numeric result is within the acceptance limit stated for that test in the Specification,
   including the limit's boundaries exactly as written (for example "98.0 - 102.0 %").
3. Each result is reported in the same unit as the Specification's limit. A result in a different
   unit is a finding even when the converted value would pass.
4. Each descriptive result (such as appearance) meets the Specification's wording.
5. The lot is identified consistently: the same lot number everywhere it appears on the certificate.
6. The certificate cites the specification number and version it is checked against.

## What NOT to flag
- Extra tests the certificate reports that the Specification does not list.
- More decimal places than the limit shows, when the value is still within the limit.
- Laboratory addresses, logos and page layout.

## Severity
- blocker: a result outside its limit, or a required test not reported: the lot must not be released
  on this certificate.
- major: a result that cannot be judged against the limit as written (wrong unit, unclear lot), which
  must be corrected before the certificate is accepted.
- minor: a documentation slip that does not affect any result, such as a misspelled material name.
- info: worth knowing; no action needed.

## How to report
- One finding per issue. Quote the test, the reported result and the Specification's limit.
- Set "reference" to "Specification" when a finding compares against it.
- If the Specification is NOT FOUND, review what you can and report the gap once.
