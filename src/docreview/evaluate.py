"""``docreview eval``: review documents with known problems and score what was caught.

Each run reviews a fresh temporary copy of a library with the local adapter, so the real ECM is never
touched. Results are compared with an expected-findings file (see examples/generic/expected.yaml).
Several runs show how consistent the results are: a problem caught in 3 of 3 runs is dependable,
1 of 3 is not.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from docreview.config import Config, ConfigError
from docreview.ecm.base import NS_CLASSIFICATION, NS_REVIEW
from docreview.ecm.local import SIDECAR
from docreview.models import Severity


@dataclass(frozen=True)
class ExpectedFinding:
    name: str
    match: tuple[str, ...]
    severity_at_least: Severity = Severity.MINOR


@dataclass(frozen=True)
class ExpectedDocument:
    path: str
    doc_type: str | None = None
    outcome: str | None = None
    findings: tuple[ExpectedFinding, ...] = ()


@dataclass(frozen=True)
class ActualFinding:
    severity: Severity
    text: str

    def matches(self, exp: ExpectedFinding) -> bool:
        low = self.text.casefold()
        return self.severity.rank >= exp.severity_at_least.rank and all(m.casefold() in low for m in exp.match)


@dataclass
class DocumentScore:
    path: str
    runs: int = 0
    doc_type_ok: int = 0
    outcome_ok: int = 0
    caught: dict[str, int] = field(default_factory=dict)  # expected finding name -> runs that caught it
    extra: dict[str, int] = field(default_factory=dict)  # unexpected finding title -> runs that reported it
    errors: list[str] = field(default_factory=list)


@dataclass
class EvalReport:
    runs: int
    documents: list[DocumentScore]
    expected: dict[str, ExpectedDocument]

    @property
    def passed(self) -> bool:
        """Every expectation met in a majority of runs, and the document type right in every run."""
        majority = self.runs // 2 + 1
        for d in self.documents:
            exp = self.expected[d.path]
            if d.errors or (exp.doc_type and d.doc_type_ok < self.runs):
                return False
            if exp.outcome and d.outcome_ok < majority:
                return False
            if any(n < majority for n in d.caught.values()):
                return False
        return True

    def to_json(self) -> str:
        return json.dumps(
            {"runs": self.runs, "passed": self.passed, "documents": [asdict(d) for d in self.documents]}, indent=2
        )

    def format(self) -> str:
        r = self.runs
        lines = [f"Evaluation over {r} run(s): {'PASS' if self.passed else 'FAIL'}", ""]
        for d in self.documents:
            exp = self.expected[d.path]
            lines.append(f"{d.path}")
            for err in d.errors:
                lines.append(f"  ERROR  {err}")
            if exp.doc_type:
                lines.append(f"  {_mark(d.doc_type_ok, r)}  document type {exp.doc_type}: {d.doc_type_ok}/{r}")
            if exp.outcome:
                lines.append(f"  {_mark(d.outcome_ok, r)}  outcome {exp.outcome}: {d.outcome_ok}/{r}")
            for name, n in d.caught.items():
                lines.append(f"  {_mark(n, r)}  caught: {name}: {n}/{r}")
            for title, n in sorted(d.extra.items(), key=lambda kv: -kv[1]):
                lines.append(f"  ?      not in the expected list: {title}: {n}/{r}")
            lines.append("")
        total = sum(len(d.caught) for d in self.documents) * r
        hit = sum(sum(d.caught.values()) for d in self.documents)
        if total:
            lines.append(f"Expected findings caught: {hit}/{total} ({100 * hit / total:.0f}%).")
        lines.append(
            "Findings not in the expected list may be real: read them, then add them to the list or tighten "
            "the prompt's 'What NOT to flag' section."
        )
        return "\n".join(lines)


def _mark(n: int, runs: int) -> str:
    return "OK   " if n == runs else ("WEAK " if n > runs // 2 else "MISS ")


def load_expected(path: Path) -> dict[str, ExpectedDocument]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = {}
    for doc_path, spec in (raw.get("documents") or {}).items():
        spec = spec or {}
        findings = []
        for f in spec.get("findings") or []:
            try:
                findings.append(
                    ExpectedFinding(
                        name=str(f["name"]),
                        match=tuple(str(m) for m in f["match"]),
                        severity_at_least=Severity(f.get("severity_at_least", "minor")),
                    )
                )
            except (KeyError, ValueError) as exc:
                raise ConfigError(f"{path}: {doc_path}: bad finding {f!r} ({exc})") from None
        out[doc_path] = ExpectedDocument(doc_path, spec.get("doc_type"), spec.get("outcome"), tuple(findings))
    if not out:
        raise ConfigError(f"{path}: no documents listed")
    return out


def _actual_findings(sidecar: dict[str, Any]) -> list[ActualFinding]:
    """Findings as posted: comments of the form "[SEVERITY] title\\nbody"."""
    out = []
    for c in sidecar.get("comments", []):
        text = c.get("text", "")
        if text.startswith("[") and "]" in text:
            label = text[1 : text.index("]")].casefold()
            try:
                out.append(ActualFinding(Severity(label), text[text.index("]") + 1 :].strip()))
            except ValueError:
                continue
    return out


def run_eval(cfg: Config, expected_path: Path, library: Path, runs: int = 3, model_client: Any = None) -> EvalReport:
    from docreview.cli import build, run_once  # deferred: cli imports this module

    expected = load_expected(expected_path)
    scores = {p: DocumentScore(p, caught={f.name: 0 for f in e.findings}) for p, e in expected.items()}
    for p in expected:
        if not (library / p).is_file():
            scores[p].errors.append(f"not found in {library}")
    for _ in range(runs):
        with tempfile.TemporaryDirectory(prefix="docreview-eval-") as tmp:
            root = Path(tmp) / "library"
            shutil.copytree(library, root, ignore=shutil.ignore_patterns(f"*{SIDECAR}", ".*"))
            run_cfg = replace(
                cfg,
                adapter="local",
                adapter_options={"root": str(root), "_base_dir": tmp},
                state_path=Path(tmp) / "state.json",
                audit_path=cfg.audit_path.with_name("eval_ai_calls.jsonl"),  # kept apart from production calls
                review=replace(cfg.review, dry_run=False, post_comments=True, comment_min_severity=Severity.INFO),
            )
            run_once(build(run_cfg, model_client=model_client))
            for p, exp in expected.items():
                score = scores[p]
                if score.errors:
                    continue
                score.runs += 1
                side_path = root / (p + SIDECAR)
                sidecar = json.loads(side_path.read_text(encoding="utf-8")) if side_path.exists() else {}
                if (sidecar.get(NS_CLASSIFICATION) or {}).get("doc_type") == exp.doc_type:
                    score.doc_type_ok += 1
                if exp.outcome and (sidecar.get(NS_REVIEW) or {}).get("outcome") == exp.outcome:
                    score.outcome_ok += 1
                actual = _actual_findings(sidecar)
                for f in exp.findings:
                    if any(a.matches(f) for a in actual):
                        score.caught[f.name] += 1
                for a in actual:
                    if not any(a.matches(f) for f in exp.findings):
                        title = a.text.splitlines()[0] if a.text else "(empty)"
                        score.extra[f"[{a.severity.value}] {title}"] = (
                            score.extra.get(f"[{a.severity.value}] {title}", 0) + 1
                        )
    return EvalReport(runs=runs, documents=list(scores.values()), expected=expected)
