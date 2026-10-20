# Running it for real

The engine is one process that polls. Run it however you run small services.

| How | Command |
|---|---|
| Container | `docker run -v /srv/docreview:/config -e ANTHROPIC_API_KEY open-doc-review -c /config/docreview.yaml run` |
| cron (every 5 min) | `*/5 * * * * cd /srv/docreview && docreview run --once >> logs/run.log 2>&1` |
| systemd | `ExecStart=/opt/docreview/.venv/bin/docreview -c /srv/docreview/docreview.yaml run --interval 60` |

Mount or keep these on persistent storage: the config folder (YAML and prompts), `logs/` (the
audit log), and `state_path` (the poll cursor). Run one instance per configuration: two pollers
on the same ECM area would review the same documents twice.

## Rolling out

1. **Dry run.** Set `review.dry_run: true` and point it at a test area of your ECM. Model calls are
   real and nothing is written; read the would-be comments in the log. What would have been written is
   kept in memory, so a long-running `docreview run` processes each document once. A restart, or
   `--once` from cron, processes them again and pays again. References are found only among documents
   the ECM already holds metadata for.
2. **Known problems.** Keep a handful of documents whose problems you already know. Re-run them after
   every prompt, model or config change, and compare the findings.
3. **Write.** Turn `dry_run` off for one folder first (adapter scope), then widen.
4. **Watch.** `logs/ai_calls.jsonl` has every call, with status, model, tokens and seconds. A
   document that keeps failing carries `failure_count` and `last_error` in its classification
   metadata, and polling skips it after `max_attempts`.

## Cost

You pay for one classification call and one extraction call per new document version, plus one review call per page window
for document types with review enabled. Reference documents are attached to every review call, so
large references multiply the cost of long documents. Use `llm.effort` per task: classification
and extraction rarely need more than `low`.
