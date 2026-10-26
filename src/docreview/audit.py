"""Append-only record of every model call: what was asked, of which model, and what came back.

One JSON object per line. Prompts and model output are recorded in full, so the log holds
extracted values and findings that quote the documents: protect it like the documents. The
attached documents themselves are recorded only as a SHA-256, never their bytes.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def write(self, record: dict[str, Any]) -> str:
        record_id = record.get("id") or uuid.uuid4().hex
        row = {"id": record_id, "at": datetime.now(UTC).isoformat(), **record}
        line = json.dumps(row, default=str, ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # A failure here must surface: an AI result without its audit record is not usable.
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return record_id
