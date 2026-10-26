"""Small helpers shared by the adapters' ``render_schema`` methods."""

from __future__ import annotations

from docreview.schema import Namespace


def header(adapter: str, how: str) -> str:
    return (
        f"# Metadata setup for the {adapter} adapter, generated from your docreview.yaml by `docreview templates`.\n"
        f"# {how}\n# Re-run after changing document types or fields.\n"
    )


def key_lines(ns: Namespace) -> list[str]:
    return [
        f"#   {k.name} ({k.type}){' - person-editable' if k.person_editable else ''}: {k.description}" for k in ns.keys
    ]
