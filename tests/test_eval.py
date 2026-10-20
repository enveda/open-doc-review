from __future__ import annotations

import argparse
import json

import yaml

from docreview.cli import cmd_eval
from docreview.config import load_config


def _expected(workspace, extra_findings=()):
    spec = {
        "documents": {
            "inbox/INV-2041.txt": {
                "doc_type": "invoice",
                "outcome": "blocking_findings",
                "findings": [
                    {"name": "quantity", "match": ["550", "500"], "severity_at_least": "major"},
                    {"name": "total", "match": ["1,460"], "severity_at_least": "blocker"},
                    *extra_findings,
                ],
            },
            "purchase_orders/PO-7730.txt": {"doc_type": "purchase_order"},
        }
    }
    path = workspace / "exp.yaml"
    path.write_text(yaml.safe_dump(spec))
    return path


def _args(path, out=None, runs=2):
    return argparse.Namespace(expected=str(path), library=None, runs=runs, out=out)


def test_eval_passes_and_never_touches_the_library(workspace, fake_model, capsys):
    cfg = load_config(workspace / "docreview.yaml")
    out = workspace / "report.json"
    assert cmd_eval(cfg, _args(_expected(workspace), str(out)), model_client=fake_model) == 0
    text = capsys.readouterr().out
    assert "PASS" in text and "caught: quantity: 2/2" in text
    assert "not in the expected list" in text  # the fake's other findings are listed for a person to read
    assert not list((workspace / "library").rglob("*.docreview.json"))
    assert json.loads(out.read_text())["passed"] is True


def test_eval_fails_on_a_missed_finding(workspace, fake_model, capsys):
    cfg = load_config(workspace / "docreview.yaml")
    missing = {"name": "expedite fee", "match": ["expedite"], "severity_at_least": "major"}
    assert cmd_eval(cfg, _args(_expected(workspace, [missing])), model_client=fake_model) == 1
    assert "MISS   caught: expedite fee: 0/2" in capsys.readouterr().out


def test_eval_counts_severity(workspace, fake_model, capsys):
    cfg = load_config(workspace / "docreview.yaml")
    too_high = {"name": "price as blocker", "match": ["0.95"], "severity_at_least": "blocker"}
    assert cmd_eval(cfg, _args(_expected(workspace, [too_high]), runs=1), model_client=fake_model) == 1


def test_example_expected_file_loads(workspace):
    from docreview.evaluate import load_expected

    exp = load_expected(workspace / "expected.yaml")
    assert len(exp["inbox/EBR-B24-0117.txt"].findings) == 4
