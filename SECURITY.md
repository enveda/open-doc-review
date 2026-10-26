# Security

Please report vulnerabilities privately through the repository's security advisory feature
("Report a vulnerability"), not in a public issue.

Deployment notes:
- Documents are sent to the Claude API. Confirm that this is permitted for your content, and set retention accordingly.
- Run the adapter's service account with least privilege: read, plus write for metadata and comments only.
- Keep secrets in environment variables or a secret manager. The YAML supports `${VAR}`.
- `logs/ai_calls.jsonl` contains prompts and model output (extracted values and findings). Protect it like the documents.
- Documents are untrusted input to the model. Text inside a document can try to steer the review (prompt
  injection). The model has no tools or credentials and only returns JSON, and the engine decides the outcome and
  what is written. Still, treat findings as advice, and expect a hostile document to be able to hide its own problems.
