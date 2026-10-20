# Configuration reference

One YAML file (default `docreview.yaml`; pass another with `-c`). Relative paths resolve from the
file's folder. `${VAR}` anywhere is replaced from the environment (an unset variable is an error), so secrets stay out of the file.
See `examples/generic/docreview.yaml` for a complete working example.

```yaml
ecm:
  adapter: local                 # local | box | package.module:Class | an installed plugin's name
  options: {...}                 # passed to the adapter's constructor

llm:
  provider: anthropic            # anthropic | bedrock | vertex | foundry
  provider_options: {}           # passed to the provider's client, e.g. {aws_region: us-east-1} or
                                 # {project_id: my-project, region: global}; credentials come from that cloud
  models:                        # per task: classify, extract, review; "default" covers the rest
    default: claude-opus-5-5
  effort:                        # low | medium | high | xhigh | max, per task or "default"
    review: high
  fallbacks: default             # "default" = server-side refusal fallback (Claude API only); "off" for other providers
  max_tokens: 16000              # per response, including the model's thinking
  max_document_mb: 30            # larger documents are rejected before sending

prompts:
  dirs: [prompts]                # searched in order; first match wins

review:
  post_comments: true
  comment_min_severity: minor    # findings below this are counted in the summary, not posted one by one
  dry_run: false                 # real model calls, no ECM writes (logged instead)
  window_pages: 100              # longer PDFs are reviewed in windows of this many pages; 0 = never split

audit:
  path: logs/ai_calls.jsonl      # one JSON line per model call

state_path: .docreview_state.json  # the adapter's poll cursor
max_attempts: 3                  # failures on one version before polling skips it (manual commands still run)

doc_types:
  <id>:                          # letters, digits, _ ; used in prompt names and metadata; 'unknown' is reserved
    description: ...             # required; the classifier reads it
    fields:
      <field>: {type: string|number|integer|boolean|date, description: "..."}   # quote descriptions
    review:
      enabled: true
      prompt: review/<id>        # optional; this is the default
      references:
        - name: Purchase order   # how prompts, comments and metadata refer to it
          doc_type: <id>         # the type to search
          match: {<field of that type>: "{<field of this type>}"}   # all must match
          required: true         # not found → outcome "insufficient_data"
          same_folder: false     # search only the reviewed document's folder
```

`docreview check` validates all of this. It reports every reference that uses a field the type
does not extract, and every prompt file that is missing.

## Outcomes

The engine computes the outcome deterministically; the model never sets it directly.

| Outcome | When |
|---|---|
| `insufficient_data` | a `required` reference was not found |
| `blocking_findings` | at least one `blocker` |
| `major_findings` | at least one `major` |
| `minor_findings` | only `minor` / `info` |
| `no_findings` | none |

When several documents match a reference, the newest is used and the summary comment says so,
so that a person can check the pick.

## Model providers

| `provider` | Credentials | Model id example |
|---|---|---|
| `anthropic` | `ANTHROPIC_API_KEY`, or `ant auth login` | `claude-opus-5-5` |
| `bedrock` | the AWS credential chain; `provider_options: {aws_region: ...}` | `anthropic.claude-opus-5-5` |
| `vertex` | Google application default credentials; `provider_options: {project_id: ..., region: global}` | `claude-opus-5-5` |
| `foundry` | `provider_options: {resource: ..., api_key: ${FOUNDRY_API_KEY}}` | `claude-opus-5-5` |

Set `fallbacks: off` for every provider except `anthropic`. Check that your provider offers the model
you name and that it supports PDF input and structured outputs.
