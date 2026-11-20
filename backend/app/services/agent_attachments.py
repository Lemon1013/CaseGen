"""Safe storage and resolution of files attached to Agent messages."""

from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import config
from app.services import agent_gateway
from app.services.parse_document import parse_document

_HEX32 = re.compile(r"^[0-9a-f]{32}$")


def _workspace_root(project_id: str | int, workspace_dir: str | None) -> Path:
    raw = str(workspace_dir or "").strip()
    root = Path(raw).expanduser() if raw else agent_gateway.ensure_project_workspace_dir(project_id)
    root = root.resolve()
    allowed_root = Path(config.agent_workspace_root()).resolve()
    try:
        root.relative_to(allowed_root)
    except ValueError:
        raise ValueError("工作区路径无效") from None
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_child(root: Path, *parts: str) -> Path:
    if any(not part or Path(part).is_absolute() or ".." in Path(part).parts for part in parts):
        raise ValueError("附件路径无效")
    target = (root.joinpath(*parts)).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError("附件路径超出工作区") from None
    return target


def save_attachment(*, project_id: str | int, conversation_id: str, filename: str,
                    content: bytes, content_type: str | None = None,
                    workspace_dir: str | None = None) -> dict[str, Any]:
    if not _HEX32.fullmatch(str(conversation_id)):
        raise ValueError("会话 ID 无效")
    if not filename or Path(filename).name != filename or ".." in Path(filename).parts:
        raise ValueError("文件名无效")
    suffix = Path(filename).suffix.lower()
    if suffix not in config.ALLOWED_EXTENSIONS:
        raise ValueError("不支持的附件类型")
    if len(content) > config.MAX_UPLOAD_BYTES:
        raise ValueError(f"附件超过 {config.MAX_UPLOAD_BYTES} 字节限制")
    root = _workspace_root(project_id, workspace_dir)
    attachment_id = uuid.uuid4().hex
    directory = _safe_child(root, "attachments", str(conversation_id), attachment_id)
    directory.mkdir(parents=True, exist_ok=False)
    original = directory / f"original{suffix}"
    parsed = directory / "parsed.txt"
    meta_path = directory / "meta.json"
    try:
        original.write_bytes(content)
        parsed_doc = parse_document(original)
        text = str(parsed_doc.text or "")
        if not text.strip():
            raise ValueError("附件没有可提取的文本")
        parsed.write_text(text, encoding="utf-8")
        meta = {
            "id": attachment_id, "conversation_id": str(conversation_id),
            "project_id": str(project_id), "filename": filename, "size": len(content),
            "content_type": content_type or "application/octet-stream", "char_count": len(text),
            "original_path": original.relative_to(root).as_posix(),
            "parsed_path": parsed.relative_to(root).as_posix(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return meta
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def load_attachment_meta(*, project_id: str | int, conversation_id: str, attachment_id: str,
                         workspace_dir: str | None = None) -> dict[str, Any]:
    if not _HEX32.fullmatch(str(conversation_id)) or not _HEX32.fullmatch(str(attachment_id)):
        raise ValueError("附件 ID 无效")
    root = _workspace_root(project_id, workspace_dir)
    meta_path = _safe_child(root, "attachments", str(conversation_id), str(attachment_id), "meta.json")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("附件元数据无效") from exc
    if not isinstance(meta, dict) or meta.get("id") != attachment_id or meta.get("conversation_id") != conversation_id:
        raise ValueError("附件元数据无效")
    for key in ("original_path", "parsed_path"):
        value = meta.get(key)
        if not isinstance(value, str) or Path(value).is_absolute() or ".." in Path(value).parts:
            raise ValueError("附件路径无效")
        if not _safe_child(root, *Path(value).parts).is_file():
            raise ValueError("附件文件不存在")
    if str(meta.get("project_id")) != str(project_id):
        raise ValueError("附件不属于当前项目")
    return meta


def resolve_attachments(*, project_id: str | int, conversation_id: str, attachment_ids: list[str],
                        workspace_dir: str | None = None) -> list[dict[str, Any]]:
    if len(attachment_ids) > 5 or len(set(attachment_ids)) != len(attachment_ids):
        raise ValueError("最多选择 5 个不同附件")
    return [load_attachment_meta(project_id=project_id, conversation_id=conversation_id,
                                 attachment_id=item, workspace_dir=workspace_dir) for item in attachment_ids]


def delete_conversation_attachments(*, project_id: str | int, conversation_id: str,
                                    workspace_dir: str | None = None) -> None:
    if not _HEX32.fullmatch(str(conversation_id)):
        return
    root = _workspace_root(project_id, workspace_dir)
    shutil.rmtree(_safe_child(root, "attachments", str(conversation_id)), ignore_errors=True)
