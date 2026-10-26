"""Model access. Every call returns JSON that matches a schema the engine defines, and is audited.

``ModelClient`` is the seam: the Anthropic implementation is the default, and tests use a fake.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from docreview.audit import AuditLog
from docreview.config import LLMConfig
from docreview.models import DocumentContent

FALLBACK_BETA = "server-side-fallback-2026-07-01"
REQUEST_LIMIT_BYTES = 32_000_000


class ModelError(RuntimeError):
    pass


class ModelRefusal(ModelError):
    pass


@dataclass(frozen=True)
class Attachment:
    label: str  # how the prompt refers to it, e.g. "Document under review" or "Purchase order"
    content: DocumentContent


@dataclass
class ModelRequest:
    task: str  # "classify" | "extract" | "review" — selects model and effort from config
    system: str
    instruction: str
    schema: dict[str, Any]
    attachments: list[Attachment] = field(default_factory=list)


@dataclass
class ModelResponse:
    data: dict[str, Any]
    model: str
    usage: dict[str, Any]
    request_id: str | None = None


class ModelClient(Protocol):
    def complete(self, request: ModelRequest, *, model: str, effort: str | None) -> ModelResponse: ...


def content_block(att: Attachment, max_mb: float) -> dict[str, Any]:
    c = att.content
    if len(c.data) > max_mb * 1024 * 1024:
        raise ModelError(f"{c.ref.name} is {len(c.data) / 1e6:.1f} MB; the limit is {max_mb} MB (llm.max_document_mb)")
    title = f"{att.label}: {c.ref.name}"
    if c.media_type == "application/pdf":
        data = base64.standard_b64encode(c.data).decode("ascii")
        return {
            "type": "document",
            "title": title,
            "source": {"type": "base64", "media_type": "application/pdf", "data": data},
        }
    if c.media_type.startswith("text/"):
        text = c.data.decode("utf-8", errors="replace")
        return {
            "type": "document",
            "title": title,
            "source": {"type": "text", "media_type": "text/plain", "data": text},
        }
    if c.media_type in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
        data = base64.standard_b64encode(c.data).decode("ascii")
        return {"type": "image", "source": {"type": "base64", "media_type": c.media_type, "data": data}}
    raise ModelError(f"{c.ref.name}: unsupported media type {c.media_type} (convert it to PDF first)")


class AnthropicModelClient:
    """Claude via the official SDK. Credentials come from the environment (ANTHROPIC_API_KEY, ...)."""

    def __init__(self, cfg: LLMConfig, client: Any | None = None) -> None:
        import anthropic

        self.cfg = cfg
        if client is None:
            factory = {
                "anthropic": anthropic.Anthropic,  # ANTHROPIC_API_KEY or an `ant auth login` profile
                "bedrock": anthropic.AnthropicBedrockMantle,  # aws_region, aws_profile, ...; AWS credential chain
                "vertex": anthropic.AnthropicVertex,  # project_id, region; Google application default credentials
                "foundry": anthropic.AnthropicFoundry,  # resource, api_key
            }[cfg.provider]
            client = factory(**cfg.provider_options)
        self.client = client

    def complete(self, request: ModelRequest, *, model: str, effort: str | None) -> ModelResponse:
        content: list[dict[str, Any]] = []
        for att in request.attachments:
            block = content_block(att, self.cfg.max_document_mb)
            if block["type"] == "image":  # images carry no title, so label them in text
                content.append({"type": "text", "text": f"{att.label}: {att.content.ref.name}"})
            content.append(block)
        content.append({"type": "text", "text": request.instruction})
        size = sum(len(b.get("source", {}).get("data", "")) for b in content)  # after base64 encoding
        if size > REQUEST_LIMIT_BYTES:
            raise ModelError(
                f"documents for this call total {size / 1e6:.1f} MB once encoded; the API accepts about "
                f"{REQUEST_LIMIT_BYTES / 1e6:.0f} MB. Lower review.window_pages or use smaller references."
            )
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": request.schema}}
        if effort:
            output_config["effort"] = effort
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": self.cfg.max_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": content}],
            "output_config": output_config,
        }
        if self.cfg.fallbacks == "default":
            response = self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
        else:
            response = self.client.messages.create(**kwargs)

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise ModelRefusal(f"model declined the request ({getattr(details, 'category', None) or 'no category'})")
        if response.stop_reason == "max_tokens":
            raise ModelError(f"response cut off at max_tokens={self.cfg.max_tokens}; raise llm.max_tokens")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise ModelError("model returned no text block")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ModelError(f"model returned invalid JSON: {exc}") from exc
        usage = response.usage.to_dict() if hasattr(response.usage, "to_dict") else dict(response.usage or {})
        return ModelResponse(
            data=data, model=response.model, usage=usage, request_id=getattr(response, "_request_id", None)
        )


class AIService:
    """Model calls with config-driven model choice and an audit record for each one."""

    def __init__(self, client: ModelClient, cfg: LLMConfig, audit: AuditLog) -> None:
        self.client = client
        self.cfg = cfg
        self.audit = audit

    def call(
        self, request: ModelRequest, *, run_id: str, document_id: str, prompt_meta: dict[str, Any]
    ) -> ModelResponse:
        model = self.cfg.model_for(request.task)
        effort = self.cfg.effort_for(request.task)
        base = {
            "run_id": run_id,
            "task": request.task,
            "document_id": document_id,
            "model_requested": model,
            "effort": effort,
            "prompts": prompt_meta,
            "system": request.system,
            "instruction": request.instruction,
            "attachments": [
                {
                    "label": a.label,
                    "document_id": a.content.ref.id,
                    "version": a.content.ref.version,
                    "sha256": hashlib.sha256(a.content.data).hexdigest(),
                }
                for a in request.attachments
            ],
        }
        started = time.monotonic()
        try:
            resp = self.client.complete(request, model=model, effort=effort)
        except Exception as exc:
            self.audit.write(
                {
                    **base,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "seconds": round(time.monotonic() - started, 2),
                }
            )
            raise
        self.audit.write(
            {
                **base,
                "status": "ok",
                "model_used": resp.model,
                "usage": resp.usage,
                "request_id": resp.request_id,
                "output": resp.data,
                "seconds": round(time.monotonic() - started, 2),
            }
        )
        return resp
