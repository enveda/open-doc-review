"""Load and validate the YAML configuration (see docs/CONFIGURATION.md)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from docreview.models import Severity

DEFAULT_MODEL = "claude-opus-5-5"
PROVIDERS = {"anthropic", "bedrock", "vertex", "foundry"}
FIELD_TYPES = {"string", "number", "integer", "boolean", "date"}
_ID = re.compile(r"[a-zA-Z][a-zA-Z0-9_]*")
_PLACEHOLDER = re.compile(r"\{([a-zA-Z0-9_]+)\}")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str = "string"
    description: str = ""


@dataclass(frozen=True)
class ReferenceRule:
    """How to find one document a reviewed document must agree with.

    ``match`` maps a field of the *reference* document type to a template filled from the
    *reviewed* document's extracted fields, e.g. ``{"po_number": "{po_number}"}``.
    """

    name: str
    doc_type: str
    match: dict[str, str]
    required: bool = False
    same_folder: bool = False  # restrict the search to the reviewed document's folder

    def placeholders(self) -> set[str]:
        return {p for tmpl in self.match.values() for p in _PLACEHOLDER.findall(tmpl)}

    def inverse(self) -> dict[str, str] | None:
        """``{reviewed-type field: reference-type field}`` when every template is a bare ``{field}``,
        so a newly arrived reference can find the documents that cite it; else ``None``."""
        out = {}
        for target, template in self.match.items():
            m = _PLACEHOLDER.fullmatch(template.strip())
            if not m:
                return None
            out[m.group(1)] = target
        return out


@dataclass(frozen=True)
class DocTypeConfig:
    name: str
    description: str
    fields: tuple[FieldSpec, ...] = ()
    review_enabled: bool = False
    review_prompt: str | None = None  # prompt name, e.g. "review/invoice"
    references: tuple[ReferenceRule, ...] = ()


@dataclass(frozen=True)
class LLMConfig:
    models: dict[str, str]
    effort: dict[str, str]
    fallbacks: str = "default"  # "default" = server-side refusal fallback; "off" to disable
    provider: str = "anthropic"  # anthropic | bedrock | vertex | foundry
    provider_options: dict[str, Any] = field(default_factory=dict)  # passed to the provider's client
    max_tokens: int = 16000
    max_document_mb: float = 30.0

    def model_for(self, task: str) -> str:
        return self.models.get(task) or self.models.get("default") or DEFAULT_MODEL

    def effort_for(self, task: str) -> str | None:
        return self.effort.get(task) or self.effort.get("default")


@dataclass(frozen=True)
class ReviewSettings:
    post_comments: bool = True
    comment_min_severity: Severity = Severity.MINOR
    dry_run: bool = False  # model calls are real; every ECM write is skipped and logged
    window_pages: int = 100  # PDFs longer than this are reviewed in windows of this many pages; 0 = never split


@dataclass(frozen=True)
class Config:
    adapter: str
    adapter_options: dict[str, Any]
    llm: LLMConfig
    prompt_dirs: tuple[Path, ...]
    review: ReviewSettings
    audit_path: Path
    state_path: Path
    max_attempts: int = 3  # failures on one version before polling stops retrying it
    doc_types: dict[str, DocTypeConfig] = field(default_factory=dict)
    base_dir: Path = Path(".")

    def doc_type(self, name: str) -> DocTypeConfig:
        try:
            return self.doc_types[name]
        except KeyError:
            raise ConfigError(f"unknown document type {name!r}") from None


def _expand_env(value: Any) -> Any:
    """``${VAR}`` in any string value is replaced from the environment (secrets stay out of YAML)."""
    if isinstance(value, str):

        def sub(m: re.Match[str]) -> str:
            if m.group(1) not in os.environ:
                raise ConfigError(f"environment variable {m.group(1)} is not set")
            return os.environ[m.group(1)]

        return re.sub(r"\$\{([A-Z0-9_]+)\}", sub, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _path(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


def _parse_fields(dt: str, raw: dict[str, Any] | None) -> tuple[FieldSpec, ...]:
    out = []
    for name, spec in (raw or {}).items():
        spec = spec or {}
        if isinstance(spec, str):
            spec = {"description": spec}
        if unknown := set(spec) - {"type", "description"}:
            raise ConfigError(
                f"{dt}.fields.{name}: unknown keys {sorted(map(str, unknown))} "
                "(a comma inside an unquoted description splits it; quote the description)"
            )
        ftype = spec.get("type", "string")
        if ftype not in FIELD_TYPES:
            raise ConfigError(f"{dt}.fields.{name}: type must be one of {sorted(FIELD_TYPES)}")
        if not _ID.fullmatch(str(name)):
            raise ConfigError(f"{dt}.fields.{name}: use letters, digits and underscores, starting with a letter")
        out.append(FieldSpec(name, ftype, spec.get("description", "")))
    return tuple(out)


def _parse_doc_type(name: str, raw: Any) -> DocTypeConfig:
    if not _ID.fullmatch(str(name)) or name == "unknown":
        raise ConfigError(f"doc_types.{name}: ids use letters, digits and underscores; 'unknown' is reserved")
    if not isinstance(raw, dict):
        raise ConfigError(f"doc_types.{name}: expected a mapping with description, fields and review")
    if not raw.get("description"):
        raise ConfigError(f"doc_types.{name}: a description is required (the classifier reads it)")
    review = raw.get("review") or {}
    refs = []
    for r in review.get("references") or []:
        try:
            refs.append(
                ReferenceRule(
                    name=r["name"],
                    doc_type=r["doc_type"],
                    match=dict(r["match"]),
                    required=bool(r.get("required", False)),
                    same_folder=bool(r.get("same_folder", False)),
                )
            )
        except KeyError as exc:
            raise ConfigError(f"doc_types.{name}.review.references: missing {exc}") from None
    return DocTypeConfig(
        name=name,
        description=raw["description"].strip(),
        fields=_parse_fields(name, raw.get("fields")),
        review_enabled=bool(review.get("enabled", bool(review))),
        review_prompt=review.get("prompt"),
        references=tuple(refs),
    )


def _validate(cfg: Config) -> None:
    for dt in cfg.doc_types.values():
        own = {f.name for f in dt.fields}
        for ref in dt.references:
            if ref.doc_type not in cfg.doc_types:
                raise ConfigError(f"{dt.name}: reference {ref.name!r} names unknown doc_type {ref.doc_type!r}")
            target = {f.name for f in cfg.doc_types[ref.doc_type].fields}
            if missing := set(ref.match) - target:
                raise ConfigError(
                    f"{dt.name}: reference {ref.name!r} matches on {sorted(missing)}, "
                    f"which {ref.doc_type!r} does not extract"
                )
            if missing := ref.placeholders() - own:
                raise ConfigError(
                    f"{dt.name}: reference {ref.name!r} uses {sorted(missing)}, which {dt.name!r} does not extract"
                )


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    raw = _expand_env(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    base = path.parent
    ecm = raw.get("ecm") or {}
    llm = raw.get("llm") or {}
    review = raw.get("review") or {}
    if not raw.get("doc_types"):
        raise ConfigError("doc_types: define at least one document type")
    try:
        min_sev = Severity(review.get("comment_min_severity", "minor"))
    except ValueError:
        raise ConfigError(f"review.comment_min_severity must be one of {[s.value for s in Severity]}") from None
    fallbacks = str(llm.get("fallbacks", "default"))
    if fallbacks not in {"default", "off"}:
        raise ConfigError("llm.fallbacks must be 'default' or 'off'")
    provider = str(llm.get("provider", "anthropic"))
    if provider not in PROVIDERS:
        raise ConfigError(f"llm.provider must be one of {sorted(PROVIDERS)}")
    if provider != "anthropic" and fallbacks != "off":
        raise ConfigError("llm.fallbacks 'default' is available on the Claude API only; set it to 'off'")
    try:
        cfg = _build(raw, base, ecm, llm, review, min_sev, fallbacks, provider)
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(f"invalid value: {exc}") from None
    _validate(cfg)
    return cfg


def _build(raw, base, ecm, llm, review, min_sev, fallbacks, provider) -> Config:  # noqa: ANN001
    return Config(
        adapter=ecm.get("adapter", "local"),
        adapter_options={**(ecm.get("options") or {}), "_base_dir": str(base)},
        llm=LLMConfig(
            models={"default": DEFAULT_MODEL, **(llm.get("models") or {})},
            effort=dict(llm.get("effort") or {}),
            fallbacks=fallbacks,
            provider=provider,
            provider_options=dict(llm.get("provider_options") or {}),
            max_tokens=int(llm.get("max_tokens", 16000)),
            max_document_mb=float(llm.get("max_document_mb", 30.0)),
        ),
        prompt_dirs=tuple(_path(base, d) for d in (raw.get("prompts") or {}).get("dirs", ["prompts"])),
        review=ReviewSettings(
            post_comments=bool(review.get("post_comments", True)),
            comment_min_severity=min_sev,
            dry_run=bool(review.get("dry_run", False)),
            window_pages=int(review.get("window_pages", 100)),
        ),
        audit_path=_path(base, (raw.get("audit") or {}).get("path", "logs/ai_calls.jsonl")),
        state_path=_path(base, raw.get("state_path", ".docreview_state.json")),
        max_attempts=int(raw.get("max_attempts", 3)),
        doc_types={n: _parse_doc_type(n, d) for n, d in raw["doc_types"].items()},
        base_dir=base,
    )
