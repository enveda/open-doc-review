# Writing your prompts

The engine ships no prompt wording. You write the prompts, in your words and your house style.
The engine controls the **answer format**: every call uses a JSON schema that the Claude API
enforces. So your prompts say *what to look for and how to judge it*, never how to format the
response. Don't paste JSON examples into them.

## Files

The engine looks prompts up by name in the folders listed under `prompts.dirs`. The first folder
holding `<name>.md` wins, so a team can override a shared set file by file.

| Name | Used for | Required |
|---|---|---|
| `system.md` | System prompt sent with every call: who the reviewer is, tone, ground rules | optional |
| `classify.md` | Choose the document type | yes |
| `extract.md` | Pull the configured fields | yes |
| `extract/<doc_type>.md` | Type-specific extraction; overrides `extract.md` for that type | optional |
| `review/<doc_type>.md` | The review itself (or the file named in `doc_types.<type>.review.prompt`) | for each type with review enabled |

`docreview check` lists any prompt that is missing.

## Variables

Write `{{ name }}` to insert a value. A misspelled variable is an error, so it never reaches the
model as literal text.

| Prompt | Variables |
|---|---|
| `classify` | `document_name`, `doc_types` (one line per type: `- name: description`) |
| `extract` | `document_name`, `doc_type`, `doc_type_description`, `fields` (one line per field: `- name (type): description`) |
| `review` | `document_name`, `doc_type`, `doc_type_description`, `fields` (the extracted values, as JSON), `references` (one line per reference: attached, or NOT FOUND and why), `severities`, `window` ("all 12 pages", or "pages 101-200 of 450" for a long PDF reviewed in windows) |

The documents themselves are attached to the call, not inserted into the text. In a review, the
document under review comes first, then each reference titled with its rule `name` (for example
"Purchase order: PO-7730.txt"). Refer to them by those names.

## What the model returns (fixed by the engine)

- **classify**: `doc_type` (one of yours, or `unknown`), `confidence`, `reason`
- **extract**: one value per configured field, `null` when absent
- **review**: a `summary`, plus `findings`, each with `title`, `severity`
  (`blocker | major | minor | info`), `body`, `page` (or null) and `reference` (a reference's name, or null)

Your prompt should define what each severity means **for this document type**. The engine
derives the outcome from the most severe finding, and reports `insufficient_data` when a
required reference was not found, whatever the model said.

## Front matter

Optional YAML between `---` lines at the top is removed before sending. It is recorded, together with
the prompt's SHA-256, in the audit log for every call, so you can tell which wording produced which finding.

```markdown
---
owner: accounts-payable
version: 3
change: tightened the unit-price tolerance wording
---
Review the invoice "{{ document_name }}" ...
```

## Changing a prompt safely

1. Keep a small set of documents whose problems you already know.
2. Run them with `review.dry_run: true` before and after the change, and compare the findings in
   `logs/ai_calls.jsonl`.
3. Keep prompts in version control. The audit log's SHA-256 ties every result to the exact text.
