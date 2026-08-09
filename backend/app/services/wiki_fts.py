"""SQLite FTS5 index for Wiki pages and immutable source chunks.

This module is deliberately standalone.  It only owns the FTS projection;
the caller owns the source tables, transactions, and any non-FTS fallback.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping

from sqlalchemy.engine import Connection, Engine
from sqlmodel import Session

WIKI_FTS_TABLE = "wiki_pages_fts"
SOURCE_CHUNKS_FTS_TABLE = "source_chunks_fts"
SOURCE_FTS_TABLE = SOURCE_CHUNKS_FTS_TABLE

_WIKI_COLUMNS = ("page_id", "space_id", "page_key", "status", "domain", "page_type", "source_document_id", "title", "aliases", "tags", "clauses", "body")
_SOURCE_COLUMNS = ("chunk_id", "space_id", "document_id", "chunk_index", "page_start", "page_end", "section", "parent_index", "title", "aliases", "tags", "clauses", "body")
_SEARCH_FIELDS = ("title", "aliases", "tags", "clauses", "body")
_WIKI_META = _WIKI_COLUMNS[:7]
_SOURCE_META = _SOURCE_COLUMNS[:8]
DEFAULT_FIELD_WEIGHTS = {"title": 12.0, "aliases": 9.0, "tags": 5.0, "clauses": 10.0, "body": 1.0}
_CLAUSE_RE = re.compile(r"(?<!\d)\d+(?:\.\d+){1,4}(?!\d)")
_QUERY_TOKEN_RE = re.compile(r"[0-9A-Za-z_\u4e00-\u9fff]+(?:[./-][0-9A-Za-z_\u4e00-\u9fff]+)*")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{3,}")


@dataclass(frozen=True)
class Fts5Status:
    available: bool
    reason: str = ""
    tokenizer: str | None = None

    @property
    def fallback_required(self) -> bool:
        return not self.available

    def as_dict(self) -> dict[str, Any]:
        return {"available": self.available, "fallback_required": self.fallback_required, "reason": self.reason, "tokenizer": self.tokenizer}


class Fts5UnavailableError(RuntimeError):
    """Raised by write operations when SQLite was built without FTS5."""

    def __init__(self, status: Fts5Status):
        self.status = status
        super().__init__(status.reason or "SQLite FTS5 is unavailable; caller must use its fallback")


class FtsResults(list):
    """List-compatible search result carrying an explicit fallback signal."""

    def __init__(self, values: list[dict[str, Any]] | None = None, *, status: Fts5Status, query: str = "", match_query: str = "", error: str | None = None):
        super().__init__(values or [])
        self.status = status
        self.query = query
        self.match_query = match_query
        self.error = error

    @property
    def fallback_required(self) -> bool:
        return self.status.fallback_required

    @property
    def available(self) -> bool:
        return self.status.available

    @property
    def reason(self) -> str:
        return self.status.reason

    def as_dict(self) -> dict[str, Any]:
        return {"items": list(self), "query": self.query, "match_query": self.match_query, "error": self.error, **self.status.as_dict()}


class FtsOperationResult(dict):
    """Mapping result with attribute access for convenient smoke/integration use."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def as_dict(self) -> dict[str, Any]:
        return dict(self)


def _execute(connection: Any, sql: str, params: Mapping[str, Any] | tuple[Any, ...] = ()) -> Any:
    if hasattr(connection, "exec_driver_sql"):
        return connection.exec_driver_sql(sql, params)
    return connection.execute(sql, params)


@contextmanager
def _connection_scope(target: Any) -> Iterator[Any]:
    if isinstance(target, Session):
        yield target.connection()
        return
    if isinstance(target, Engine):
        with target.begin() as connection:
            yield connection
        return
    if isinstance(target, Connection):
        yield target
        return
    if isinstance(target, sqlite3.Connection):
        try:
            yield target
            target.commit()
        except Exception:
            target.rollback()
            raise
        return
    if hasattr(target, "connect"):
        with target.begin() as connection:
            yield connection
        return
    raise TypeError("target must be a SQLModel Session, SQLAlchemy Connection/Engine, or sqlite3.Connection")


def _probe_fts5(connection: Any) -> Fts5Status:
    probe = "_casegen_fts5_probe"
    try:
        _execute(connection, f"CREATE VIRTUAL TABLE temp.{probe} USING fts5(value)")
        _execute(connection, f"DROP TABLE temp.{probe}")
    except Exception as exc:
        try:
            _execute(connection, f"DROP TABLE IF EXISTS temp.{probe}")
        except Exception:
            pass
        return Fts5Status(False, f"SQLite FTS5 unavailable: {exc}")
    tokenizer = None
    for candidate in ("trigram", "unicode61"):
        name = f"{probe}_{candidate}"
        try:
            _execute(connection, f"CREATE VIRTUAL TABLE temp.{name} USING fts5(value, tokenize='{candidate}')")
            _execute(connection, f"DROP TABLE temp.{name}")
            tokenizer = candidate
            break
        except Exception:
            try:
                _execute(connection, f"DROP TABLE IF EXISTS temp.{name}")
            except Exception:
                pass
    return Fts5Status(True, "", tokenizer or "unicode61")


def detect_fts5(target: Any) -> Fts5Status:
    """Detect FTS5 on the supplied database handle without touching app data."""
    with _connection_scope(target) as connection:
        return _probe_fts5(connection)


def is_fts5_available(target: Any) -> bool:
    return detect_fts5(target).available


def _create_table(connection: Any, table: str, columns: tuple[str, ...], meta_count: int, tokenizer: str) -> None:
    declarations = [f"{name} UNINDEXED" for name in columns[:meta_count]] + list(columns[meta_count:])
    sql = f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING fts5({', '.join(declarations)}, tokenize='{tokenizer}')"
    _execute(connection, sql)


def _table_columns(connection: Any, table: str) -> set[str]:
    try:
        result = _execute(connection, f'PRAGMA table_info("{table}")')
        return {str(row[1]) for row in result.fetchall()}
    except Exception:
        return set()


def _ensure_on_connection(connection: Any) -> Fts5Status:
    status = _probe_fts5(connection)
    if not status.available:
        return status
    try:
        tokenizer = status.tokenizer or "unicode61"
        # FTS5 virtual tables do not support adding a projected column.  If a
        # pre-space projection is present, rebuild both projections together;
        # otherwise an old table would silently ignore the isolation filter.
        existing_tables = _table_columns(connection, WIKI_FTS_TABLE), _table_columns(connection, SOURCE_CHUNKS_FTS_TABLE)
        if any(columns and "space_id" not in columns for columns in existing_tables):
            _execute(connection, f"DROP TABLE IF EXISTS {WIKI_FTS_TABLE}")
            _execute(connection, f"DROP TABLE IF EXISTS {SOURCE_CHUNKS_FTS_TABLE}")
        _create_table(connection, WIKI_FTS_TABLE, _WIKI_COLUMNS, len(_WIKI_META), tokenizer)
        _create_table(connection, SOURCE_CHUNKS_FTS_TABLE, _SOURCE_COLUMNS, len(_SOURCE_META), tokenizer)
    except Exception as exc:
        return Fts5Status(False, f"SQLite FTS5 index schema unavailable: {exc}", tokenizer)
    return status


def ensure_fts_schema(target: Any) -> Fts5Status:
    """Create both FTS5 tables idempotently and return capability status."""
    with _connection_scope(target) as connection:
        return _ensure_on_connection(connection)


def _require(connection: Any) -> Fts5Status:
    status = _ensure_on_connection(connection)
    if not status.available:
        raise Fts5UnavailableError(status)
    return status


def build_match_query(query: str | None) -> str:
    """Quote every user token so MATCH operators and punctuation stay inert."""
    if not query or not str(query).strip():
        return ""
    tokens: list[str] = []
    for raw in _QUERY_TOKEN_RE.findall(str(query))[:64]:
        candidates = [raw]
        if _CJK_RUN_RE.fullmatch(raw) and len(raw) > 4:
            # Trigram tokenization otherwise treats a natural-language Chinese
            # question as one overly strict phrase. Short windows preserve
            # recall while every token remains quoted and operator-safe.
            candidates.extend(raw[i : i + 4] for i in range(len(raw) - 3))
            candidates.extend(raw[i : i + 3] for i in range(len(raw) - 2))
        for candidate in candidates:
            token = candidate.replace('"', '""')[:256]
            if token and token not in tokens:
                tokens.append(token)
            if len(tokens) >= 64:
                break
        if len(tokens) >= 64:
            break
    return " OR ".join(f'"{token}"' for token in tokens)


safe_match_query = build_match_query


def _json_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = [value]
    if isinstance(value, Mapping):
        value = list(value.values())
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _result_column_names(result: Any) -> list[str]:
    try:
        return [str(name) for name in result.keys()]
    except Exception:
        description = getattr(result, "description", None)
        return [str(item[0]) for item in (description or [])]


def _rows_as_mappings(result: Any, rows: list[Any]) -> list[Any]:
    if not rows or hasattr(rows[0], "_mapping") or isinstance(rows[0], Mapping):
        return rows
    names = _result_column_names(result)
    return [dict(zip(names, row)) for row in rows] if names else rows


def _get(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _strip_frontmatter(value: str) -> str:
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    if not text.lstrip().startswith("---"):
        return text.strip()
    lines = text.lstrip().split("\n")
    for index, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return "\n".join(lines[index + 1 :]).strip()
    return text.strip()


def _clauses(value: Any, body: str = "") -> list[str]:
    values = _json_list(value)
    if not values:
        values = _CLAUSE_RE.findall(body)
    return list(dict.fromkeys(values))


def _page_values(item: Any, content: str | None = None, **overrides: Any) -> tuple[int, dict[str, str]]:
    row = _get(item, "row", item)
    frontmatter = _get(item, "frontmatter")
    if frontmatter is None:
        frontmatter = _get(row, "frontmatter", None)
    source = frontmatter if frontmatter is not None else item
    page_id = overrides.get("page_id", _get(row, "id", _get(item, "page_id")))
    if page_id is None:
        raise ValueError("Wiki page FTS upsert requires a persisted integer page id")
    title = overrides.get("title", _get(source, "title", _get(row, "title", "")))
    aliases = overrides.get("aliases", _get(source, "aliases", _get(row, "aliases_json", [])))
    tags = overrides.get("tags", _get(source, "tags", _get(row, "tags_json", [])))
    body = content
    if body is None:
        body = _get(item, "body", _get(item, "content", None))
    if body is None:
        path = _get(row, "path")
        if path:
            try:
                from pathlib import Path
                from app import config
                candidates = [Path(path)] if Path(path).is_absolute() else [config.WIKI_DIR / path, config.WIKI_PAGES_DIR / path]
                for candidate in candidates:
                    resolved = candidate.resolve()
                    try:
                        resolved.relative_to(config.WIKI_DIR.resolve())
                    except ValueError:
                        continue
                    if resolved.is_file():
                        body = resolved.read_text(encoding="utf-8", errors="replace")
                        break
            except (OSError, UnicodeError):
                pass
    body = _strip_frontmatter(str(body or ""))
    clause_value = overrides.get("clauses", _get(source, "clauses", None))
    if clause_value is None:
        clause_values: list[str] = []
        source_rows = _get(source, "sources", [])
        if isinstance(source_rows, str):
            try:
                source_rows = json.loads(source_rows)
            except json.JSONDecodeError:
                source_rows = []
        if not isinstance(source_rows, (list, tuple, set)):
            source_rows = [source_rows]
        for source_row in source_rows:
            clause_values.extend(_json_list(_get(source_row, "clauses", [])))
        clause_value = clause_values
    values = {
        "page_id": _text(page_id),
        "space_id": _text(overrides.get("space_id", _get(source, "space_id", _get(row, "space_id", "")))),
        "page_key": _text(overrides.get("page_key", _get(source, "page_key", _get(row, "page_key", "")))),
        "status": _text(overrides.get("status", _get(source, "status", _get(row, "status", "published")))),
        "domain": _text(overrides.get("domain", _get(source, "domain", _get(row, "domain", "")))),
        "page_type": _text(overrides.get("page_type", _get(source, "type", _get(row, "page_type", "")))),
        "source_document_id": _text(_get(row, "source_document_id", "")),
        "title": _text(title),
        "aliases": " ".join(_json_list(aliases)),
        "tags": " ".join(_json_list(tags)),
        "clauses": " ".join(_clauses(clause_value, body)),
        "body": body,
    }
    return int(page_id), values


def _source_values(item: Any, content: str | None = None, **overrides: Any) -> tuple[int, dict[str, str]]:
    chunk_id = overrides.get("chunk_id", _get(item, "id", _get(item, "chunk_id")))
    if chunk_id is None:
        raise ValueError("SourceChunk FTS upsert requires a persisted integer chunk id")
    text = content if content is not None else _get(item, "text", _get(item, "content", _get(item, "body", "")))
    text = str(text or "")
    section = str(overrides.get("section", _get(item, "section", "")) or "")
    aliases = _json_list(overrides.get("aliases", _get(item, "aliases", [])))
    if section and section not in aliases:
        aliases.append(section)
    clauses = overrides.get("clauses", _get(item, "clause_ids", _get(item, "clause_ids_json", [])))
    values = {
        "chunk_id": _text(chunk_id),
        "space_id": _text(overrides.get("space_id", _get(item, "space_id", ""))),
        "document_id": _text(overrides.get("document_id", _get(item, "document_id", ""))),
        "chunk_index": _text(_get(item, "chunk_index", "")),
        "page_start": _text(_get(item, "page_start", "")),
        "page_end": _text(_get(item, "page_end", "")),
        "section": section,
        "parent_index": _text(_get(item, "parent_index", "")),
        "title": _text(overrides.get("title", _get(item, "title", ""))),
        "aliases": " ".join(aliases),
        "tags": " ".join(_json_list(overrides.get("tags", _get(item, "tags", ["source", "原文"])))),
        "clauses": " ".join(_clauses(clauses, text)),
        "body": text,
    }
    return int(chunk_id), values


def _upsert(connection: Any, table: str, values: dict[str, str]) -> None:
    row_id = values[list(values)[0]]
    _execute(connection, f"DELETE FROM {table} WHERE rowid = :row_id", {"row_id": int(row_id)})
    columns = ", ".join(values)
    placeholders = ", ".join(f":{name}" for name in values)
    _execute(connection, f"INSERT INTO {table}(rowid, {columns}) VALUES (:row_id, {placeholders})", {"row_id": int(row_id), **values})


def upsert_wiki_page(target: Any, page: Any = None, content: str | None = None, **fields: Any) -> FtsOperationResult:
    with _connection_scope(target) as connection:
        status = _require(connection)
        page_id, values = _page_values(page or fields, content, **fields)
        _upsert(connection, WIKI_FTS_TABLE, values)
        return FtsOperationResult({"available": True, "fallback_required": False, "table": WIKI_FTS_TABLE, "id": page_id, "action": "upsert", "tokenizer": status.tokenizer})


def upsert_source_chunk(target: Any, chunk: Any = None, content: str | None = None, **fields: Any) -> FtsOperationResult:
    with _connection_scope(target) as connection:
        status = _require(connection)
        chunk_id, values = _source_values(chunk or fields, content, **fields)
        _upsert(connection, SOURCE_CHUNKS_FTS_TABLE, values)
        return FtsOperationResult({"available": True, "fallback_required": False, "table": SOURCE_CHUNKS_FTS_TABLE, "id": chunk_id, "action": "upsert", "tokenizer": status.tokenizer})


def _delete(target: Any, table: str, identifier: Any) -> FtsOperationResult:
    if hasattr(identifier, "id") and not isinstance(identifier, (str, bytes, int)):
        identifier = getattr(identifier, "id")
    if identifier is None:
        raise ValueError("FTS delete requires a persisted integer id")
    with _connection_scope(target) as connection:
        status = _require(connection)
        _execute(connection, f"DELETE FROM {table} WHERE rowid = :row_id", {"row_id": int(identifier)})
        return FtsOperationResult({"available": True, "fallback_required": False, "table": table, "id": int(identifier), "action": "delete", "tokenizer": status.tokenizer})


def delete_wiki_page(target: Any, page_id: Any) -> FtsOperationResult:
    return _delete(target, WIKI_FTS_TABLE, page_id)


def delete_source_chunk(target: Any, chunk_id: Any) -> FtsOperationResult:
    return _delete(target, SOURCE_CHUNKS_FTS_TABLE, chunk_id)


def _rows_from_target(target: Any, connection: Any, table: str, space_id: int | None = None) -> list[Any]:
    try:
        if isinstance(target, Session):
            from sqlmodel import select
            from app.models.entities import SourceChunk, WikiPageRow
            model = WikiPageRow if table == WIKI_FTS_TABLE else SourceChunk
            statement = select(model)
            if space_id is not None and hasattr(model, "space_id"):
                statement = statement.where(model.space_id == int(space_id))
            return list(target.exec(statement).all())
        source_table = "wiki_pages" if table == WIKI_FTS_TABLE else "source_chunks"
        sql = f"SELECT * FROM {source_table}"
        params: tuple[Any, ...] = ()
        if space_id is not None:
            sql += " WHERE space_id = ?"
            params = (int(space_id),)
        result = _execute(connection, sql, params)
        rows = result.fetchall()
        return list(_rows_as_mappings(result, rows))
    except Exception:
        return []


def rebuild_fts(target: Any, wiki_pages: Any = None, source_chunks: Any = None, *, clear: bool = True, space_id: int | None = None) -> FtsOperationResult:
    """Idempotently rebuild both indexes; supplied rows may be ORM or mappings."""
    with _connection_scope(target) as connection:
        status = _require(connection)
        pages = list(wiki_pages) if wiki_pages is not None else _rows_from_target(target, connection, WIKI_FTS_TABLE, space_id)
        chunks = list(source_chunks) if source_chunks is not None else _rows_from_target(target, connection, SOURCE_CHUNKS_FTS_TABLE, space_id)
        deleted_pages = deleted_chunks = 0
        if clear:
            scope_clause = " WHERE space_id = :space_id" if space_id is not None else ""
            scope_params = {"space_id": int(space_id)} if space_id is not None else {}
            page_count_result = _execute(connection, f"SELECT count(*) FROM {WIKI_FTS_TABLE}{scope_clause}", scope_params)
            chunk_count_result = _execute(connection, f"SELECT count(*) FROM {SOURCE_CHUNKS_FTS_TABLE}{scope_clause}", scope_params)
            deleted_pages = int(page_count_result.scalar_one() if hasattr(page_count_result, "scalar_one") else page_count_result.fetchone()[0])
            deleted_chunks = int(chunk_count_result.scalar_one() if hasattr(chunk_count_result, "scalar_one") else chunk_count_result.fetchone()[0])
            _execute(connection, f"DELETE FROM {WIKI_FTS_TABLE}{scope_clause}", scope_params)
            _execute(connection, f"DELETE FROM {SOURCE_CHUNKS_FTS_TABLE}{scope_clause}", scope_params)
        for page in pages:
            _upsert(connection, WIKI_FTS_TABLE, _page_values(page)[1])
        for chunk in chunks:
            _upsert(connection, SOURCE_CHUNKS_FTS_TABLE, _source_values(chunk)[1])
        return FtsOperationResult({"available": True, "fallback_required": False, "tokenizer": status.tokenizer, "wiki_pages": len(pages), "source_chunks": len(chunks), "deleted_wiki_pages": deleted_pages, "deleted_source_chunks": deleted_chunks, "action": "rebuild"})


rebuild_fts_index = rebuild_fts
rebuild_index = rebuild_fts


def index_counts(target: Any, *, space_id: int | None = None) -> dict[str, Any]:
    """Return projection row counts, with an explicit fallback signal."""
    with _connection_scope(target) as connection:
        status = _ensure_on_connection(connection)
        if not status.available:
            return {"wiki_pages": 0, "source_chunks": 0, **status.as_dict()}
        def count(table: str) -> int:
            sql = f"SELECT count(*) FROM {table}"
            params: tuple[Any, ...] = ()
            if space_id is not None:
                sql += " WHERE space_id = ?"
                params = (int(space_id),)
            result = _execute(connection, sql, params)
            if hasattr(result, "scalar_one"):
                return int(result.scalar_one())
            return int(result.fetchone()[0])

        wiki_count = count(WIKI_FTS_TABLE)
        source_count = count(SOURCE_CHUNKS_FTS_TABLE)
        return {
            "wiki_pages": wiki_count,
            "source_chunks": source_count,
            **status.as_dict(),
        }


def _weights(value: Mapping[str, Any] | None) -> dict[str, float]:
    result = dict(DEFAULT_FIELD_WEIGHTS)
    for name, weight in (value or {}).items():
        if name not in _SEARCH_FIELDS:
            continue
        number = float(weight)
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"invalid FTS field weight for {name}")
        result[name] = number
    return result


def _bm25(table: str, meta_count: int, weights: Mapping[str, float]) -> str:
    values = ["0"] * meta_count + [format(float(weights[name]), ".12g") for name in _SEARCH_FIELDS]
    return f"bm25({table}, {', '.join(values)})"


def _row_value(row: Any, name: str) -> Any:
    try:
        return row._mapping[name]
    except AttributeError:
        return row[name]


def _search(target: Any, table: str, query: str, *, limit: int, offset: int, weights: Mapping[str, Any] | None, filters: Mapping[str, Any], include_archived: bool) -> FtsResults:
    with _connection_scope(target) as connection:
        status = _ensure_on_connection(connection)
        match_query = build_match_query(query)
        if not status.available or not match_query or limit <= 0:
            return FtsResults(status=status, query=query or "", match_query=match_query)
        meta_count = len(_WIKI_META if table == WIKI_FTS_TABLE else _SOURCE_META)
        columns = _WIKI_COLUMNS if table == WIKI_FTS_TABLE else _SOURCE_COLUMNS
        searchable_start = meta_count
        body_index = columns.index("body")
        weight_map = _weights(weights)
        bm25_sql = _bm25(table, meta_count, weight_map)
        where = [f"{table} MATCH :match_query"]
        params: dict[str, Any] = {"match_query": match_query, "open_tag": "<mark>", "close_tag": "</mark>", "ellipsis": "…", "snippet_tokens": 48, "limit": min(int(limit), 100), "offset": max(int(offset), 0)}
        for name, value in filters.items():
            if value is not None and name in columns and name in {"space_id", "status", "domain", "page_type", "source_document_id", "document_id"}:
                where.append(f"{table}.{name} = :filter_{name}")
                params[f"filter_{name}"] = str(value)
        if table == WIKI_FTS_TABLE and not include_archived and "status" not in filters:
            where.append(f"{table}.status != 'archived'")
        selected = ", ".join(f"{table}.{name} AS {name}" for name in columns)
        highlighted = ", ".join(f"highlight({table}, {searchable_start + index}, :open_tag, :close_tag) AS h_{name}" for index, name in enumerate(_SEARCH_FIELDS))
        sql = f"SELECT rowid AS _rowid, {selected}, {bm25_sql} AS _bm25, snippet({table}, {body_index}, :open_tag, :close_tag, :ellipsis, :snippet_tokens) AS _snippet, {highlighted} FROM {table} WHERE {' AND '.join(where)} ORDER BY _bm25 ASC, _rowid ASC LIMIT :limit OFFSET :offset"
        try:
            result = _execute(connection, sql, params)
            rows = result.fetchall()
            rows = _rows_as_mappings(result, rows)
        except Exception as exc:
            return FtsResults(status=status, query=query or "", match_query=match_query, error=f"FTS5 MATCH query failed: {exc}")
        values: list[dict[str, Any]] = []
        for row in rows:
            raw_bm25 = float(_row_value(row, "_bm25"))
            highlights = {name: str(_row_value(row, f"h_{name}") or "") for name in _SEARCH_FIELDS}
            marked = [name for name, text in highlights.items() if "<mark>" in text]
            snippet = str(_row_value(row, "_snippet") or "")
            if "<mark>" not in snippet:
                snippet = next((highlights[name] for name in marked), snippet)
            item = {name: _row_value(row, name) for name in columns}
            item.update({"id": int(_row_value(row, "_rowid")), "kind": "wiki_page" if table == WIKI_FTS_TABLE else "source_chunk", "score": max(0.0, -raw_bm25), "bm25": raw_bm25, "snippet": snippet, "highlights": highlights, "explain": {"algorithm": "bm25", "raw_bm25": raw_bm25, "field_weights": weight_map, "matched_fields": marked, "match_query": match_query}})
            if str(item.get("space_id") or "").isdigit():
                item["space_id"] = int(item["space_id"])
            if table == WIKI_FTS_TABLE:
                item["page_id"] = int(item["page_id"])
                item["aliases"] = str(item["aliases"] or "").split()
                item["tags"] = str(item["tags"] or "").split()
                item["clauses"] = str(item["clauses"] or "").split()
                item["content"] = item["body"]
            else:
                item["chunk_id"] = int(item["chunk_id"])
                item["document_id"] = int(item["document_id"]) if str(item["document_id"]).isdigit() else item["document_id"]
                item["clauses"] = str(item["clauses"] or "").split()
                item["text"] = item["body"]
                item["content"] = item["body"]
            values.append(item)
        return FtsResults(values, status=status, query=query or "", match_query=match_query)


def _compat_space_id(target: Any, space_id: int | None) -> int | None:
    if space_id is not None:
        return int(space_id)
    # Legacy service/test callers receive the explicit default namespace.  A
    # Session is the only target where this can be resolved without opening a
    # second database handle; production retrieval always passes the id.
    try:
        from sqlmodel import Session as SqlModelSession
        from app.services.wiki_spaces import resolve_space_id

        if isinstance(target, SqlModelSession):
            return resolve_space_id(target)
    except Exception:
        pass
    return None


def search_wiki(target: Any, query: str, *, limit: int = 10, offset: int = 0, field_weights: Mapping[str, Any] | None = None, space_id: int | None = None, status: str | None = None, domain: str | None = None, page_type: str | None = None, include_archived: bool = False) -> FtsResults:
    space_id = _compat_space_id(target, space_id)
    return _search(target, WIKI_FTS_TABLE, query, limit=limit, offset=offset, weights=field_weights, filters={"space_id": space_id, "status": status, "domain": domain, "page_type": page_type}, include_archived=include_archived)


def search_source_chunks(target: Any, query: str, *, limit: int = 10, offset: int = 0, field_weights: Mapping[str, Any] | None = None, space_id: int | None = None, document_id: int | None = None) -> FtsResults:
    space_id = _compat_space_id(target, space_id)
    return _search(target, SOURCE_CHUNKS_FTS_TABLE, query, limit=limit, offset=offset, weights=field_weights, filters={"space_id": space_id, "document_id": document_id}, include_archived=True)


def search(target: Any, query: str, *, limit: int = 10, scope: str = "all", field_weights: Mapping[str, Any] | None = None, space_id: int | None = None) -> FtsResults:
    if scope == "wiki":
        return search_wiki(target, query, limit=limit, field_weights=field_weights, space_id=space_id)
    if scope in {"source", "source_chunks"}:
        return search_source_chunks(target, query, limit=limit, field_weights=field_weights, space_id=space_id)
    wiki = search_wiki(target, query, limit=limit, field_weights=field_weights, space_id=space_id)
    source = search_source_chunks(target, query, limit=limit, field_weights=field_weights, space_id=space_id)
    status = wiki.status if not wiki.status.available else source.status
    values = sorted([*wiki, *source], key=lambda item: (-float(item.get("score", 0.0)), int(item.get("id", 0))))[: max(0, int(limit))]
    return FtsResults(values, status=status, query=query or "", match_query=build_match_query(query), error=wiki.error or source.error)


class WikiFtsIndex:
    """Object facade for callers that prefer a reusable index handle."""

    def __init__(self, target: Any):
        self.target = target

    def status(self) -> Fts5Status:
        return detect_fts5(self.target)

    def ensure(self) -> Fts5Status:
        return ensure_fts_schema(self.target)

    def rebuild(self, wiki_pages: Any = None, source_chunks: Any = None, *, clear: bool = True, space_id: int | None = None) -> FtsOperationResult:
        return rebuild_fts(self.target, wiki_pages, source_chunks, clear=clear, space_id=space_id)

    def upsert_wiki_page(self, page: Any = None, content: str | None = None, **fields: Any) -> FtsOperationResult:
        return upsert_wiki_page(self.target, page, content, **fields)

    def upsert_source_chunk(self, chunk: Any = None, content: str | None = None, **fields: Any) -> FtsOperationResult:
        return upsert_source_chunk(self.target, chunk, content, **fields)

    def delete_wiki_page(self, page_id: Any) -> FtsOperationResult:
        return delete_wiki_page(self.target, page_id)

    def delete_source_chunk(self, chunk_id: Any) -> FtsOperationResult:
        return delete_source_chunk(self.target, chunk_id)

    def search_wiki(self, query: str, **kwargs: Any) -> FtsResults:
        return search_wiki(self.target, query, **kwargs)

    def search_source_chunks(self, query: str, **kwargs: Any) -> FtsResults:
        return search_source_chunks(self.target, query, **kwargs)

    def search(self, query: str, **kwargs: Any) -> FtsResults:
        return search(self.target, query, **kwargs)


FTS5Index = WikiFtsIndex
WikiFTS = WikiFtsIndex
