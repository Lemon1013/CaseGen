from pathlib import Path

import pytest

from app.services import agent_attachments


CONVERSATION_ID = "0123456789abcdef0123456789abcdef"


def test_save_load_and_delete_attachment(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.agent_workspace_root", lambda: tmp_path)
    meta = agent_attachments.save_attachment(
        project_id=7,
        conversation_id=CONVERSATION_ID,
        filename="需求.md",
        content="# 需求\n支持附件。".encode(),
        content_type="text/markdown",
        workspace_dir=str(tmp_path / "project-7"),
    )
    root = tmp_path / "project-7"
    assert (root / meta["original_path"]).is_file()
    assert (root / meta["parsed_path"]).read_text(encoding="utf-8") == "# 需求\n支持附件。"
    assert agent_attachments.load_attachment_meta(
        project_id=7, conversation_id=CONVERSATION_ID, attachment_id=meta["id"], workspace_dir=str(root)
    )["filename"] == "需求.md"
    agent_attachments.delete_conversation_attachments(
        project_id=7, conversation_id=CONVERSATION_ID, workspace_dir=str(root)
    )
    assert not (root / "attachments" / CONVERSATION_ID).exists()


def test_failed_parse_leaves_no_attachment_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.agent_workspace_root", lambda: tmp_path)
    with pytest.raises(ValueError):
        agent_attachments.save_attachment(
            project_id=7,
            conversation_id=CONVERSATION_ID,
            filename="empty.txt",
            content=b"   \n",
            workspace_dir=str(tmp_path / "project-7"),
        )
    attachments = tmp_path / "project-7" / "attachments" / CONVERSATION_ID
    assert not attachments.exists() or not list(attachments.iterdir())


def test_workspace_path_must_be_under_agent_root(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.agent_workspace_root", lambda: tmp_path / "allowed")
    with pytest.raises(ValueError):
        agent_attachments.save_attachment(
            project_id=7,
            conversation_id=CONVERSATION_ID,
            filename="x.md",
            content=b"text",
            workspace_dir=str(tmp_path / "outside"),
        )
