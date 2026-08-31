from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


class _HtmlParagraphParser(HTMLParser):
    """Small dependency-free HTML extractor that preserves heading context."""

    _block_tags = {"p", "li", "blockquote", "figcaption", "td", "th"}
    _heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    _ignored_tags = {"script", "style", "noscript", "svg", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._current: list[str] = []
        self._paragraphs: list[tuple[str | None, str]] = []
        self._section: str | None = None
        self._tag: str | None = None
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in self._ignored_tags:
            self._ignored_depth += 1
        if self._ignored_depth:
            return
        if tag in self._block_tags | self._heading_tags:
            self._flush()
            self._tag = tag

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in self._ignored_tags:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth:
            return
        if tag in self._block_tags | self._heading_tags:
            self._flush()
            self._tag = None

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self._current.append(data)

    def _flush(self) -> None:
        text = " ".join("".join(self._current).split())
        self._current.clear()
        if not text:
            return
        if self._tag in self._heading_tags:
            self._section = text
        else:
            self._paragraphs.append((self._section, text))

    def paragraphs(self) -> list[tuple[str | None, str]]:
        self._flush()
        return self._paragraphs


def _sha256(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _passage(document_id: str, text: str, locator: dict[str, Any]) -> dict[str, Any]:
    passage_hash = _sha256(text)
    return {
        "passage_id": _sha256(f"{document_id}:{locator}:{passage_hash}")[:16],
        "document_id": document_id,
        "text": text,
        "locator": locator,
        "passage_sha256": passage_hash,
        "support_status": "candidate",
        "verification_scope": "full_text_extracted_unverified",
    }


def _text_paragraphs(text: str) -> list[tuple[str | None, str]]:
    blocks = [" ".join(block.split()) for block in re.split(r"\n\s*\n+", text) if block.strip()]
    return [(None, block) for block in blocks]


def _pdf_paragraphs(path: Path) -> tuple[list[tuple[int, str]], str | None]:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError:
        return [], "pypdf_not_installed"
    try:
        reader = PdfReader(str(path))
        pages = []
        for index, page in enumerate(reader.pages, start=1):
            text = " ".join((page.extract_text() or "").split())
            if text:
                pages.append((index, text))
        return pages, None
    except Exception as exc:
        return [], f"pdf_extract_failed:{exc}"


def extract_local_full_text(path_value: str | Path, max_passages: int = 500) -> dict[str, Any]:
    """Return content-hashed, locatable passages from an explicitly supplied file.

    The caller is responsible for lawful access. This function never fetches a URL,
    follows browser sessions, or changes an input file.
    """
    source_path = Path(path_value).expanduser().resolve()
    base = {
        "source_path": str(source_path),
        "access_mode": "user_provided_local",
        "license_status": "not_asserted",
        "retrieval_status": "not_fetched_by_autoresearch",
    }
    if max_passages < 1:
        raise ValueError("max_passages must be positive")
    if not source_path.is_file():
        return {**base, "status": "missing_file", "document_id": None, "content_sha256": None, "format": None, "passages": []}
    raw = source_path.read_bytes()
    content_hash = _sha256(raw)
    document_id = _sha256(f"{source_path.name}:{content_hash}")[:16]
    suffix = source_path.suffix.casefold()
    document = {**base, "status": "extracted", "document_id": document_id, "content_sha256": content_hash, "format": suffix.lstrip(".") or "unknown"}
    if suffix in {".txt", ".md"}:
        paragraphs = _text_paragraphs(raw.decode("utf-8", errors="replace"))
        passages = [_passage(document_id, text, {"type": "text_paragraph", "paragraph_index": index, "section": section}) for index, (section, text) in enumerate(paragraphs[:max_passages])]
    elif suffix in {".html", ".htm"}:
        parser = _HtmlParagraphParser()
        parser.feed(raw.decode("utf-8", errors="replace"))
        parser.close()
        paragraphs = parser.paragraphs()
        passages = [_passage(document_id, text, {"type": "html_paragraph", "paragraph_index": index, "section": section}) for index, (section, text) in enumerate(paragraphs[:max_passages])]
    elif suffix == ".pdf":
        pages, error = _pdf_paragraphs(source_path)
        if error:
            return {**document, "status": "parser_unavailable" if error == "pypdf_not_installed" else "extract_failed", "error": error, "passages": []}
        passages = [_passage(document_id, text, {"type": "pdf_page", "page": page_number}) for page_number, text in pages[:max_passages]]
    else:
        return {**document, "status": "unsupported_format", "error": "supported formats are .txt, .md, .html, .htm and .pdf", "passages": []}
    return {**document, "status": "extracted" if passages else "empty_text", "passages": passages}
