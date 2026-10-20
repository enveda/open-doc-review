"""Split a long PDF into page windows, so a document of any length can be reviewed."""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, replace

from docreview.models import DocumentContent

log = logging.getLogger("docreview")


@dataclass(frozen=True)
class Window:
    first_page: int  # 1-based, in the whole document
    last_page: int
    total_pages: int | None
    content: DocumentContent

    @property
    def is_whole(self) -> bool:
        return self.total_pages is None or (self.first_page == 1 and self.last_page == self.total_pages)

    def describe(self) -> str:
        if self.total_pages is None:
            return "the whole document"
        if self.is_whole:
            return f"all {self.total_pages} pages"
        return f"pages {self.first_page}-{self.last_page} of {self.total_pages}"


def page_windows(content: DocumentContent, window_pages: int) -> list[Window]:
    """One window for non-PDFs, short PDFs, ``window_pages <= 0``, or a PDF we cannot split."""
    whole = [Window(1, 1, None, content)]
    if content.media_type != "application/pdf" or window_pages <= 0:
        return whole
    try:
        from pypdf import PdfReader, PdfWriter

        reader = PdfReader(io.BytesIO(content.data))
        total = len(reader.pages)
    except Exception as exc:  # encrypted or damaged: send it whole and let the model try
        log.warning("could not count pages of %s (%s); reviewing it whole", content.ref.name, exc)
        return whole
    if total <= window_pages:
        return [Window(1, total, total, content)]
    out = []
    for start in range(0, total, window_pages):
        writer = PdfWriter()
        for i in range(start, min(start + window_pages, total)):
            writer.add_page(reader.pages[i])
        buf = io.BytesIO()
        writer.write(buf)
        out.append(Window(start + 1, min(start + window_pages, total), total, replace(content, data=buf.getvalue())))
    return out
