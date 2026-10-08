"""External knowledge HTTP client adhering to docs/llm-wiki-reference/API_README.md."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class ExternalWikiHit:
    chunk_id: int | str = 0
    document_path: str = ""
    heading_path: str = ""
    title: str = ""
    score: float = 0.0
    snippet: str = ""
    evidence_snippet: str = ""
    highlight_terms: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def list_external_projects(
    base_url: str,
    timeout_sec: float = 3.0,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Fetch registered projects from external LLM Wiki service.

    Docs specification:
      GET /api/projects/
      Returns: {"projects": [{"id": "...", "name": "...", ...}]}
    """
    clean_base = (base_url or "").rstrip("/")
    if not clean_base:
        return []
    url = f"{clean_base}/api/projects/"
    try:
        with httpx.Client(timeout=max(0.1, float(timeout_sec)), transport=transport) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                logger.warning("External wiki list_projects failed: HTTP %s %s", resp.status_code, resp.text[:200])
                return []
            data = resp.json()
            if isinstance(data, dict):
                return data.get("projects") or []
            if isinstance(data, list):
                return data
            return []
    except Exception as exc:
        logger.warning("External wiki list_projects request error (%s): %s", url, exc)
        return []


def search_external_knowledge(
    base_url: str,
    external_project_id: str,
    query: str,
    limit: int = 6,
    use_synonyms: bool = True,
    timeout_sec: float = 3.0,
    transport: httpx.BaseTransport | None = None,
) -> list[ExternalWikiHit]:
    """Search external LLM Wiki for relevant knowledge chunks.

    Docs specification:
      POST /api/projects/{external_project_id}/search/
      Request body: {"query": query, "limit": limit, "useSynonyms": use_synonyms}
      Returns: {"results": [...]}
    """
    clean_base = (base_url or "").rstrip("/")
    project_id = str(external_project_id or "").strip()
    if not clean_base or not project_id or not query.strip():
        return []
    url = f"{clean_base}/api/projects/{project_id}/search/"
    payload = {
        "query": query,
        "limit": max(1, int(limit)),
        "useSynonyms": bool(use_synonyms),
    }
    try:
        with httpx.Client(timeout=max(0.1, float(timeout_sec)), transport=transport) as client:
            resp = client.post(url, json=payload)
            if resp.status_code != 200:
                logger.warning(
                    "External wiki search failed (%s): HTTP %s %s",
                    url,
                    resp.status_code,
                    resp.text[:200],
                )
                return []
            data = resp.json()
            raw_results = data.get("results") if isinstance(data, dict) else []
            if not isinstance(raw_results, list):
                return []
            hits: list[ExternalWikiHit] = []
            for item in raw_results:
                if not isinstance(item, dict):
                    continue
                chunk_id = item.get("chunkId") if item.get("chunkId") is not None else item.get("chunk_id", 0)
                doc_path = item.get("documentPath") or item.get("document_path") or item.get("path") or ""
                heading_path = item.get("headingPath") or item.get("heading_path") or ""
                title = item.get("title") or heading_path or doc_path or "外部知识切片"
                try:
                    score = float(item.get("score") or 0.0)
                except (TypeError, ValueError):
                    score = 0.0
                snippet = item.get("snippet") or ""
                evidence_snippet = item.get("evidenceSnippet") or item.get("evidence_snippet") or ""
                raw_highlights = item.get("highlightTerms") or item.get("highlight_terms") or []
                highlight_terms = [str(t) for t in raw_highlights] if isinstance(raw_highlights, list) else []

                hits.append(
                    ExternalWikiHit(
                        chunk_id=chunk_id,
                        document_path=doc_path,
                        heading_path=heading_path,
                        title=title,
                        score=score,
                        snippet=snippet,
                        evidence_snippet=evidence_snippet,
                        highlight_terms=highlight_terms,
                        raw=item,
                    )
                )
            return hits
    except Exception as exc:
        logger.warning("External wiki search error (%s): %s", url, exc)
        return []
