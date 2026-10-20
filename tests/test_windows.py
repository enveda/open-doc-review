from __future__ import annotations

import io
import json

from pypdf import PdfReader, PdfWriter

from docreview.cli import run_once
from docreview.models import DocumentContent, DocumentRef
from docreview.windows import page_windows


def _pdf(pages: int) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _content(data: bytes, media="application/pdf") -> DocumentContent:
    return DocumentContent(DocumentRef("x", "x.pdf"), data, media)


def test_windows_split_and_describe():
    ws = page_windows(_content(_pdf(5)), 2)
    assert [(w.first_page, w.last_page) for w in ws] == [(1, 2), (3, 4), (5, 5)]
    assert [len(PdfReader(io.BytesIO(w.content.data)).pages) for w in ws] == [2, 2, 1]
    assert ws[1].describe() == "pages 3-4 of 5" and not ws[1].is_whole


def test_no_split_when_short_disabled_text_or_broken():
    assert page_windows(_content(_pdf(3)), 5)[0].describe() == "all 3 pages"
    assert len(page_windows(_content(_pdf(9)), 0)) == 1
    assert page_windows(_content(b"hello", "text/plain"), 1)[0].describe() == "the whole document"
    assert page_windows(_content(b"%PDF-broken"), 1)[0].describe() == "the whole document"


def test_long_pdf_review_maps_pages_back(workspace, pipeline, fake_model):
    from dataclasses import replace

    pipeline.cfg = replace(pipeline.cfg, review=replace(pipeline.cfg.review, window_pages=2))
    (workspace / "library" / "inbox" / "INV-2041.txt").unlink()
    (workspace / "library" / "inbox" / "INV-2041.pdf").write_bytes(_pdf(5))
    fake_model.findings = [
        {"title": "Check", "severity": "minor", "body": "b", "page": 2, "reference": None},
    ]
    run_once(pipeline)
    reviews = [r for r, _, _ in fake_model.requests if r.task == "review"]
    assert len(reviews) == 3
    assert "pages 3-4 of 5" in reviews[1].instruction
    assert [a.label for a in reviews[0].attachments] == ["Document under review", "Purchase order"]
    side = json.loads((workspace / "library" / "inbox" / "INV-2041.pdf.docreview.json").read_text())
    pages = [c["text"] for c in side["comments"] if c["text"].startswith("[MINOR]")]
    # page 2 of the 1-page last window is out of range, so that finding keeps no page
    assert sorted(t.split("(page ")[1][0] if "(page" in t else "-" for t in pages) == ["-", "2", "4"]
