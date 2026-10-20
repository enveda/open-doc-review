"""Prompts are plain Markdown files you own. The engine ships none of its own wording.

A prompt is looked up by name (``"system"``, ``"classify"``, ``"review/invoice"``) across the
configured directories; the first directory that has ``<name>.md`` wins, so you can layer a
team's overrides over a shared set.

Placeholders use ``{{ name }}``. Unknown placeholders are an error, so a typo never reaches the
model silently. Available variables are listed in docs/PROMPTS.md.

The engine, not the prompt, defines the response format: every call uses a JSON schema that
the API enforces, so prompts describe *what to look for*, never how to format the answer.
HTML comments (``<!-- ... -->``) are authors' notes and are removed before sending.
Optional YAML front matter (between ``---`` lines) is stripped and kept as ``meta`` — use it
for owner, version, change notes; it is written to the audit log with every call.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_VAR = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")
_COMMENT = re.compile(r"<!--.*?-->\n?", re.S)  # authors' notes: never sent to the model


class PromptError(LookupError):
    pass


@dataclass(frozen=True)
class Prompt:
    name: str
    path: Path
    template: str
    meta: dict[str, Any]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.template.encode("utf-8")).hexdigest()[:16]

    def render(self, **values: Any) -> str:
        def sub(m: re.Match[str]) -> str:
            key = m.group(1)
            cur: Any = values
            for part in key.split("."):
                if not isinstance(cur, dict) or part not in cur:
                    raise PromptError(f"prompt {self.name!r} uses {{{{ {key} }}}}, which is not available")
                cur = cur[part]
            return "" if cur is None else str(cur)

        return _VAR.sub(sub, self.template).strip()


class PromptLibrary:
    def __init__(self, dirs: tuple[Path, ...] | list[Path]) -> None:
        self.dirs = [Path(d) for d in dirs]

    def get(self, name: str) -> Prompt:
        rel = name if name.endswith(".md") else f"{name}.md"
        for d in self.dirs:
            p = (d / rel).resolve()
            if d.resolve() in p.parents and p.is_file():
                return self._parse(name.removesuffix(".md"), p)
        raise PromptError(f"prompt {name!r} not found in {[str(d) for d in self.dirs]}")

    def has(self, name: str) -> bool:
        try:
            self.get(name)
        except PromptError:
            return False
        return True

    @staticmethod
    def _parse(name: str, path: Path) -> Prompt:
        text = path.read_text(encoding="utf-8")
        meta: dict[str, Any] = {}
        if text.startswith("---\n"):
            head, sep, body = text[4:].partition("\n---\n")
            if sep:
                meta = yaml.safe_load(head) or {}
                text = body
        return Prompt(name=name, path=path, template=_COMMENT.sub("", text), meta=meta)
