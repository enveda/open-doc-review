---
owner: example
version: 1
notice: >-
  STARTER TEXT. These checks and severity definitions are illustrative. Align them with your organization's
  own policies, procedures and regulatory obligations, and have the policy owner approve them, before use.
---
Review the executed batch record "{{ document_name }}" against the master batch record it was made to.

Fields already extracted from the batch record:
{{ fields }}

Reference documents:
{{ references }}

Check:
1. Every step and recorded value in the master batch record is present in the batch record.
2. Every recorded value is within the limit the master batch record sets for it.
3. Any value outside its limit has a deviation number recorded at that step.
4. Every entry the master batch record requires to be verified has a second-person verification.
5. The yield is within the acceptable range, and its arithmetic is correct.
6. The batch record cites the same product and master batch record version as the reference.

Severity: blocker = a result outside its limit with no deviation recorded, or a wrong master record;
major = a missing required entry or verification; minor = a small inconsistency that does not affect the
result; info = worth knowing, no action needed.
Set "reference" to "Master batch record" when a finding compares against it. Quote the step number, the
recorded value and the limit in each finding.
