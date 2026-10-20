"""Every starter pack under examples/starter_pack/ is complete and self-consistent. No model calls."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from docreview.cli import required_prompts
from docreview.config import load_config
from docreview.prompts import PromptLibrary

PACKS = sorted(
    p.parent for p in (Path(__file__).resolve().parents[1] / "examples" / "starter_pack").glob("*/docreview.yaml")
)
REVIEW_VARIABLES = dict.fromkeys(
    ("document_name", "doc_type", "doc_type_description", "fields", "references", "severities", "window"), "x"
)


def test_the_four_packs_are_present():
    assert {p.name for p in PACKS} == {
        "three_way_match",
        "coa_vs_specification",
        "contract_vs_standard_terms",
        "policy_checklist",
    }


@pytest.mark.parametrize("pack", PACKS, ids=lambda p: p.name)
def test_pack_config_prompts_and_samples_agree(pack: Path):
    cfg = load_config(pack / "docreview.yaml")
    assert cfg.adapter == "local"
    assert "STARTER EXAMPLE" in (pack / "docreview.yaml").read_text(encoding="utf-8").splitlines()[0]
    assert (pack / "README.md").is_file()

    library = PromptLibrary(cfg.prompt_dirs)
    for name in required_prompts(cfg):
        assert library.has(name), f"{pack.name}: missing prompt {name}.md"
    for dt in cfg.doc_types.values():
        if dt.review_enabled:
            prompt = library.get(dt.review_prompt or f"review/{dt.name}")
            assert prompt.path.is_relative_to(pack / "prompts"), f"{pack.name}: review prompts belong to the pack"
            assert "policies" in str(prompt.meta.get("notice", "")), f"{pack.name}: {prompt.name} needs the notice"
            for section in ("## What to check", "## What NOT to flag", "## Severity", "## How to report"):
                assert section in prompt.template, f"{pack.name}: {prompt.name} lacks {section!r}"
            prompt.render(**REVIEW_VARIABLES)  # raises on a misspelled {{ variable }}
            for rule in dt.references:
                assert rule.doc_type in cfg.doc_types, f"{pack.name}: {rule.name} names an unknown type"

    expected = yaml.safe_load((pack / "expected.yaml").read_text(encoding="utf-8"))["documents"]
    root = pack / cfg.adapter_options["root"]
    reviewed = 0
    for doc, want in expected.items():
        assert (root / doc).is_file(), f"{pack.name}: expected.yaml names {doc}, which is not in library/"
        assert want["doc_type"] in cfg.doc_types, f"{pack.name}: {doc} has unknown doc_type {want['doc_type']}"
        if want.get("findings"):
            reviewed += 1
            assert 3 <= len(want["findings"]) <= 4, f"{pack.name}: {doc} should plant 3-4 problems"
            assert cfg.doc_types[want["doc_type"]].review_enabled
            for f in want["findings"]:
                assert f["match"] and f["severity_at_least"] in {"blocker", "major", "minor", "info"}
    assert reviewed, f"{pack.name}: no document under review"
    on_disk = {p.relative_to(root).as_posix() for p in root.rglob("*.txt")}
    assert on_disk == set(expected), f"{pack.name}: library/ and expected.yaml list different documents"
