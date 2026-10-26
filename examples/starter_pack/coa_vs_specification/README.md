# Starter pack: certificate of analysis vs specification

**What it checks.** A certificate of analysis (CoA) is checked against the exact specification
version it cites: every specified test reported, each result within its limit, units matching the
limit, and the lot identified consistently. The reference rule matches on `spec_number` AND
`spec_version`, so a certificate citing version 3 is never checked against version 2.

**Planted problems** (`expected.yaml` lists them for `docreview eval`):

| Document | Problems |
|---|---|
| `inbox/COA-5001.txt` | assay 97.6 % below the 98.0 - 102.0 % limit; loss on drying reported as 4 mg/g against a % limit; particle size (D90) not reported; lot LOT-A17 in the header but LOT-A71 in the conclusion |

`SPEC-100_v2.txt` is there on purpose: its wider assay limit (97.0 - 103.0 %) would pass the
certificate's 97.6 %. Only the cited version 3 applies.

**Run it.** From this folder: `docreview check`, then `docreview run --once` (needs `ANTHROPIC_API_KEY`),
or `docreview eval` to score the results against `expected.yaml`.

**Adapt it.**
- Add your own tests and limits to the sample specification, or point the adapter at your
  specifications and certificates.
- If your certificates cite a specification without a version, decide whether the newest effective
  version applies, and change the rule's `match` and the prompt accordingly.
- Fill in `owner` and `approved_by` in the prompt's front matter, and align the checks and severity
  definitions with your own procedures before relying on any result. The engine is advisory: a
  person makes the release decision.
