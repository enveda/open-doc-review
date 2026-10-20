# Starter pack: supplier agreement vs standard terms

> **Not legal advice.** The review lists where an agreement departs from your own standard terms, so
> that a qualified person can decide. It does not judge whether a clause is acceptable or enforceable.

**What it checks.** A supplier agreement (NDA or MSA) is checked against the version of the company's
standard terms it says it is based on, found by `terms_version`. It reports deviations from four
standard positions: liability cap, term, governing law and confidentiality period.

**Planted problems** (`expected.yaml` lists them for `docreview eval`):

| Document | Problems |
|---|---|
| `inbox/MSA-0417.txt` | liability cap over 24 months of fees instead of 12; a 60-month term against a 36-month maximum; the laws of Otherland instead of Exampleland; confidentiality for 18 months instead of 5 years |

The jurisdictions, companies and terms are fictional.

**Run it.** From this folder: `docreview check`, then `docreview run --once` (needs `ANTHROPIC_API_KEY`),
or `docreview eval` to score the results against `expected.yaml`.

**Adapt it.**
- Replace the sample standard terms with your own, one document per version, each stating its
  version identifier, and list every position you want checked in the prompt.
- Redefine `blocker` in the prompt by your own approval rules.
- If your agreements do not cite a terms version, add a field they do carry, or use a single current
  standard-terms document and match on its title.
- Fill in `owner` and `approved_by` in the prompt's front matter before relying on any result.
