"""The request we send to the Claude API, checked against a stand-in SDK client (no network)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from docreview.config import LLMConfig
from docreview.llm import FALLBACK_BETA, AnthropicModelClient, Attachment, ModelError, ModelRefusal, ModelRequest
from docreview.models import DocumentContent, DocumentRef


class _Messages:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def _client(stop_reason="end_turn", text='{"ok": true}'):
    resp = SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(category="cyber"),
        content=[SimpleNamespace(type="text", text=text)],
        model="m",
        usage={"input_tokens": 3},
    )
    msgs = _Messages(resp)
    return SimpleNamespace(messages=msgs, beta=SimpleNamespace(messages=msgs)), msgs


def _request(media_type="application/pdf", data=b"%PDF-1.4"):
    doc = DocumentContent(DocumentRef("1", "a.pdf"), data, media_type)
    return ModelRequest("review", "sys", "check it", {"type": "object"}, [Attachment("Document under review", doc)])


def _cfg(**kw):
    return LLMConfig(models={"default": "m"}, effort={}, **kw)


def test_request_shape_with_fallbacks():
    sdk, msgs = _client()
    resp = AnthropicModelClient(_cfg(), client=sdk).complete(_request(), model="m", effort="high")
    assert resp.data == {"ok": True}
    k = msgs.kwargs
    assert k["betas"] == [FALLBACK_BETA] and k["fallbacks"] == "default"
    assert k["output_config"] == {"format": {"type": "json_schema", "schema": {"type": "object"}}, "effort": "high"}
    blocks = k["messages"][0]["content"]
    assert blocks[0]["type"] == "document" and blocks[0]["source"]["media_type"] == "application/pdf"
    assert blocks[-1] == {"type": "text", "text": "check it"}  # documents first, instruction last
    assert "thinking" not in k and "temperature" not in k


def test_fallbacks_off_uses_plain_endpoint():
    sdk, msgs = _client()
    AnthropicModelClient(_cfg(fallbacks="off"), client=sdk).complete(_request(), model="m", effort=None)
    assert "betas" not in msgs.kwargs and "effort" not in msgs.kwargs["output_config"]


def test_text_documents_are_sent_as_text():
    sdk, msgs = _client()
    AnthropicModelClient(_cfg(), client=sdk).complete(_request("text/plain", b"hello"), model="m", effort=None)
    assert msgs.kwargs["messages"][0]["content"][0]["source"] == {
        "type": "text",
        "media_type": "text/plain",
        "data": "hello",
    }


@pytest.mark.parametrize(("stop", "exc"), [("refusal", ModelRefusal), ("max_tokens", ModelError)])
def test_bad_stops_raise(stop, exc):
    sdk, _ = _client(stop_reason=stop)
    with pytest.raises(exc):
        AnthropicModelClient(_cfg(), client=sdk).complete(_request(), model="m", effort=None)


def test_oversized_and_unsupported_documents_raise():
    sdk, _ = _client()
    c = AnthropicModelClient(_cfg(max_document_mb=0.000001), client=sdk)
    with pytest.raises(ModelError, match="limit"):
        c.complete(_request(), model="m", effort=None)
    with pytest.raises(ModelError, match="unsupported"):
        AnthropicModelClient(_cfg(), client=sdk).complete(_request("application/zip"), model="m", effort=None)


def test_invalid_json_raises():
    sdk, _ = _client(text="not json")
    with pytest.raises(ModelError, match="invalid JSON"):
        AnthropicModelClient(_cfg(), client=sdk).complete(_request(), model="m", effort=None)


def test_provider_selects_client(monkeypatch):
    import anthropic

    made = {}

    class Fake:
        def __init__(self, **kw):
            made.update(kw)

    monkeypatch.setattr(anthropic, "AnthropicVertex", Fake)
    c = AnthropicModelClient(_cfg(provider="vertex", provider_options={"project_id": "p", "region": "global"}))
    assert isinstance(c.client, Fake) and made == {"project_id": "p", "region": "global"}
