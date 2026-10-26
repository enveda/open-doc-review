"""The offline cost estimate: page counting, the call pattern, prices and overrides (no network)."""

from __future__ import annotations

import io
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter

from docreview.config import load_config
from docreview.ecm.local import LocalFolderAdapter
from docreview.estimate import CHARS_PER_TOKEN, OUTPUT_PER_CALL, OVERHEAD_PER_CALL, PRICES, estimate, measure

EXAMPLE = Path(__file__).parents[1] / "examples" / "generic"


def _pdf(pages: int) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _cfg(window=100, reviewed=True, models=None, prices=None, provider="anthropic"):
    models = models or {"default": "claude-opus-5-5"}
    llm = SimpleNamespace(
        provider=provider, prices=prices, model_for=lambda task: models.get(task) or models["default"]
    )
    types = {
        "invoice": SimpleNamespace(review_enabled=reviewed),
        "purchase_order": SimpleNamespace(review_enabled=False),
    }
    return SimpleNamespace(llm=llm, review=SimpleNamespace(window_pages=window), doc_types=types)


def test_measure_counts_pdf_pages_text_images_and_unknowns():
    assert measure("a.pdf", _pdf(3), "application/pdf").pages == 3
    assert measure("a.pdf", _pdf(3), "application/pdf").tokens == (4_500, 9_000)
    text = measure("a.txt", b"x" * 400, "text/plain")
    assert (text.pages, text.tokens) == (1, (400 // CHARS_PER_TOKEN, 400 // CHARS_PER_TOKEN))
    assert measure("a.png", b"\x89PNG", "image/png").pages == 1
    broken = measure("b.pdf", b"not a pdf", "application/pdf")
    assert broken.pages == 1 and "unreadable" in broken.note
    assert "not sent as-is" in measure("c.docx", b"PK", "application/msword").note


def test_call_pattern_and_token_ranges():
    docs = [("long.pdf", _pdf(250), "application/pdf"), ("short.pdf", _pdf(2), "application/pdf")]
    est = estimate(_cfg(window=100), docs)
    assert est.pages == 252
    # low: classify + extract for each document; high: plus 3 + 1 review windows
    assert est.calls == (4, 8)
    own_low, own_high = 252 * 1_500, 252 * 3_000
    assert est.input_tokens[0] == 2 * own_low + 4 * OVERHEAD_PER_CALL
    assert est.input_tokens[1] == 3 * own_high + 8 * OVERHEAD_PER_CALL
    out_hi = 2 * (OUTPUT_PER_CALL["classify"][1] + OUTPUT_PER_CALL["extract"][1]) + 4 * OUTPUT_PER_CALL["review"][1]
    assert est.output_tokens[1] == out_hi
    p_in, p_out = PRICES["claude-opus-5-5"]
    low, high = est.costs["as configured (claude-opus-5-5)"]
    assert low == pytest.approx((est.input_tokens[0] * p_in + est.output_tokens[0] * p_out) / 1e6)
    assert high == pytest.approx((est.input_tokens[1] * p_in + est.output_tokens[1] * p_out) / 1e6)
    assert "all calls on claude-sonnet-5-5" in est.costs


def test_no_review_and_no_windows():
    docs = [("a.pdf", _pdf(250), "application/pdf")]
    assert estimate(_cfg(reviewed=False), docs).calls == (2, 2)
    assert estimate(_cfg(window=0), docs).calls == (2, 3)  # window_pages 0: one review call per document


def test_price_overrides_and_unknown_models():
    docs = [("a.txt", b"hello world", "text/plain")]
    est = estimate(_cfg(models={"default": "my-model"}), docs)
    assert est.costs["as configured (my-model)"] is None
    assert "no price known" in est.format()
    priced = estimate(_cfg(models={"default": "my-model"}, prices={"my-model": {"input": 1, "output": 2}}), docs)
    low, _ = priced.costs["as configured (my-model)"]
    assert low == pytest.approx((priced.input_tokens[0] * 1 + priced.output_tokens[0] * 2) / 1e6)
    mixed = estimate(_cfg(models={"default": "claude-opus-5-5", "classify": "claude-haiku-5-5"}), docs)
    assert "as configured (claude-haiku-5-5, claude-opus-5-5)" in mixed.costs


def test_format_states_assumptions_and_estimate_only():
    text = estimate(_cfg(provider="bedrock"), [("a.pdf", _pdf(1), "application/pdf")]).format()
    for part in (
        "Documents:      1",
        "Pages:          1",
        "Assumptions:",
        "bedrock prices separately",
        "ESTIMATE ONLY",
    ):
        assert part in text


def test_local_adapter_and_the_example_config(tmp_path):
    shutil.copytree(EXAMPLE, tmp_path / "ex")
    cfg = load_config(tmp_path / "ex" / "docreview.yaml")
    adapter = LocalFolderAdapter(root="library", _base_dir=str(tmp_path / "ex"))
    est = estimate(cfg, adapter)
    assert len(est.documents) == len(list(adapter.poll(None)[0])) > 0
    assert est.calls[0] == 2 * len(est.documents)
    assert not list((tmp_path / "ex").rglob("*.docreview.json"))  # nothing written
