from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote
from urllib.request import Request, urlopen
from xml.etree import ElementTree


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().lower()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)
    value = re.sub(r"^doi:\s*", "", value)
    return value or None


def normalize_arxiv(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().lower()
    value = re.sub(r"^https?://arxiv\.org/(?:abs|pdf)/", "", value)
    value = re.sub(r"v\d+$", "", value)
    return value or None


@dataclass(frozen=True)
class PaperRecord:
    title: str
    authors: list[str]
    year: int | None
    doi: str | None = None
    arxiv_id: str | None = None
    url: str | None = None
    venue: str | None = None
    abstract: str | None = None
    source: str = "unknown"
    citation_status: str = "unverified"

    def canonical_key(self) -> str:
        if self.doi:
            return f"doi:{normalize_doi(self.doi)}"
        if self.arxiv_id:
            return f"arxiv:{normalize_arxiv(self.arxiv_id)}"
        normalized_title = re.sub(r"\W+", "", self.title.casefold())
        return f"title:{normalized_title}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["doi"] = normalize_doi(self.doi)
        data["arxiv_id"] = normalize_arxiv(self.arxiv_id)
        return data


def candidate_passages(record: PaperRecord, query: str, max_passages: int = 3) -> list[dict[str, Any]]:
    """Extract conservative, hashable candidate passages from an abstract.

    These are retrieval candidates, not verified support for a claim. Full-text
    adapters can emit the same schema with a page/section locator later.
    """
    if not record.abstract or max_passages <= 0:
        return []
    text = " ".join(record.abstract.split())
    terms = {term for term in re.findall(r"[a-z0-9]{3,}", query.casefold())}
    sentences = [sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+", text) if sentence.strip()]
    ranked = sorted(
        enumerate(sentences),
        key=lambda item: (-len(terms.intersection(re.findall(r"[a-z0-9]{3,}", item[1].casefold()))), item[0]),
    )
    selected = ranked[:max_passages]
    passages = []
    for index, sentence in sorted(selected, key=lambda item: item[0]):
        passages.append({
            "passage_id": hashlib.sha256(f"{record.canonical_key()}:{index}:{sentence}".encode("utf-8")).hexdigest()[:16],
            "paper_key": record.canonical_key(),
            "text": sentence,
            "locator": {"type": "abstract", "sentence_index": index},
            "source": record.source,
            "source_url": record.url,
            "passage_sha256": hashlib.sha256(sentence.encode("utf-8")).hexdigest(),
            "support_status": "candidate",
            "verification_scope": "abstract_text_only",
        })
    return passages


@dataclass(frozen=True)
class SourceSearch:
    source: str
    request_url: str
    records: list[PaperRecord]
    retrieved_at: str
    raw_response: str
    status: str = "success"
    error: str | None = None
    intelligence: dict[str, Any] | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "request_url": self.request_url,
            "retrieved_at": self.retrieved_at,
            "status": self.status,
            "error": self.error,
            "raw_sha256": hashlib.sha256(self.raw_response.encode("utf-8")).hexdigest(),
            "raw_response": self.raw_response,
            "literature_intelligence": self.intelligence,
        }


class LiteratureSource(Protocol):
    name: str

    async def search(self, query: str, limit: int) -> SourceSearch: ...


class DeepResearchSource:
    """Run an external DeepResearch-compatible retriever.

    The process receives ``{"query": ..., "limit": ...}`` on stdin and must
    return ``{"records": [...]}`` (``papers`` is accepted as an alias). The
    strict record contract keeps an arbitrary narrative report from entering
    the evidence graph without provenance.
    """

    name = "deepresearch"

    def __init__(self, command: list[str], cwd: str | os.PathLike[str] = ".", timeout_seconds: int = 900) -> None:
        if not command or any(not isinstance(item, str) or not item for item in command):
            raise ValueError("DeepResearch command must be a non-empty string array")
        self.command = tuple(command)
        self.cwd = Path(cwd).resolve()
        if not self.cwd.is_dir():
            raise ValueError(f"DeepResearch cwd does not exist: {self.cwd}")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds

    async def search(self, query: str, limit: int) -> SourceSearch:
        request = {"query": query, "limit": limit}
        process = None
        raw = ""
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command, cwd=str(self.cwd), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate((json.dumps(request, ensure_ascii=True) + "\n").encode("utf-8")),
                timeout=self.timeout_seconds,
            )
            raw = stdout.decode("utf-8", errors="replace")
            if process.returncode != 0:
                return SourceSearch(self.name, "command://deepresearch", [], utc_now(), raw, status="failed", error=stderr.decode("utf-8", errors="replace"))
            data = json.loads(raw)
            values = data.get("records", data.get("papers")) if isinstance(data, dict) else None
            if not isinstance(values, list):
                raise ValueError("response must contain a records or papers array")
            records: list[PaperRecord] = []
            for value in values[:limit]:
                if not isinstance(value, dict) or not isinstance(value.get("title"), str) or not value["title"].strip():
                    raise ValueError("each record requires a non-empty title")
                authors = value.get("authors", [])
                if not isinstance(authors, list) or not all(isinstance(author, str) for author in authors):
                    raise ValueError("record authors must be a string array")
                year = value.get("year")
                if year is not None and (not isinstance(year, int) or isinstance(year, bool)):
                    raise ValueError("record year must be an integer or null")
                records.append(PaperRecord(
                    title=value["title"].strip(), authors=authors, year=year,
                    doi=normalize_doi(value.get("doi")), arxiv_id=normalize_arxiv(value.get("arxiv_id")),
                    url=value.get("url"), venue=value.get("venue"), abstract=value.get("abstract"),
                    source=self.name, citation_status=value.get("citation_status", "unverified"),
                ))
            return SourceSearch(self.name, "command://deepresearch", records, utc_now(), raw)
        except asyncio.TimeoutError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            return SourceSearch(self.name, "command://deepresearch", [], utc_now(), raw, status="failed", error="timeout")
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            return SourceSearch(self.name, "command://deepresearch", [], utc_now(), raw, status="failed", error=str(exc))


class DeerFlowSource:
    """Adapt DeerFlow's ``--json`` newline-delimited StreamEvents.

    DeerFlow's headless output is a stream of ``{"type": ..., "data": ...}``
    events, and its report convention uses ``[citation:Title](URL)`` links.
    This adapter extracts only those explicit links, retaining the complete
    event stream as the source snapshot. A narrative response without explicit
    citations deliberately yields no EvidenceSet records.
    """

    name = "deerflow"
    _citation_pattern = re.compile(r"\[citation:([^\]]+)\]\((https?://[^)\s]+)\)")
    _url_pattern = re.compile(r"https?://(?:doi\.org/|arxiv\.org/)[^\s)]+")
    # JSON contains many nested objects; use a greedy match bounded by the
    # closing tag rather than stopping at the first inner `}`.
    _intelligence_pattern = re.compile(r"<literature_intelligence>\s*(\{.*\})\s*</literature_intelligence>", re.DOTALL)

    def __init__(self, command: list[str] | None = None, cwd: str | os.PathLike[str] = ".", timeout_seconds: int = 900) -> None:
        if command is None:
            local_cli = Path(cwd).resolve() / "backend" / ".venv" / "Scripts" / "deerflow.exe"
            # `--print` is robust on Windows consoles and returns a bounded
            # final answer; `--json` may trigger interactive tool streams that
            # hang or fail GBK encoding in headless subprocesses.
            command = [str(local_cli), "--recursion-limit", "30", "--print"] if local_cli.exists() else ["deerflow", "--recursion-limit", "30", "--print"]
        if not command or any(not isinstance(item, str) or not item for item in command):
            raise ValueError("DeerFlow command must be a non-empty string array")
        self.command = tuple(command)
        self.cwd = Path(cwd).resolve()
        if not self.cwd.is_dir():
            raise ValueError(f"DeerFlow cwd does not exist: {self.cwd}")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds

    @classmethod
    def _strings(cls, value: Any):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from cls._strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from cls._strings(item)

    @staticmethod
    def _intelligence_prompt(query: str, limit: int) -> str:
        """Ask DeerFlow for a bounded, provenance-labelled research brief."""
        return f'''Research question: {query}

Do not call tools, read files, browse local paths, or enter interactive mode. Use only your existing model knowledge and provide verifiable DOI/URL citations; stop after the final tagged JSON.

Act as a literature-intelligence assistant. Search and compare at most {limit} directly relevant papers. After shortlisting, open the full text only for the most relevant 3 papers when a lawful open-access, arXiv, publisher, or already-authorized page is available. Do not bulk-download files. Do not claim novelty is established. For every statement derived only from an abstract or metadata, label its source_level accordingly. Do not invent DOI, URL, result, page, section, or full-text evidence.

Return a single JSON object enclosed exactly in <literature_intelligence> and </literature_intelligence> with this shape:
{{
  "paper_cards": [{{"title":"...","authors":["..."],"year":2024,"url":"https://...","source_level":"full_text|abstract_only|metadata_only","research_question":"...","method":"...","key_results":["..."],"limitations":["..."],"relevance":"...","evidence_notes":["..."],"full_text_evidence":[{{"text":"verbatim short passage","locator":{{"type":"section|page","value":"Methods / p. 4"}}}}]}}],
  "comparison_matrix": [{{"dimension":"method|data|evaluation|limitation","comparison":"...","paper_titles":["..."]}}],
  "gap_candidates": [{{"statement":"...","related_paper_titles":["..."],"evidence_level":"candidate_only","why_not_established":"...","falsification_search":"..."}}],
  "research_plan": {{"recommended_question":"...","baseline":"...","candidate":"...","metric":"...","failure_condition":"...","resource_budget":"..."}}
}}
Only output the tagged JSON object in the final answer.'''

    @classmethod
    def _parse_intelligence(cls, texts: list[str]) -> dict[str, Any] | None:
        """Validate DeerFlow's optional structured final answer conservatively."""
        for text in reversed(texts):
            for match in cls._intelligence_pattern.finditer(text):
                try:
                    value = json.loads(match.group(1))
                except json.JSONDecodeError:
                    continue
                if not isinstance(value, dict) or not isinstance(value.get("paper_cards"), list):
                    continue
                cards = []
                for item in value["paper_cards"]:
                    if not isinstance(item, dict) or not isinstance(item.get("title"), str) or not item["title"].strip():
                        continue
                    card = {key: item.get(key) for key in ("title", "authors", "year", "url", "source_level", "research_question", "method", "key_results", "limitations", "relevance", "evidence_notes", "full_text_evidence")}
                    card["title"] = card["title"].strip()
                    card["source_level"] = card["source_level"] if card["source_level"] in {"full_text", "abstract_only", "metadata_only"} else "metadata_only"
                    if not isinstance(card["full_text_evidence"], list):
                        card["full_text_evidence"] = []
                    card["full_text_evidence"] = [entry for entry in card["full_text_evidence"] if isinstance(entry, dict) and isinstance(entry.get("text"), str) and entry["text"].strip() and isinstance(entry.get("locator"), dict)]
                    cards.append(card)
                # DeerFlow may return a structured research plan before it has
                # citation cards (e.g. when web tools are unavailable). Keep
                # the plan as provenance instead of discarding the response;
                # the workflow can proceed with an explicit evidence warning.
                gaps = value.get("gap_candidates", [])
                if not isinstance(gaps, list):
                    gaps = []
                normalized_gaps = []
                for item in gaps:
                    if isinstance(item, dict) and isinstance(item.get("statement"), str) and item["statement"].strip():
                        normalized_gaps.append({
                            "statement": item["statement"].strip(),
                            "related_paper_titles": item.get("related_paper_titles", []),
                            "evidence_level": "candidate_only",
                            "why_not_established": item.get("why_not_established", "Requires human novelty review."),
                            "falsification_search": item.get("falsification_search", "Not supplied."),
                        })
                matrix = value.get("comparison_matrix", [])
                return {
                    "provider": "deerflow",
                    "paper_cards": cards,
                    "comparison_matrix": matrix if isinstance(matrix, list) else [],
                    "gap_candidates": normalized_gaps,
                    "research_plan": value.get("research_plan", {}) if isinstance(value.get("research_plan"), dict) else {},
                    "limitations": ["DeerFlow synthesis is a candidate research brief; paper-level claims require source-grounded human review."],
                }
        return None

    async def search(self, query: str, limit: int) -> SourceSearch:
        command = [*self.command, self._intelligence_prompt(query, limit)]
        process = None
        raw = ""
        try:
            process = await asyncio.create_subprocess_exec(
                *command, cwd=str(self.cwd), env={**os.environ, "PYTHONIOENCODING": "utf-8:replace", "PYTHONUTF8": "1", "PYTHONLEGACYWINDOWSSTDIO": "0"},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout_seconds)
            raw = stdout.decode("utf-8", errors="replace")
            if process.returncode != 0:
                return SourceSearch(self.name, "command://deerflow", [], utc_now(), raw, status="failed", error=stderr.decode("utf-8", errors="replace"))
            citations: list[tuple[str, str]] = []
            texts: list[str] = []
            # Parse both DeerFlow JSONL events and plain `--print` output.
            citations.extend(self._citation_pattern.findall(raw))
            for line in raw.splitlines():
                urls = self._url_pattern.findall(line)
                if urls:
                    title = re.sub(r"^\s*\d+[.)]\s*", "", line).split(".", 1)[0].strip()
                    for url in urls:
                        citations.append((title or url, url.rstrip(".,")))
            texts.append(raw)
            for line in raw.splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for text in self._strings(event):
                    texts.append(text)
                    citations.extend(self._citation_pattern.findall(text))
            unique: dict[str, tuple[str, str]] = {}
            for title, url in citations:
                unique.setdefault(url, (title.strip(), url))
            intelligence = self._parse_intelligence(texts)
            records = [PaperRecord(title, [], None, url=url, source=self.name, citation_status="unverified") for title, url in list(unique.values())[:limit]]
            if not records and intelligence:
                records = [PaperRecord(card["title"], card.get("authors") if isinstance(card.get("authors"), list) else [], card.get("year") if isinstance(card.get("year"), int) else None, url=card.get("url") if isinstance(card.get("url"), str) else None, source=self.name, citation_status="unverified") for card in intelligence["paper_cards"][:limit]]
            status = "success" if records or intelligence else "success"
            return SourceSearch(self.name, "command://deerflow", records, utc_now(), raw, status=status, intelligence=intelligence)
        except asyncio.TimeoutError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            return SourceSearch(self.name, "command://deerflow", [], utc_now(), raw, status="failed", error="timeout")
        except (OSError, UnicodeError) as exc:
            return SourceSearch(self.name, "command://deerflow", [], utc_now(), raw, status="failed", error=str(exc))


def deduplicate(records: list[PaperRecord]) -> list[PaperRecord]:
    """Prefer verified records and retain first appearance for stable output."""
    unique: dict[str, PaperRecord] = {}
    for record in records:
        key = record.canonical_key()
        previous = unique.get(key)
        rank = {"unverified": 0, "metadata_only": 1, "metadata_verified": 2}
        if previous is None or rank.get(record.citation_status, 0) > rank.get(previous.citation_status, 0):
            unique[key] = record
    return list(unique.values())


def _fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "AutoResearch/0.1 (research orchestration prototype)"})
    with urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8", errors="replace")


class CrossrefSource:
    name = "crossref"

    async def search(self, query: str, limit: int) -> SourceSearch:
        url = "https://api.crossref.org/works?query.bibliographic=" + quote(query) + f"&rows={limit}"
        try:
            raw = await asyncio.to_thread(_fetch_text, url)
            items = json.loads(raw).get("message", {}).get("items", [])
            records = []
            for item in items:
                dates = item.get("published-print") or item.get("published-online") or item.get("issued") or {}
                parts = dates.get("date-parts", [[]])
                year = parts[0][0] if parts and parts[0] else None
                records.append(PaperRecord(
                    title=(item.get("title") or ["Untitled"])[0],
                    authors=[" ".join(filter(None, [author.get("given"), author.get("family")])) for author in item.get("author", [])],
                    year=year,
                    doi=normalize_doi(item.get("DOI")),
                    url=item.get("URL"),
                    venue=(item.get("container-title") or [None])[0],
                    source=self.name,
                    citation_status="metadata_verified" if item.get("DOI") else "unverified",
                ))
            return SourceSearch(self.name, url, records, utc_now(), raw)
        except Exception as exc:
            return SourceSearch(self.name, url, [], utc_now(), "", status="failed", error=str(exc))


class ArxivSource:
    name = "arxiv"

    async def search(self, query: str, limit: int) -> SourceSearch:
        url = "https://export.arxiv.org/api/query?search_query=all%3A" + quote(query) + f"&start=0&max_results={limit}"
        try:
            raw = await asyncio.to_thread(_fetch_text, url)
            root = ElementTree.fromstring(raw)
            atom = "{http://www.w3.org/2005/Atom}"
            records = []
            for entry in root.findall(f"{atom}entry"):
                identifier = (entry.findtext(f"{atom}id") or "").rstrip("/").split("/")[-1]
                published = entry.findtext(f"{atom}published") or ""
                records.append(PaperRecord(
                    title=" ".join((entry.findtext(f"{atom}title") or "Untitled").split()),
                    authors=[author.findtext(f"{atom}name") or "Unknown" for author in entry.findall(f"{atom}author")],
                    year=int(published[:4]) if published[:4].isdigit() else None,
                    arxiv_id=normalize_arxiv(identifier),
                    url=entry.findtext(f"{atom}id"),
                    abstract=" ".join((entry.findtext(f"{atom}summary") or "").split()),
                    source=self.name,
                    citation_status="metadata_only",
                ))
            return SourceSearch(self.name, url, records, utc_now(), raw)
        except Exception as exc:
            return SourceSearch(self.name, url, [], utc_now(), "", status="failed", error=str(exc))


class FixtureLiteratureSource:
    """Deterministic source for tests and offline demonstrations."""

    name = "fixture"

    def __init__(self, records: list[PaperRecord] | None = None) -> None:
        self.records = records if records is not None else [
            PaperRecord("Reproducible baseline study", ["A. Researcher"], 2024, doi="10.0000/example", source=self.name, citation_status="metadata_verified")
        ]

    async def search(self, query: str, limit: int) -> SourceSearch:
        response = json.dumps({"query": query, "records": [record.to_dict() for record in self.records[:limit]]}, sort_keys=True)
        return SourceSearch(self.name, "fixture://literature", self.records[:limit], utc_now(), response)
