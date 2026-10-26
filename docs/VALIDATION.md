# Adopting open-doc-review in a regulated organization

> **This is a starting template, not a validation package.** Adapt it to your organization's
> quality system, policies and regulatory obligations, and have your quality function approve
> whatever you derive from it. It describes what the tool does and suggests checks; it does not
> claim that any use is compliant. **It is not legal or regulatory advice.**

The guide is written for teams in regulated environments (life sciences, finance, public sector,
and similar) that must show their tools are fit for purpose. Use the sections that apply to you,
and replace the bracketed placeholders.

## 1. Intended use

State the intended use in your own words, and keep it narrow. A suggested form:

> open-doc-review is an **advisory review aid**. For the document types configured in
> `[docreview.yaml]`, it classifies documents, extracts the configured fields, finds the configured
> reference documents, and posts findings as comments and metadata beside each document in
> `[content-management system]`. **A qualified person reviews every document and makes every
> decision.** The tool does not approve, reject, release or change any document, and no process
> step depends on its output alone.

Also record:

- **Out of scope.** For example, using the findings as the only review, using the outcome
  label as a disposition, or document types and sources not listed in the configuration.
- **Who uses it.** Reviewers read the findings. Administrators maintain the configuration and
  prompts.
- **Data.** Which documents are sent to the model provider, under which agreement and
  retention terms.

## 2. Risk assessment template

Rate each hazard with your organization's own scale for severity, likelihood and detectability.
The "controls in the tool" column lists features the tool has today. Check each one in your own
environment rather than relying on this table.

| Hazard | Typical causes | Controls in the tool | Residual risk and controls to add |
|---|---|---|---|
| **A real issue is missed** | Model error; a long document read in windows; the issue needs two windows at once; unclear prompt | Advisory only: a person still reviews the whole document. Findings carry page references. Long PDFs are reviewed window by window, and the summary says which pages each part covers. `docreview eval` measures recall on documents with known problems. | Medium. Keep the human review unchanged. Set a minimum recall in your acceptance criteria. Re-run eval after every change (§4). |
| **A false finding** | Model error; ambiguous rule in the prompt | Findings are comments a person accepts or ignores; nothing acts on them. The outcome is derived from severities by fixed rules (see [CONFIGURATION.md](CONFIGURATION.md#outcomes)), never chosen by the model. | Low (wasted effort). Track the rate of false findings in eval and in periodic review (§6). |
| **The wrong reference document is used** | Two documents match the same identifier; an extraction error in the matching field | Matching uses only configured fields, equal after trimming and folding case. When several documents match, the summary comment says so and names the one used. **A required reference that is not found is reported, never guessed: the outcome is `insufficient_data`.** The references used are recorded in the review metadata. | Medium. Make the matching fields specific. Train reviewers to check the named reference. Include ambiguous cases in eval. |
| **Data leaves the organization's perimeter** | Documents are sent to the model provider by design | Only the document under review and its references are sent. The model has no tools and cannot fetch anything. `llm.max_document_mb` caps what is sent. The audit log records each attachment by hash, never its content. | Depends on your agreement. Decide which documents may be sent. Set retention terms (for example zero data retention) with the provider. Choose the provider region. Record the decision. |
| **Instructions injected through a document** | Text in a document tries to steer the review | The model returns JSON against a fixed schema and has no credentials or tools. The engine decides what is written, and to which document. Output is advisory, and a person reviews it. | Medium: a hostile document can hide its own problems. Add documents with embedded instructions to the eval set, and keep the human review. |
| **A silent change of prompt or model** | A prompt file is edited; the model id in the config changes; the provider updates a model behind an alias; a package upgrade | Every call is audited with the model requested and the model used, plus a SHA-256 fingerprint and the front matter of every prompt file. Prompts are plain files you can keep under version control. | Low with change control (§4). Pin exact model ids, not aliases. Restrict who can edit prompts and config. Review the audit log for unexpected model or prompt fingerprints (§6). |
| **Processing fails silently** | ECM or API outage; an unreadable file | Errors are logged and recorded on the document (`failed_version`, `failure_count`, `last_error`). Polling stops retrying a version after `max_attempts`. Failed model calls are written to the audit log too. | Low. Monitor the log and these metadata fields. Define who reviews failed documents. |
| **The tool changes or deletes a document** | A defect in an adapter | The ECM port has no delete, move, rename or content-overwrite method. Adapters write only metadata and comments beside the document. **Grant the service account least privilege** at the ECM. | Low. Confirm the permissions at installation (§3) and in periodic review. |
| **Misconfiguration** | A wrong folder, field, template or prompt name | `docreview check` validates the config, prompts and adapter connection. `review.dry_run: true` runs real model calls while writing nothing. | Low. Make the checks in §3 part of every installation and change. |

## 3. What to verify before go-live

### Installation checklist

Record evidence for each item: the output, a screenshot, or a signed checklist.

| # | Check | How |
|---|---|---|
| 1 | The package version and its dependencies are the ones approved | `pip show open-doc-review`; keep the lock file |
| 2 | The configuration file is the approved version | Version control: commit hash or checksum of `docreview.yaml` and the prompt folder |
| 3 | Secrets come from the environment or a secret manager, not the YAML | Inspect the config: only `${VAR}` references |
| 4 | Model ids are exact and approved | `llm.models` in the config |
| 5 | The adapter's service account has **least privilege**: read documents, write metadata and comments, nothing more | The ECM's permission screen, plus the adapter's guide ([BOX.md](BOX.md), [SHAREPOINT.md](SHAREPOINT.md), [ALFRESCO.md](ALFRESCO.md), [S3.md](S3.md), …) |
| 6 | The metadata schema in the ECM matches the config | `docreview templates`, then [METADATA.md](METADATA.md) |
| 7 | Config, prompts and adapter are consistent | `docreview check` exits 0 |
| 8 | The audit log is written to protected, persistent storage | Process one test document; confirm a new line in `audit.path` and its access rights |
| 9 | Nothing outside the configured scope is touched | Place a document outside the scope; confirm it is not processed |
| 10 | A dry run writes nothing | `review.dry_run: true` on a test document; the ECM is unchanged |

### Performance check

1. **Build an evaluation set.** Use documents from your own process with **known, documented
   problems**, chosen by subject-matter experts before the tool sees them. Include clean
   documents, borderline cases, long documents, documents with missing or ambiguous references,
   and a few with embedded instructions.
2. **Define acceptance criteria in advance.** Examples: the share of known issues found
   (recall), the share of findings that are real (precision), and correct handling of missing
   references.
3. **Run `docreview eval`** against the set, and keep its report as the **baseline**.
4. **Repeat the run** at least three times. Model output varies between identical runs, so
   record the spread of results, not one number. Which findings appear, and especially their
   severity, can change from run to run.
5. **Have a qualified reviewer assess the findings**, and document any disagreement with the
   expected answers.
6. **Decide and record** whether the criteria are met, with the limitations users must know.

## 4. Change control

Treat each of these as a change that needs assessment and approval under your change-control
procedure:

| What changes | Examples |
|---|---|
| **Prompts** | any edit to a file in `prompts.dirs` ([PROMPTS.md](PROMPTS.md)) |
| **Configuration** | document types, fields, reference rules, severities, `window_pages`, `max_attempts` |
| **Model** | `llm.models`, `llm.effort`, `llm.provider`, `llm.fallbacks` |
| **Software** | the open-doc-review version, its dependencies, the adapter |
| **ECM setup** | templates, columns, permissions, scope folders |

**For any of these, re-run the eval set and compare the result with the baseline** before
approving the change. Keep both reports with the change record. A changed prompt fingerprint or
model id in the audit log must always trace back to an approved change.

Test changes with `review.dry_run: true` against a test area first ([DEPLOY.md](DEPLOY.md)).

## 5. Records

**What the audit log holds.** `audit.path`, default `logs/ai_calls.jsonl`, has one JSON line per
model call, failed calls included. Each line holds:

- the run id and the document id;
- the task, the model requested and the model that answered, and the effort;
- the full system prompt and instruction text, and every prompt file's SHA-256 fingerprint and
  front matter;
- each attachment's document id, version and SHA-256, but never its bytes;
- the structured output, token usage and duration, or the error.

**Beside each document**, the ECM holds the classification, the extracted fields, the review
outcome with its run id and the references used, and the comments
([METADATA.md](METADATA.md)). The ECM's own audit trail records who changed what.

Decide and document:

- **Retention:** how long the audit log is kept. Align it with the records it supports.
- **Integrity:** append-only or write-once storage, and backups.
- **Access control:** who can read the log, which contains document content in the extracted
  values and findings, and who can change it (no one, ideally).
- **Link to the record:** how a reviewer gets from a comment to its run id and audit lines.

## 6. Periodic review

At an interval your quality system sets, for example every 6 or 12 months, and after any major
incident, review:

- **Performance:** re-run the eval set, and compare it with the baseline and the previous review.
- **Feedback:** how reviewers rated findings in practice, including findings marked as wrong and
  issues the tool missed.
- **Audit log:**
  - only approved model ids and prompt fingerprints appear;
  - failure rates and documents that were skipped after `max_attempts`.
- **Access:** service-account permissions, and who can edit config and prompts.
- **The provider:** model deprecations, changes to terms or retention, and new versions.
- **Intended use:** is the tool still used as described in §1?

Record the outcome and any actions, and feed them back into change control (§4).
