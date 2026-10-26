"""Adapter lookup: a built-in name, an installed plugin, or ``package.module:ClassName``."""

from __future__ import annotations

import importlib
from importlib.metadata import entry_points
from typing import Any

from docreview.ecm.base import NS_CLASSIFICATION, NS_FIELDS, NS_REVIEW, ECMAdapter

__all__ = ["ECMAdapter", "NS_CLASSIFICATION", "NS_FIELDS", "NS_REVIEW", "adapter_class", "load_adapter"]

_BUILTIN = {
    "local": "docreview.ecm.local:LocalFolderAdapter",
    "box": "docreview.ecm.box:BoxAdapter",
    "sharepoint": "docreview.ecm.sharepoint:SharePointAdapter",
    # Example adapters: follow each vendor's documented API but are not tested against a live system.
    "veeva_vault": "docreview.ecm.veeva_vault:VeevaVaultAdapter",
    "mfiles": "docreview.ecm.mfiles:MFilesAdapter",
    "alfresco": "docreview.ecm.alfresco:AlfrescoAdapter",
    "s3": "docreview.ecm.s3:S3Adapter",
}
ENTRY_POINT_GROUP = "docreview.adapters"


def _import(target: str) -> type:
    module, _, attr = target.partition(":")
    if not attr:
        raise ValueError(f"adapter {target!r}: expected 'package.module:ClassName'")
    return getattr(importlib.import_module(module), attr)


def adapter_class(name: str) -> type:
    """The adapter class for a built-in name, an installed plugin, or ``package.module:ClassName``."""
    if name in _BUILTIN:
        cls = _import(_BUILTIN[name])
    elif ":" in name:
        cls = _import(name)
    else:
        found = [ep for ep in entry_points(group=ENTRY_POINT_GROUP) if ep.name == name]
        if not found:
            raise ValueError(f"no ECM adapter named {name!r} (built-in: {sorted(_BUILTIN)})")
        cls = found[0].load()
    return cls


def load_adapter(name: str, options: dict[str, Any]) -> ECMAdapter:
    """Build the adapter named in config. Every adapter takes its options as keyword arguments.

    ``_base_dir`` (the config file's folder) is always passed so relative paths resolve.
    """
    cls = adapter_class(name)
    adapter = cls(**options)
    if not isinstance(adapter, ECMAdapter):
        raise TypeError(f"{cls.__name__} does not implement every ECMAdapter method")
    return adapter
