---
title: Field extraction
version: 1
notice: >-
  STARTER TEXT. Check that these extraction rules match your organization's data and records policies
  (for example, how identifiers and dates must be recorded) before relying on extracted values.
---
The attached document "{{ document_name }}" is a {{ doc_type }}: {{ doc_type_description }}

Extract these fields:

{{ fields }}

Rules:
- Copy each value exactly as printed: same characters, punctuation, leading zeros and capitalization for
  identifiers. Do not correct apparent typos; the review may need to see them.
- Use null when a field is absent, illegible, or you are not sure which value it is. A missing value is
  better than a wrong one.
- When a value appears more than once, use the one in the document's header or main identification block.
  If the occurrences disagree, use the header value; the review will check the rest.
- Numbers: digits only, using "." as the decimal separator, without thousands separators, currency symbols
  or units, unless the field description says otherwise.
- Dates: YYYY-MM-DD. If the date is ambiguous (for example 03/04/2026 with no stated format), use null.
- Handwritten corrections: use the corrected value only if the correction is legible. Otherwise use null.
- Read only the document itself. Never fill a field from general knowledge or from other documents.
