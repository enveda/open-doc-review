"""Offline cost estimate: how much a run over a set of documents might cost, before any model call.

Nothing here contacts a model. It counts pages, applies the call pattern the pipeline uses
(classify + extract per document, plus one review call per page window for types with review on)
and multiplies by list prices. Every number is a range, and the assumptions are printed with it.
"""

from __future__ import annotations

import io
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

# US$ per million tokens (input, output): Claude API list prices as of 2026-10-06, without prompt-cache
# discounts. Bedrock and Vertex AI price separately. Override or extend with `llm.prices` in the config.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.00, 50.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-5-5": (0.10, 0.50),  # prompts up to 100K tokens; 0.50 / 2.50 beyond
    "claude-haiku-4-5": (1.00, 5.00),
}
PRICES_AS_OF = "2026-10-06"

TOKENS_PER_PAGE = (1_500, 3_000)  # a PDF page sent as text plus page image
CHARS_PER_TOKEN = 4  # plain text
OVERHEAD_PER_CALL = 2_000  # system prompt, instructions, field list, schema
OUTPUT_PER_CALL = {"classify": (300, 1_500), "extract": (1_000, 4_000), "review": (3_000, 12_000)}  # incl. thinking
_IMAGES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

Document = tuple[str, bytes, str]  # (name, bytes, media type)


@dataclass(frozen=True)
class DocSize:
    name: str
    pages: int
    tokens: tuple[int, int]  # the document's own input tokens, low and high
    note: str = ""


@dataclass
class Estimate:
    documents: list[DocSize]
    window_pages: int
    reviewed_types: int
    doc_types: int
    calls: tuple[int, int]
    input_tokens: tuple[int, int]
    output_tokens: tuple[int, int]
    costs: dict[str, tuple[float, float] | None]  # label -> (low, high) US$, None when a price is missing
    assumptions: list[str] = field(default_factory=list)

    @property
    def pages(self) -> int:
        return sum(d.pages for d in self.documents)

    def format(self) -> str:
        def rng(pair: tuple[float, float], fmt: str = "{:,.0f}") -> str:
            lo, hi = (fmt.format(v) for v in pair)
            return lo if lo == hi else f"{lo} to {hi}"

        lines = [
            "Cost estimate (offline; no model was called)",
            f"  Documents:      {len(self.documents)}",
            f"  Pages:          {self.pages:,}",
            f"  Model calls:    {rng(self.calls)}",
            f"  Input tokens:   {rng(self.input_tokens)}",
            f"  Output tokens:  {rng(self.output_tokens)}",
            "  Cost (US$):",
        ]
        for label, cost in self.costs.items():
            text = rng(cost, "{:,.2f}") if cost is not None else "no price known; add it under llm.prices"
            lines.append(f"    {label}: {text}")
        notes = [d for d in self.documents if d.note]
        if notes:
            lines.append("  Documents counted approximately:")
            lines += [f"    {d.name}: {d.note}" for d in notes]
        lines.append("  Assumptions:")
        lines += [f"    - {a}" for a in self.assumptions]
        lines.append("ESTIMATE ONLY. Actual cost depends on the documents, prompts, model behaviour and caching;")
        lines.append("measure it on a sample run (the audit log records token use for every call).")
        return "\n".join(lines)


def measure(name: str, data: bytes, media_type: str) -> DocSize:
    """Pages and input tokens of one document."""
    if media_type == "application/pdf":
        try:
            from pypdf import PdfReader

            pages = len(PdfReader(io.BytesIO(data)).pages)
        except Exception as exc:  # encrypted or damaged: count it as one page and say so
            return DocSize(name, 1, TOKENS_PER_PAGE, f"page count unreadable ({type(exc).__name__}); counted as 1 page")
        return DocSize(name, pages, (pages * TOKENS_PER_PAGE[0], pages * TOKENS_PER_PAGE[1]))
    if media_type.startswith("text/"):
        tokens = max(1, math.ceil(len(data.decode("utf-8", errors="replace")) / CHARS_PER_TOKEN))
        return DocSize(name, 1, (tokens, tokens))
    if media_type in _IMAGES:
        return DocSize(name, 1, TOKENS_PER_PAGE)
    return DocSize(name, 1, TOKENS_PER_PAGE, f"{media_type} is not sent as-is; counted as 1 page")


def _documents(source: Any) -> Iterable[Document]:
    """An iterable of (name, bytes, media type), or an adapter: the documents its ``poll`` would offer."""
    if hasattr(source, "poll") and hasattr(source, "download"):
        events, _ = source.poll(None)
        for ref in {e.ref.id: e.ref for e in events}.values():
            content = source.download(ref)
            yield ref.name, content.data, content.media_type
    else:
        yield from source


def _prices(cfg: Any) -> dict[str, tuple[float, float]]:
    out = dict(PRICES)
    for model, p in (getattr(cfg.llm, "prices", None) or {}).items():  # llm.prices: {model: {input, output}}
        out[model] = (float(p["input"]), float(p["output"]))
    return out


def estimate(cfg: Any, adapter_or_paths: Any) -> Estimate:
    docs = [measure(*d) for d in _documents(adapter_or_paths)]
    window = cfg.review.window_pages
    types = list(cfg.doc_types.values())
    reviewed = sum(1 for t in types if t.review_enabled)
    prices = _prices(cfg)

    # per task: (calls, input tokens, output tokens) as (low, high); low = no document reviewed,
    # high = every document reviewed (the type is unknown until classification has run)
    per_task: dict[str, list[tuple[int, int]]] = {}
    n = len(docs)
    for task in ("classify", "extract"):
        tin = [sum(d.tokens[i] for d in docs) + n * OVERHEAD_PER_CALL for i in (0, 1)]
        out = [n * OUTPUT_PER_CALL[task][i] for i in (0, 1)]
        per_task[task] = [(n, n), (tin[0], tin[1]), (out[0], out[1])]
    windows = sum(1 if window <= 0 else max(1, math.ceil(d.pages / window)) for d in docs) if reviewed else 0
    review_in = sum(d.tokens[1] for d in docs) + windows * OVERHEAD_PER_CALL if reviewed else 0
    per_task["review"] = [(0, windows), (0, review_in), (0, windows * OUTPUT_PER_CALL["review"][1])]

    def total(i: int) -> tuple[int, int]:
        return sum(v[i][0] for v in per_task.values()), sum(v[i][1] for v in per_task.values())

    def cost(model_for: Mapping[str, str]) -> tuple[float, float] | None:
        lo = hi = 0.0
        for task, (_, tin, tout) in per_task.items():
            if (tin[1] or tout[1]) and model_for[task] not in prices:
                return None
            p_in, p_out = prices.get(model_for[task], (0.0, 0.0))
            lo += (tin[0] * p_in + tout[0] * p_out) / 1e6
            hi += (tin[1] * p_in + tout[1] * p_out) / 1e6
        return lo, hi

    configured = {t: cfg.llm.model_for(t) for t in per_task}
    label = ", ".join(sorted({f"{m}" for m in configured.values()}))
    costs: dict[str, tuple[float, float] | None] = {f"as configured ({label})": cost(configured)}
    for model in sorted(set(prices) - set(configured.values())):
        costs[f"all calls on {model}"] = cost(dict.fromkeys(per_task, model))

    provider = getattr(cfg.llm, "provider", "anthropic")
    assumptions = [
        f"Prices: Claude API list prices as of {PRICES_AS_OF} (US$ per million tokens), no cache discount"
        + ("" if provider == "anthropic" else f"; {provider} prices separately, so set llm.prices"),
        f"A PDF page is {TOKENS_PER_PAGE[0]:,}-{TOKENS_PER_PAGE[1]:,} input tokens; text is 1 token per "
        f"{CHARS_PER_TOKEN} characters; an image counts as one page",
        f"Every call adds about {OVERHEAD_PER_CALL:,} input tokens of prompt and schema",
        "Output per call, thinking included: "
        + "; ".join(f"{t} {lo:,}-{hi:,}" for t, (lo, hi) in OUTPUT_PER_CALL.items())
        + " tokens (review per window)",
        f"Low end: no document is reviewed. High end: all are ({reviewed} of {len(types)} document types "
        "have review on; the type is known only after classification)",
        f"Review runs one call per {window}-page window" if window > 0 else "Review runs one call per document",
        "Not counted: reference documents attached to review calls (each adds its pages to every window), "
        "retries, refusal fallbacks and re-reviews",
    ]
    return Estimate(
        documents=docs,
        window_pages=window,
        reviewed_types=reviewed,
        doc_types=len(types),
        calls=total(0),
        input_tokens=total(1),
        output_tokens=total(2),
        costs=costs,
        assumptions=assumptions,
    )
