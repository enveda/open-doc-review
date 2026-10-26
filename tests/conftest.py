from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from docreview.cli import build
from docreview.config import load_config
from docreview.llm import ModelRequest, ModelResponse

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class FakeModel:
    """Answers by task from canned data; records every request."""

    def __init__(self) -> None:
        self.requests: list[tuple[ModelRequest, str, str | None]] = []
        self.findings: list[dict[str, Any]] = [
            {
                "title": "Quantity exceeds order",
                "severity": "major",
                "body": "Line 1 invoices 550; PO-7730 ordered 500.",
                "page": None,
                "reference": "Purchase order",
            },
            {
                "title": "Unit price differs",
                "severity": "major",
                "body": "Bracket billed at 0.95; agreed 0.85.",
                "page": None,
                "reference": "Purchase order",
            },
            {
                "title": "Total mis-added",
                "severity": "blocker",
                "body": "Lines sum to 1,450.00; total says 1,460.00.",
                "page": None,
                "reference": None,
            },
            {
                "title": "Payment terms wording",
                "severity": "info",
                "body": "'30 days' vs 'Net 30'.",
                "page": None,
                "reference": "Purchase order",
            },
        ]

    def complete(self, request: ModelRequest, *, model: str, effort: str | None) -> ModelResponse:
        self.requests.append((request, model, effort))
        name = request.attachments[0].content.ref.name
        if request.task == "classify":
            types = {"INV": "invoice", "EBR": "executed_batch_record", "MBR": "master_batch_record"}
            data = {
                "doc_type": types.get(name[:3], "purchase_order"),
                "confidence": "high",
                "reason": "header",
            }
        elif request.task == "extract" and name.startswith("EBR"):
            data = {
                "batch_number": "B24-0117",
                "mbr_number": "MBR-0042",
                "mbr_version": "3.0",
                "product": "Examplamide Tablets 50 mg",
                "manufacture_date": "2026-09-22",
            }
        elif request.task == "extract" and name.startswith("MBR"):
            data = {"mbr_number": "mbr-0042", "mbr_version": "3.0", "product": "Examplamide Tablets 50 mg"}
        elif request.task == "extract":
            if name.startswith("INV"):
                data = {
                    "invoice_number": "INV-2041",
                    "po_number": "PO-7730",
                    "supplier": "Acme Widgets Co.",
                    "invoice_date": "2026-09-18",
                    "total": 1460.0,
                    "currency": "USD",
                }
            else:
                data = {
                    "po_number": "po-7730 ",
                    "supplier": "Acme Widgets Co.",
                    "order_date": "2026-09-01",
                    "total": 1265.0,
                    "currency": "USD",
                }
        else:
            data = {"summary": "Three discrepancies against the purchase order.", "findings": self.findings}
        return ModelResponse(data=data, model=model, usage={"input_tokens": 1, "output_tokens": 1})


BATCH_FILES = ("inbox/EBR-B24-0117.txt", "master_batch_records/MBR-0042.txt")


@pytest.fixture
def full_workspace(tmp_path: Path) -> Path:
    shutil.copytree(EXAMPLES, tmp_path / "examples")
    return tmp_path / "examples" / "generic"


@pytest.fixture
def workspace(full_workspace: Path) -> Path:
    """The example with only the invoice and purchase order (most tests count calls on those two)."""
    for rel in BATCH_FILES:
        (full_workspace / "library" / rel).unlink()
    return full_workspace


@pytest.fixture
def fake_model() -> FakeModel:
    return FakeModel()


@pytest.fixture
def pipeline(workspace: Path, fake_model: FakeModel):
    return build(load_config(workspace / "docreview.yaml"), model_client=fake_model)
