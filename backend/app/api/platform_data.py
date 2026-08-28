from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, col, select

from app.db import get_session
from app.models.entities import (
    DataPool,
    DataPoolRevision,
    ExampleVariant,
    GenerationTask,
    ManagedPlatformCase,
    ModelConfig,
    PlatformArtifact,
    PlatformExample,
    PlatformProfile,
    PlatformRenderRun,
    PromptTemplate,
    TestCase,
    WikiSpace,
)
from app.schemas.platform_data import (
    ArtifactOut,
    DataPoolCreate,
    DataPoolOut,
    DataPoolRevisionOut,
    ExampleCreate,
    ExampleOut,
    ExampleUpdate,
    PlatformCreate,
    PlatformCaseCreate,
    PlatformCaseDetail,
    PlatformCasePage,
    PlatformCaseSummary,
    PlatformCaseUpdate,
    PlatformOut,
    PlatformUpdate,
    PlatformRenderRequest,
    RenderRunOut,
    SemanticCaseOut,
    VariantCreate,
    VariantOut,
    VariantUpdate,
)
from app.services.llm import LLMError, chat_completion

router = APIRouter(tags=["platform-data"])

# Deterministic test hooks. They also make the exact LLM context inspectable.
_PLATFORM_CHAT_FN: Callable[..., Any] | None = None
_DESCRIPTION_CHAT_FN: Callable[..., Any] | None = None

ARTIFACT_KINDS = {"case", "data", "relation", "combined", "attachment"}
DATA_PREVIEW_LIMIT = 20
VALIDATION_SCOPE_WARNING = "当前只完成语法和拓扑校验，上传前需人工确认平台字段、关系与数据来源。"
PLATFORM_CASE_CSV_ROWS = 200
PLATFORM_CASE_CSV_COLUMNS = 100


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _artifact_render_mode(media_type: str, filename: str) -> str:
    """Choose a renderer using recognized media types first, extension second."""
    media = (media_type or "").split(";", 1)[0].strip().lower()
    if media in {"application/json", "text/json"} or media.endswith("+json"):
        return "json"
    if media in {"text/csv", "application/csv"}:
        return "csv"
    if media in {"text/markdown", "text/x-markdown"}:
        return "markdown"
    if media.startswith("text/"):
        return "text"
    lower = filename.lower()
    if lower.endswith(".json"):
        return "json"
    if lower.endswith(".csv"):
        return "csv"
    if lower.endswith((".md", ".markdown")):
        return "markdown"
    return "text"


def _extract_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.I)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            value = json.loads(text[start : end + 1])
        else:
            raise
    if not isinstance(value, dict):
        raise ValueError("LLM response must be a JSON object")
    return value


def _prompt(session: Session, prompt_type: str) -> PromptTemplate:
    row = session.exec(
        select(PromptTemplate).where(
            PromptTemplate.type == prompt_type,
            PromptTemplate.is_active == True,  # noqa: E712
        ).order_by(col(PromptTemplate.id).desc())
    ).first()
    if row is None:
        raise HTTPException(status_code=503, detail=f"Active prompt not found: {prompt_type}")
    return row


def _model(session: Session) -> ModelConfig:
    row = session.exec(
        select(ModelConfig).order_by(col(ModelConfig.is_default).desc(), col(ModelConfig.id).desc())
    ).first()
    if row is None:
        raise HTTPException(status_code=503, detail="No ModelConfig available for LLM generation")
    return row


def _call(session: Session, messages: list[dict[str, str]], hook: Callable[..., Any] | None) -> tuple[str, str]:
    model = _model(session)
    if hook is not None:
        try:
            result = hook(messages=messages, model=model)
        except TypeError:
            result = hook(messages)
        raw = result[0] if isinstance(result, tuple) else result
        return str(raw), f"{model.name}:{model.model_name}"
    raw, _usage = chat_completion(
        base_url=model.base_url,
        api_key=model.api_key,
        model=model.model_name,
        messages=messages,
    )
    return raw, f"{model.name}:{model.model_name}"


def _space(session: Session, space_id: int) -> WikiSpace:
    row = session.get(WikiSpace, space_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Wiki space not found")
    if row.status != "active":
        raise HTTPException(status_code=409, detail="Wiki space is archived")
    return row


def _infer_schema(records: list[Any]) -> dict[str, Any]:
    fields: dict[str, set[str]] = {}
    for record in records[:200]:
        if not isinstance(record, dict):
            fields.setdefault("value", set()).add(type(record).__name__)
            continue
        for key, value in record.items():
            kind = "null" if value is None else type(value).__name__
            fields.setdefault(str(key), set()).add(kind)
    return {"fields": [{"name": key, "types": sorted(types)} for key, types in fields.items()]}


def _parse_import(body: DataPoolCreate, session: Session) -> tuple[dict[str, Any], list[Any], str | None, str | None]:
    if body.source_kind == "csv":
        records = list(csv.DictReader(io.StringIO(body.content)))
        if not records:
            raise HTTPException(status_code=422, detail="CSV contains no data rows")
        return _infer_schema(records), records, None, None
    if body.source_kind == "json":
        try:
            value = json.loads(body.content)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc.msg}") from exc
        records = value if isinstance(value, list) else [value]
        return _infer_schema(records), records, None, None
    if not body.parse_with_llm:
        records = [{"description": body.content}]
        return _infer_schema(records), records, None, None
    prompt = _prompt(session, "data_description_parse")
    try:
        raw, model_ref = _call(
            session,
            [{"role": "system", "content": prompt.content}, {"role": "user", "content": body.content}],
            _DESCRIPTION_CHAT_FN,
        )
        parsed = _extract_json(raw)
        records = parsed.get("records", [])
        if not isinstance(records, list):
            raise ValueError("records must be an array")
        schema = parsed.get("schema") if isinstance(parsed.get("schema"), dict) else _infer_schema(records)
        return schema, records, model_ref, f"{prompt.id}:v{prompt.version}"
    except (LLMError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail=f"Description parsing failed: {exc}") from exc


def _revision_out(row: DataPoolRevision) -> DataPoolRevisionOut:
    records = _json(row.records_json, [])
    return DataPoolRevisionOut(
        **row.model_dump(exclude={"inferred_schema_json", "records_json", "raw_text"}),
        schema_data=_json(row.inferred_schema_json, {}),
        records=records[:DATA_PREVIEW_LIMIT],
        record_count=len(records),
    )


def _pool_out(session: Session, row: DataPool) -> DataPoolOut:
    latest = session.exec(
        select(DataPoolRevision).where(DataPoolRevision.data_pool_id == row.id)
        .order_by(col(DataPoolRevision.revision).desc())
    ).first()
    return DataPoolOut(
        **row.model_dump(exclude={"attributes_json"}),
        attributes=_json(row.attributes_json, {}),
        latest_revision=_revision_out(latest) if latest else None,
    )


@router.post("/api/data-pools", response_model=DataPoolOut)
def create_data_pool(body: DataPoolCreate, session: Session = Depends(get_session)) -> DataPoolOut:
    _space(session, body.wiki_space_id)
    schema, records, model_ref, prompt_ref = _parse_import(body, session)
    pool = DataPool(
        wiki_space_id=body.wiki_space_id, name=body.name.strip(), description=body.description.strip(),
        attributes_json=json.dumps(body.attributes, ensure_ascii=False),
    )
    session.add(pool)
    session.flush()
    revision = DataPoolRevision(
        data_pool_id=int(pool.id), source_kind=body.source_kind,
        inferred_schema_json=json.dumps(schema, ensure_ascii=False), records_json=json.dumps(records, ensure_ascii=False),
        raw_text=body.content, content_hash=_hash(body.content), model_ref=model_ref, prompt_ref=prompt_ref,
    )
    session.add(revision)
    session.commit()
    session.refresh(pool)
    return _pool_out(session, pool)


@router.get("/api/data-pools", response_model=list[DataPoolOut])
def list_data_pools(wiki_space_id: int = Query(ge=1), include_archived: bool = False, session: Session = Depends(get_session)) -> list[DataPoolOut]:
    statement = select(DataPool).where(DataPool.wiki_space_id == wiki_space_id)
    if not include_archived:
        statement = statement.where(DataPool.status == "active")
    return [_pool_out(session, row) for row in session.exec(statement.order_by(col(DataPool.id).desc())).all()]


@router.get("/api/data-pools/{pool_id}", response_model=DataPoolOut)
def get_data_pool(pool_id: int, wiki_space_id: int = Query(ge=1), session: Session = Depends(get_session)) -> DataPoolOut:
    row = session.get(DataPool, pool_id)
    if row is None or row.wiki_space_id != wiki_space_id:
        raise HTTPException(status_code=404, detail="Data pool not found")
    return _pool_out(session, row)


@router.post("/api/data-pools/{pool_id}/archive", response_model=DataPoolOut)
def archive_data_pool(pool_id: int, wiki_space_id: int = Query(ge=1), session: Session = Depends(get_session)) -> DataPoolOut:
    row = session.get(DataPool, pool_id)
    if row is None or row.wiki_space_id != wiki_space_id:
        raise HTTPException(status_code=404, detail="Data pool not found")
    row.status = "archived"
    session.add(row)
    session.commit()
    session.refresh(row)
    return _pool_out(session, row)


def _example_out(row: PlatformExample) -> ExampleOut:
    return ExampleOut(**row.model_dump())


def _variant_out(session: Session, row: ExampleVariant) -> VariantOut:
    examples = session.exec(select(PlatformExample).where(PlatformExample.variant_id == row.id).order_by(PlatformExample.id)).all()
    return VariantOut(**row.model_dump(), examples=[_example_out(item) for item in examples])


def _platform_out(session: Session, row: PlatformProfile) -> PlatformOut:
    variants = session.exec(select(ExampleVariant).where(ExampleVariant.platform_id == row.id, ExampleVariant.status == "active").order_by(ExampleVariant.id)).all()
    return PlatformOut(**row.model_dump(), variants=[_variant_out(session, item) for item in variants])


def _owned_platform(session: Session, platform_id: int, wiki_space_id: int) -> PlatformProfile:
    row = session.get(PlatformProfile, platform_id)
    if row is None or row.wiki_space_id != wiki_space_id:
        raise HTTPException(status_code=404, detail="Platform not found")
    return row


def _owned_variant(session: Session, platform_id: int, variant_id: int, wiki_space_id: int) -> tuple[PlatformProfile, ExampleVariant]:
    platform = _owned_platform(session, platform_id, wiki_space_id)
    variant = session.get(ExampleVariant, variant_id)
    if variant is None or variant.platform_id != platform.id:
        raise HTTPException(status_code=404, detail="Example type not found")
    return platform, variant


def _example_referenced(session: Session, variant_id: int, example_id: int) -> bool:
    runs = session.exec(select(PlatformRenderRun).where(PlatformRenderRun.variant_id == variant_id)).all()
    for run in runs:
        manifest = _json(run.input_snapshot_json, {})
        refs = manifest.get("example_refs", []) if isinstance(manifest, dict) else []
        if any(isinstance(item, dict) and item.get("id") == example_id for item in refs):
            return True
    return False


@router.post("/api/platforms", response_model=PlatformOut)
def create_platform(body: PlatformCreate, session: Session = Depends(get_session)) -> PlatformOut:
    _space(session, body.wiki_space_id)
    row = PlatformProfile(**body.model_dump())
    session.add(row)
    session.commit()
    session.refresh(row)
    return _platform_out(session, row)


@router.get("/api/platforms", response_model=list[PlatformOut])
def list_platforms(wiki_space_id: int = Query(ge=1), session: Session = Depends(get_session)) -> list[PlatformOut]:
    rows = session.exec(select(PlatformProfile).where(PlatformProfile.wiki_space_id == wiki_space_id, PlatformProfile.status == "active").order_by(PlatformProfile.id)).all()
    return [_platform_out(session, row) for row in rows]


@router.patch("/api/platforms/{platform_id}", response_model=PlatformOut)
def update_platform(platform_id: int, body: PlatformUpdate, wiki_space_id: int = Query(ge=1), session: Session = Depends(get_session)) -> PlatformOut:
    _space(session, wiki_space_id)
    row = _owned_platform(session, platform_id, wiki_space_id)
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(row, key, value.strip() if isinstance(value, str) else value)
    session.add(row)
    session.commit()
    session.refresh(row)
    return _platform_out(session, row)


@router.delete("/api/platforms/{platform_id}")
def delete_platform(platform_id: int, wiki_space_id: int = Query(ge=1), session: Session = Depends(get_session)) -> dict[str, bool]:
    _space(session, wiki_space_id)
    row = _owned_platform(session, platform_id, wiki_space_id)
    if session.exec(select(PlatformRenderRun.id).where(PlatformRenderRun.platform_id == row.id)).first() is not None:
        raise HTTPException(status_code=409, detail="Platform is referenced by render history")
    if session.exec(select(ManagedPlatformCase.id).where(ManagedPlatformCase.platform_id == row.id)).first() is not None:
        raise HTTPException(status_code=409, detail="Platform is referenced by managed platform cases and cannot be deleted")
    variants = session.exec(select(ExampleVariant).where(ExampleVariant.platform_id == row.id)).all()
    variant_ids = [int(item.id) for item in variants]
    if variant_ids:
        examples = session.exec(select(PlatformExample).where(PlatformExample.variant_id.in_(variant_ids))).all()
        for example in examples:
            session.delete(example)
        for variant in variants:
            session.delete(variant)
    session.delete(row)
    session.commit()
    return {"ok": True}


@router.post("/api/platforms/{platform_id}/variants", response_model=VariantOut)
def create_variant(platform_id: int, body: VariantCreate, wiki_space_id: int = Query(ge=1), session: Session = Depends(get_session)) -> VariantOut:
    _space(session, wiki_space_id)
    platform = _owned_platform(session, platform_id, wiki_space_id)
    row = ExampleVariant(platform_id=platform_id, **body.model_dump())
    session.add(row)
    session.commit()
    session.refresh(row)
    return _variant_out(session, row)


@router.patch("/api/platforms/{platform_id}/variants/{variant_id}", response_model=VariantOut)
def update_variant(platform_id: int, variant_id: int, body: VariantUpdate, wiki_space_id: int = Query(ge=1), session: Session = Depends(get_session)) -> VariantOut:
    _space(session, wiki_space_id)
    _platform, row = _owned_variant(session, platform_id, variant_id, wiki_space_id)
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(row, key, value.strip() if isinstance(value, str) else value)
    session.add(row)
    session.commit()
    session.refresh(row)
    return _variant_out(session, row)


@router.delete("/api/platforms/{platform_id}/variants/{variant_id}")
def delete_variant(platform_id: int, variant_id: int, wiki_space_id: int = Query(ge=1), session: Session = Depends(get_session)) -> dict[str, bool]:
    _space(session, wiki_space_id)
    _platform, row = _owned_variant(session, platform_id, variant_id, wiki_space_id)
    if session.exec(select(PlatformRenderRun.id).where(PlatformRenderRun.variant_id == row.id)).first() is not None:
        raise HTTPException(status_code=409, detail="Example type is referenced by render history")
    if session.exec(select(ManagedPlatformCase.id).where(ManagedPlatformCase.variant_id == row.id)).first() is not None:
        raise HTTPException(status_code=409, detail="Example type is referenced by managed platform cases and cannot be deleted")
    examples = session.exec(select(PlatformExample).where(PlatformExample.variant_id == row.id)).all()
    for example in examples:
        session.delete(example)
    session.delete(row)
    session.commit()
    return {"ok": True}


@router.post("/api/platforms/{platform_id}/variants/{variant_id}/examples", response_model=ExampleOut)
def create_example(platform_id: int, variant_id: int, body: ExampleCreate, wiki_space_id: int = Query(ge=1), session: Session = Depends(get_session)) -> ExampleOut:
    _space(session, wiki_space_id)
    _platform, variant = _owned_variant(session, platform_id, variant_id, wiki_space_id)
    row = PlatformExample(variant_id=variant_id, **body.model_dump(), content_hash=_hash(body.content))
    session.add(row)
    session.commit()
    session.refresh(row)
    return _example_out(row)


@router.patch("/api/platforms/{platform_id}/variants/{variant_id}/examples/{example_id}", response_model=ExampleOut)
def update_example(platform_id: int, variant_id: int, example_id: int, body: ExampleUpdate, wiki_space_id: int = Query(ge=1), session: Session = Depends(get_session)) -> ExampleOut:
    _space(session, wiki_space_id)
    _owned_variant(session, platform_id, variant_id, wiki_space_id)
    row = session.get(PlatformExample, example_id)
    if row is None or row.variant_id != variant_id:
        raise HTTPException(status_code=404, detail="Platform example not found")
    for key, value in body.model_dump(exclude_unset=True).items():
        if key in {"name", "media_type"} and isinstance(value, str):
            value = value.strip()
        setattr(row, key, value)
    if body.content is not None:
        row.content_hash = _hash(row.content)
    session.add(row)
    session.commit()
    session.refresh(row)
    return _example_out(row)


@router.delete("/api/platforms/{platform_id}/variants/{variant_id}/examples/{example_id}")
def delete_example(platform_id: int, variant_id: int, example_id: int, wiki_space_id: int = Query(ge=1), session: Session = Depends(get_session)) -> dict[str, bool]:
    _space(session, wiki_space_id)
    _owned_variant(session, platform_id, variant_id, wiki_space_id)
    row = session.get(PlatformExample, example_id)
    if row is None or row.variant_id != variant_id:
        raise HTTPException(status_code=404, detail="Platform example not found")
    if _example_referenced(session, variant_id, example_id):
        raise HTTPException(status_code=409, detail="Platform example is referenced by render history and cannot be deleted")
    session.delete(row)
    session.commit()
    return {"ok": True}


def _run_out(session: Session, run: PlatformRenderRun) -> RenderRunOut:
    artifacts = session.exec(select(PlatformArtifact).where(PlatformArtifact.run_id == run.id).order_by(PlatformArtifact.id)).all()
    platform = session.get(PlatformProfile, run.platform_id)
    variant = session.get(ExampleVariant, run.variant_id)
    manifest = _json(run.input_snapshot_json, {})
    platform_snapshot = manifest.get("platform_snapshot", {}) if isinstance(manifest, dict) else {}
    variant_snapshot = manifest.get("variant_snapshot", {}) if isinstance(manifest, dict) else {}
    return RenderRunOut(
        **run.model_dump(exclude={"warnings_json", "input_snapshot_json"}),
        platform_name=str(platform_snapshot.get("name") or (platform.name if platform else "")),
        variant_name=str(variant_snapshot.get("name") or (variant.name if variant else "")),
        warnings=_json(run.warnings_json, []), artifacts=[ArtifactOut(**item.model_dump()) for item in artifacts],
    )


def _validate_artifacts(topology: str, artifacts: Any) -> tuple[list[dict[str, str]], list[str]]:
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("artifacts must be a non-empty array")
    if len(artifacts) > 20:
        raise ValueError("artifacts must contain at most 20 items")
    clean: list[dict[str, str]] = []
    kinds: set[str] = set()
    total_content_bytes = 0
    for item in artifacts:
        if not isinstance(item, dict) or item.get("kind") not in ARTIFACT_KINDS:
            raise ValueError("artifact kind is invalid")
        if not str(item.get("filename", "")).strip() or not str(item.get("content", "")).strip():
            raise ValueError("artifact filename and content are required")
        filename = str(item["filename"]).strip()
        declared_media_type = item.get("media_type")
        media_type = str(declared_media_type or "text/plain").lower()
        content = str(item["content"])
        content_bytes = len(content.encode("utf-8"))
        if content_bytes > 1_000_000:
            raise ValueError(f"Artifact {filename} content exceeds 1 MB")
        total_content_bytes += content_bytes
        if total_content_bytes > 5_000_000:
            raise ValueError("Total artifact content exceeds 5 MB")
        if len(filename) > 240 or filename in {".", ".."} or "/" in filename or "\\" in filename:
            raise ValueError("artifact filename is unsafe")
        if any(ord(char) < 32 or ord(char) == 127 for char in filename):
            raise ValueError("artifact filename is unsafe")
        render_mode = _artifact_render_mode(str(declared_media_type or ""), filename)
        if render_mode == "json":
            try:
                json.loads(content)
            except (json.JSONDecodeError, RecursionError) as exc:
                message = exc.msg if isinstance(exc, json.JSONDecodeError) else "nesting is too deep"
                raise ValueError(f"Artifact {filename} contains invalid JSON: {message}") from exc
        if render_mode == "csv":
            try:
                reader = csv.reader(io.StringIO(content), strict=True)
                header = next(reader, None)
                if not header or any(not str(cell).strip() for cell in header):
                    raise ValueError(f"Artifact {filename} requires a non-empty CSV header")
                width = len(header)
                for row in reader:
                    if len(row) != width:
                        raise ValueError(f"Artifact {filename} contains inconsistent CSV rows")
            except csv.Error as exc:
                raise ValueError(f"Artifact {filename} contains invalid CSV: {exc}") from exc
        clean.append({
            "kind": item["kind"], "filename": filename,
            "media_type": media_type, "content": content,
        })
        kinds.add(item["kind"])
    warnings: list[str] = []
    if topology == "separated" and not {"case", "data"}.issubset(kinds):
        raise ValueError("Separated platform requires case and data artifacts")
    if topology == "separated" and "relation" not in kinds:
        warnings.append("分离式平台未生成 relation 产物，请确认平台是否通过内置字段关联。")
    if topology == "combined" and "combined" not in kinds:
        raise ValueError("Combined platform requires a combined artifact")
    if topology == "hybrid" and "combined" not in kinds and not {"case", "data"}.issubset(kinds):
        raise ValueError("Hybrid platform requires combined or case+data artifacts")
    return clean, warnings


def _read_space(session: Session, space_id: int) -> WikiSpace:
    row = session.get(WikiSpace, space_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Wiki space not found")
    return row


def _sync_managed_platform_cases(session: Session, wiki_space_id: int) -> int:
    """Backfill copies with an atomic SQLite upsert, safe across concurrent lists."""
    rows = session.exec(
        select(PlatformArtifact, PlatformRenderRun)
        .join(PlatformRenderRun, PlatformArtifact.run_id == PlatformRenderRun.id)
        .where(PlatformRenderRun.wiki_space_id == wiki_space_id)
    ).all()
    created = 0
    for artifact, run in rows:
        statement = sqlite_insert(ManagedPlatformCase).values(
            wiki_space_id=wiki_space_id,
            platform_id=run.platform_id,
            variant_id=run.variant_id,
            source_run_id=run.id,
            source_artifact_id=artifact.id,
            filename=artifact.filename,
            name=artifact.filename,
            kind=artifact.kind,
            media_type=artifact.media_type,
            content=artifact.content,
            content_hash=artifact.content_hash,
        ).on_conflict_do_nothing(index_elements=["source_artifact_id"])
        result = session.execute(statement)
        created += max(int(result.rowcount or 0), 0)
    session.commit()
    return created


def _platform_case_summary(session: Session, row: ManagedPlatformCase) -> PlatformCaseSummary:
    platform = session.get(PlatformProfile, row.platform_id)
    variant = session.get(ExampleVariant, row.variant_id) if row.variant_id else None
    return PlatformCaseSummary(
        **row.model_dump(),
        platform_name=platform.name if platform else "",
        variant_name=variant.name if variant else "",
        content_length=len(row.content),
    )


def _bounded_json_preview(value: Any, max_depth: int = 20, max_nodes: int = 2000) -> tuple[Any, bool]:
    nodes = 0
    truncated = False

    def visit(item: Any, depth: int) -> Any:
        nonlocal nodes, truncated
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            truncated = True
            return "<truncated>"
        if isinstance(item, dict):
            output: dict[str, Any] = {}
            for key, child in item.items():
                if nodes >= max_nodes:
                    truncated = True
                    output["<truncated>"] = "<truncated>"
                    break
                output[str(key)] = visit(child, depth + 1)
            return output
        if isinstance(item, list):
            output: list[Any] = []
            for child in item:
                if nodes >= max_nodes:
                    truncated = True
                    output.append("<truncated>")
                    break
                output.append(visit(child, depth + 1))
            return output
        return item

    return visit(value, 0), truncated


def _platform_case_detail(session: Session, row: ManagedPlatformCase) -> PlatformCaseDetail:
    summary = _platform_case_summary(session, row)
    mode = _artifact_render_mode(row.media_type, row.filename)
    values: dict[str, Any] = {
        "content": row.content,
        "render_mode": mode,
        "json_value": None,
        "json_truncated": False,
        "csv_headers": [],
        "csv_rows": [],
        "csv_total_rows": 0,
        "csv_truncated": False,
    }
    if mode == "json":
        try:
            parsed = json.loads(row.content)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise HTTPException(status_code=422, detail="Managed platform case contains invalid JSON") from exc
        values["json_value"], values["json_truncated"] = _bounded_json_preview(parsed)
    elif mode == "csv":
        try:
            reader = csv.reader(io.StringIO(row.content), strict=True)
            headers = next(reader, [])
            preview_rows: list[list[str]] = []
            total_rows = 0
            for item in reader:
                total_rows += 1
                if len(preview_rows) < PLATFORM_CASE_CSV_ROWS:
                    preview_rows.append(item[:PLATFORM_CASE_CSV_COLUMNS])
            values.update({
                "csv_headers": headers[:PLATFORM_CASE_CSV_COLUMNS],
                "csv_rows": preview_rows,
                "csv_total_rows": total_rows,
                "csv_truncated": total_rows > PLATFORM_CASE_CSV_ROWS or len(headers) > PLATFORM_CASE_CSV_COLUMNS,
            })
        except csv.Error as exc:
            raise HTTPException(status_code=422, detail="Managed platform case contains invalid CSV") from exc
    return PlatformCaseDetail(**summary.model_dump(), **values)


def _owned_platform_case(session: Session, case_id: int, wiki_space_id: int) -> ManagedPlatformCase:
    row = session.get(ManagedPlatformCase, case_id)
    if row is None or row.wiki_space_id != wiki_space_id:
        raise HTTPException(status_code=404, detail="Platform case not found")
    return row


def _validate_platform_case_values(filename: str, kind: str, media_type: str, content: str) -> dict[str, str]:
    try:
        clean, _warnings = _validate_artifacts("managed", [{
            "filename": filename, "kind": kind, "media_type": media_type, "content": content,
        }])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return clean[0]


@router.get("/api/platform-cases", response_model=PlatformCasePage)
def list_platform_cases(
    wiki_space_id: int = Query(ge=1),
    platform_id: int | None = Query(default=None, ge=1),
    variant_id: int | None = Query(default=None, ge=1),
    media_type: str | None = None,
    search: str | None = None,
    include_archived: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> PlatformCasePage:
    space = _read_space(session, wiki_space_id)
    if space.status == "active":
        _sync_managed_platform_cases(session, wiki_space_id)
    conditions: list[Any] = [ManagedPlatformCase.wiki_space_id == wiki_space_id]
    if not include_archived:
        conditions.append(ManagedPlatformCase.status == "active")
    if platform_id:
        conditions.append(ManagedPlatformCase.platform_id == platform_id)
    if variant_id:
        conditions.append(ManagedPlatformCase.variant_id == variant_id)
    if media_type:
        conditions.append(ManagedPlatformCase.media_type == media_type)
    needle = (search or "").strip().lower()
    if needle:
        conditions.append(or_(
            func.lower(ManagedPlatformCase.filename).contains(needle),
            func.lower(ManagedPlatformCase.name).contains(needle),
        ))
    total = int(session.exec(select(func.count()).select_from(ManagedPlatformCase).where(*conditions)).one())
    statement = select(ManagedPlatformCase).where(*conditions)
    rows = session.exec(
        statement.order_by(ManagedPlatformCase.updated_at.desc(), ManagedPlatformCase.id.desc()).offset(offset).limit(limit)
    ).all()
    return PlatformCasePage(items=[_platform_case_summary(session, row) for row in rows], total=total, limit=limit, offset=offset)


@router.post("/api/platform-cases", response_model=PlatformCaseDetail)
def create_platform_case(body: PlatformCaseCreate, session: Session = Depends(get_session)) -> PlatformCaseDetail:
    _space(session, body.wiki_space_id)
    _owned_platform(session, body.platform_id, body.wiki_space_id)
    if body.variant_id is not None:
        _owned_variant(session, body.platform_id, body.variant_id, body.wiki_space_id)
    clean = _validate_platform_case_values(body.filename, body.kind, body.media_type, body.content)
    row = ManagedPlatformCase(
        wiki_space_id=body.wiki_space_id, platform_id=body.platform_id, variant_id=body.variant_id,
        name=body.name.strip(), **clean, content_hash=_hash(clean["content"]),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return _platform_case_detail(session, row)


@router.get("/api/platform-cases/{case_id}", response_model=PlatformCaseDetail)
def get_platform_case(case_id: int, wiki_space_id: int = Query(ge=1), session: Session = Depends(get_session)) -> PlatformCaseDetail:
    _read_space(session, wiki_space_id)
    return _platform_case_detail(session, _owned_platform_case(session, case_id, wiki_space_id))


@router.patch("/api/platform-cases/{case_id}", response_model=PlatformCaseDetail)
def update_platform_case(case_id: int, body: PlatformCaseUpdate, wiki_space_id: int = Query(ge=1), session: Session = Depends(get_session)) -> PlatformCaseDetail:
    _space(session, wiki_space_id)
    row = _owned_platform_case(session, case_id, wiki_space_id)
    if row.status == "archived":
        raise HTTPException(status_code=409, detail="Archived platform case must be restored before editing")
    changes = body.model_dump(exclude_unset=True)
    filename = changes.get("filename", row.filename)
    kind = changes.get("kind", row.kind)
    media_type = changes.get("media_type", row.media_type)
    content = changes.get("content", row.content)
    clean = _validate_platform_case_values(filename, kind, media_type, content)
    row.filename, row.kind, row.media_type, row.content = clean["filename"], clean["kind"], clean["media_type"], clean["content"]
    if "name" in changes:
        row.name = changes["name"].strip()
    row.content_hash = _hash(row.content)
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(row)
    session.commit()
    session.refresh(row)
    return _platform_case_detail(session, row)


@router.delete("/api/platform-cases/{case_id}", response_model=PlatformCaseDetail)
def archive_platform_case(case_id: int, wiki_space_id: int = Query(ge=1), session: Session = Depends(get_session)) -> PlatformCaseDetail:
    _space(session, wiki_space_id)
    row = _owned_platform_case(session, case_id, wiki_space_id)
    row.status = "archived"
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(row); session.commit(); session.refresh(row)
    return _platform_case_detail(session, row)


@router.post("/api/platform-cases/{case_id}/restore", response_model=PlatformCaseDetail)
def restore_platform_case(case_id: int, wiki_space_id: int = Query(ge=1), session: Session = Depends(get_session)) -> PlatformCaseDetail:
    _space(session, wiki_space_id)
    row = _owned_platform_case(session, case_id, wiki_space_id)
    row.status = "active"
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(row); session.commit(); session.refresh(row)
    return _platform_case_detail(session, row)


def _fail_run(session: Session, run_id: int, error: Exception) -> None:
    """Persist a terminal failure even when the render transaction is dirty."""

    session.rollback()
    row = session.get(PlatformRenderRun, run_id)
    if row is None:
        return
    row.status = "failed"
    row.error_message = str(error)
    session.add(row)
    session.commit()


@router.get("/api/platform-semantic-cases", response_model=list[SemanticCaseOut])
def list_platform_semantic_cases(
    wiki_space_id: int = Query(ge=1),
    session: Session = Depends(get_session),
) -> list[SemanticCaseOut]:
    """Return only active imported cases whose source task belongs to the space."""

    _space(session, wiki_space_id)
    rows = session.exec(
        select(TestCase)
        .join(GenerationTask, GenerationTask.id == TestCase.source_task_id)
        .where(
            GenerationTask.wiki_space_id == wiki_space_id,
            TestCase.status == "active",
        )
        .order_by(col(TestCase.id).desc())
    ).all()
    return [
        SemanticCaseOut(
            id=int(row.id), case_key=row.case_key, title=row.title,
            priority=row.priority, content_md=row.content_md,
        )
        for row in rows
    ]


@router.post("/api/platform-renders/example-direct", response_model=RenderRunOut)
def render_example_direct(body: PlatformRenderRequest, session: Session = Depends(get_session)) -> RenderRunOut:
    _space(session, body.wiki_space_id)
    platform = session.get(PlatformProfile, body.platform_id)
    variant = session.get(ExampleVariant, body.variant_id)
    if platform is None or platform.wiki_space_id != body.wiki_space_id:
        raise HTTPException(status_code=404, detail="Platform not found")
    if variant is None or variant.platform_id != platform.id or variant.status != "active":
        raise HTTPException(status_code=404, detail="Selected platform variant not found")
    examples = session.exec(select(PlatformExample).where(PlatformExample.variant_id == variant.id).order_by(PlatformExample.id)).all()
    if not examples:
        raise HTTPException(status_code=422, detail="当前示例类型没有平台示例，请先保存至少一个平台示例")

    cases: list[dict[str, Any]] = []
    for case_id in dict.fromkeys(body.test_case_ids):
        case = session.get(TestCase, case_id)
        task = session.get(GenerationTask, case.source_task_id) if case and case.source_task_id else None
        if case is None or task is None or task.wiki_space_id != body.wiki_space_id:
            raise HTTPException(status_code=404, detail=f"Test case {case_id} not found in wiki space")
        cases.append({"id": case.id, "case_key": case.case_key, "title": case.title, "priority": case.priority, "content_md": case.content_md})

    data: list[dict[str, Any]] = []
    data_manifest: list[dict[str, Any]] = []
    for revision_id in dict.fromkeys(body.data_pool_revision_ids):
        revision = session.get(DataPoolRevision, revision_id)
        pool = session.get(DataPool, revision.data_pool_id) if revision else None
        if revision is None or pool is None or pool.wiki_space_id != body.wiki_space_id or pool.status != "active":
            raise HTTPException(status_code=404, detail=f"Data pool revision {revision_id} not found in wiki space")
        records = _json(revision.records_json, [])
        data_item = {
            "pool": pool.name,
            "revision_id": revision.id,
            "schema": _json(revision.inferred_schema_json, {}),
            "record_count": len(records),
        }
        if body.include_data_values:
            data_item["records"] = records[:body.data_sample_limit]
        data.append(data_item)
        data_manifest.append({
            "pool_id": pool.id,
            "revision_id": revision.id,
            "content_hash": revision.content_hash,
            "record_count": len(records),
            "values_included": body.include_data_values,
            "sample_count": min(len(records), body.data_sample_limit) if body.include_data_values else 0,
        })

    # Security/correctness boundary: examples are fetched by selected variant ID only.
    llm_payload = {
        "platform": {"id": platform.id, "name": platform.name, "artifact_topology": platform.artifact_topology},
        "selected_variant": {"id": variant.id, "name": variant.name, "applicability": variant.applicability},
        "selected_variant_examples": [{"kind": item.kind, "name": item.name, "media_type": item.media_type, "content": item.content} for item in examples],
        "semantic_cases": cases,
        "external_case_markdown": body.external_case_markdown,
        "keywords": body.keywords,
        "test_data": data,
    }
    payload_text = json.dumps(llm_payload, ensure_ascii=False, sort_keys=True)
    manifest = {
        "platform_id": platform.id,
        "variant_id": variant.id,
        "platform_snapshot": {"name": platform.name, "artifact_topology": platform.artifact_topology},
        "variant_snapshot": {"name": variant.name, "applicability": variant.applicability},
        "example_refs": [
            {"id": item.id, "kind": item.kind, "content_hash": item.content_hash}
            for item in examples
        ],
        "semantic_case_refs": [
            {"id": item["id"], "content_hash": _hash(item["content_md"])}
            for item in cases
        ],
        "external_case_markdown": (
            {"content_hash": _hash(body.external_case_markdown), "length": len(body.external_case_markdown)}
            if body.external_case_markdown else None
        ),
        "keyword_refs": [{"content_hash": _hash(item), "length": len(item)} for item in body.keywords],
        "data_refs": data_manifest,
        "include_data_values": body.include_data_values,
        "data_sample_limit": body.data_sample_limit,
    }
    manifest_text = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    prompt = _prompt(session, "platform_example_render")
    run = PlatformRenderRun(
        wiki_space_id=body.wiki_space_id, platform_id=platform.id, variant_id=variant.id,
        input_snapshot_json=manifest_text, input_hash=_hash(payload_text), prompt_ref=f"{prompt.id}:v{prompt.version}",
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    try:
        raw, model_ref = _call(session, [{"role": "system", "content": prompt.content}, {"role": "user", "content": payload_text}], _PLATFORM_CHAT_FN)
        run.model_ref = model_ref
        payload = _extract_json(raw)
        artifacts, topology_warnings = _validate_artifacts(platform.artifact_topology, payload.get("artifacts"))
        warnings = payload.get("warnings", []) if isinstance(payload.get("warnings"), list) else []
        warnings = [str(item) for item in warnings] + topology_warnings + [VALIDATION_SCOPE_WARNING]
        for item in artifacts:
            artifact = PlatformArtifact(run_id=int(run.id), **item, content_hash=_hash(item["content"]))
            session.add(artifact)
            session.flush()
            session.add(ManagedPlatformCase(
                wiki_space_id=body.wiki_space_id,
                platform_id=platform.id,
                variant_id=variant.id,
                source_run_id=run.id,
                source_artifact_id=artifact.id,
                filename=artifact.filename,
                name=artifact.filename,
                kind=artifact.kind,
                media_type=artifact.media_type,
                content=artifact.content,
                content_hash=artifact.content_hash,
            ))
        run.status = "completed"
        run.warnings_json = json.dumps(warnings, ensure_ascii=False)
        session.add(run)
        session.commit()
        session.refresh(run)
        return _run_out(session, run)
    except HTTPException as exc:
        _fail_run(session, int(run.id), exc)
        raise HTTPException(
            status_code=exc.status_code,
            detail=f"Platform render failed (run {run.id}): {exc.detail}",
        ) from exc
    except Exception as exc:
        _fail_run(session, int(run.id), exc)
        raise HTTPException(status_code=502, detail=f"Platform render failed (run {run.id}): {exc}") from exc


@router.get("/api/platform-renders", response_model=list[RenderRunOut])
def list_render_runs(
    wiki_space_id: int = Query(ge=1),
    platform_id: int | None = Query(default=None, ge=1),
    session: Session = Depends(get_session),
) -> list[RenderRunOut]:
    statement = select(PlatformRenderRun).where(PlatformRenderRun.wiki_space_id == wiki_space_id)
    if platform_id is not None:
        platform = _owned_platform(session, platform_id, wiki_space_id)
        statement = statement.where(PlatformRenderRun.platform_id == platform.id)
    rows = session.exec(statement.order_by(col(PlatformRenderRun.id).desc())).all()
    return [_run_out(session, row) for row in rows]


@router.get("/api/platform-renders/{run_id}", response_model=RenderRunOut)
def get_render_run(run_id: int, wiki_space_id: int = Query(ge=1), session: Session = Depends(get_session)) -> RenderRunOut:
    run = session.get(PlatformRenderRun, run_id)
    if run is None or run.wiki_space_id != wiki_space_id:
        raise HTTPException(status_code=404, detail="Platform render run not found")
    return _run_out(session, run)
