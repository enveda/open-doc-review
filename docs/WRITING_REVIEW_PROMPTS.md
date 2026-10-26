# Writing review prompts that give consistent results

> **Your policies come first.** Every prompt in this project, starter text included, is a starting point.
> The checks, severity levels and exclusions must come from **your organization's own policies, procedures,
> specifications and regulatory obligations**. Have the owner of those policies approve the wording, and
> change it under your normal change control. Nothing here is legal, quality or regulatory advice.

Start from `examples/prompt_templates/review_template.md`. Copy it to `prompts/review/<doc_type>.md` and
fill in each section. Notes in `<!-- ... -->` are for authors and are never sent to the model.

## The five parts of a good review prompt

1. **Context.** Which document this is and which references are attached, using the template's `{{ fields }}`,
   `{{ references }}` and `{{ window }}` variables. The engine attaches the documents themselves.
2. **What to check.** A numbered list. Each check:
   - is testable from the documents alone. Write "every result is within the limit stated for it in the
     Specification", not "results look reasonable";
   - names where "correct" is defined: which reference, which section;
   - covers one idea. Two ideas in one check get reported inconsistently.
3. **What NOT to flag.** The things reviewers keep dismissing: formatting, items your procedures check
   elsewhere, known harmless differences. This cuts noise more than anything else.
4. **Severity.** Define each level for this document type, in terms of consequence (see below).
5. **How to report.** Quote the value, the page and the reference value or limit. One finding per issue.

You don't need to describe the answer format. The engine enforces it, so leave out JSON examples and
"respond with" instructions.

## Defining severity

The engine turns severities into the outcome with fixed rules: any `blocker` means `blocking_findings`,
any `major` means `major_findings`, and so on. That makes the definitions the most important part of the
prompt. Write them as consequences, using your own policy's terms:

| Level | Ask yourself | Generic example |
|---|---|---|
| `blocker` | Would accepting the document as it is lead to a wrong decision, or to a record that breaks a requirement? | A result outside its limit with nothing explaining it. A total that pays the wrong amount. |
| `major` | Must it be fixed before the document is accepted? | A required entry or approval missing. A cited reference that doesn't match. |
| `minor` | Should it be fixed, though it doesn't change the outcome? | A typo in a non-key field. An inconsistent unit label where the value is clear. |
| `info` | Is it worth knowing, with no action needed? | A value close to its limit. |

When in doubt between two levels, say which one in the prompt: "treat X as major, not blocker, unless Y".

## Making results consistent

- **Measure, don't guess.** Write the problems you know a sample document has into an `expected.yaml` file,
  then run `docreview eval --runs 3`. A problem caught in 3 of 3 runs is dependable; 1 of 3 means the
  check is ambiguous, so reword it.
- **Change one thing at a time,** and re-run eval after every prompt, model or setting change. Keep the
  report from each run as the baseline for the next.
- **Read the findings that aren't on the expected list.** Each one is either a real problem (add it to
  the list) or noise (add an exclusion under "What NOT to flag").
- **Prefer specific over clever.** Name the field, the section, the limit. The model follows concrete
  instructions far more consistently than general ones.
- **Keep the shared prompts short.** The system prompt applies to every call. Put document-type rules
  in the review prompt, not the system prompt.

## Front matter: who owns the wording

Every prompt can start with YAML front matter. It is never sent to the model, but it is recorded with
the prompt's SHA-256 in the audit log for every call:

```markdown
---
title: Supplier invoice review
version: 4
owner: Accounts payable
approved_by: AP policy owner, per change request 123
notice: Aligned with the accounts-payable policy, revision 7.
---
```
