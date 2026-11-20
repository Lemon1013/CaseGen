"""Idempotent migration for project visibility and conversation ownership.

Simulates a pre-upgrade SQLite database by dropping the two new columns,
writing legacy rows with NULL values, then running init_db() repeatedly.
"""

from __future__ import annotations

import sqlite3

from sqlalchemy import text
from sqlmodel import Session, select

from app.db import get_engine, init_db, reset_engine
from app.models.entities import AgentConversation, Project, User
from app.services.auth import hash_password


def _columns(db_path, table: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _drop_legacy_columns(db_path) -> None:
    """Recreate a pre-upgrade shape by removing the two added columns."""
    with sqlite3.connect(db_path) as conn:
        proj = [row[1] for row in conn.execute("PRAGMA table_info(projects)")]
        conv = [row[1] for row in conn.execute("PRAGMA table_info(agent_conversations)")]
        if "is_public" in proj:
            kept = [c for c in proj if c != "is_public"]
            conn.execute("ALTER TABLE projects RENAME TO projects_old")
            conn.execute(
                f"CREATE TABLE projects ({', '.join(kept)})"
            )
            conn.execute(
                f"INSERT INTO projects ({', '.join(kept)}) SELECT {', '.join(kept)} FROM projects_old"
            )
            conn.execute("DROP TABLE projects_old")
        if "user_id" in conv:
            kept = [c for c in conv if c != "user_id"]
            conn.execute("ALTER TABLE agent_conversations RENAME TO agent_conversations_old")
            conn.execute(
                f"CREATE TABLE agent_conversations ({', '.join(kept)})"
            )
            conn.execute(
                f"INSERT INTO agent_conversations ({', '.join(kept)}) SELECT {', '.join(kept)} FROM agent_conversations_old"
            )
            conn.execute("DROP TABLE agent_conversations_old")
        conn.commit()


def test_migration_adds_columns_and_backfills_legacy_rows(tmp_app_data):
    init_db()
    db_path = tmp_app_data / "meta" / "app.db"
    engine = get_engine()

    # Seed an admin and a legacy conversation before simulating the upgrade.
    with Session(engine) as session:
        admin = User(
            username="admin",
            password_hash=hash_password("CaseGen-Test1!"),
            role="admin",
            is_active=True,
        )
        session.add(admin)
        session.flush()
        project = session.exec(select(Project)).first()
        project_id = int(project.id)
        legacy = AgentConversation(project_id=str(project_id), title="legacy chat")
        session.add(legacy)
        session.commit()
        legacy_id = str(legacy.id)
        admin_id = int(admin.id)

    # Emulate an old database that has neither new column.
    _drop_legacy_columns(db_path)
    assert "is_public" not in _columns(db_path, "projects")
    assert "user_id" not in _columns(db_path, "agent_conversations")

    reset_engine()
    init_db()

    assert "is_public" in _columns(db_path, "projects")
    assert "user_id" in _columns(db_path, "agent_conversations")
    with Session(get_engine()) as session:
        project = session.get(Project, project_id)
        conversation = session.get(AgentConversation, legacy_id)
        assert bool(project.is_public) is True
        assert conversation.user_id == admin_id

    # A second run (another create_app) must be idempotent.
    reset_engine()
    init_db()
    with Session(get_engine()) as session:
        assert bool(session.get(Project, project_id).is_public) is True
        assert session.get(AgentConversation, legacy_id).user_id == admin_id


def test_backfill_never_reassigns_an_explicit_owner(tmp_app_data):
    init_db()
    db_path = tmp_app_data / "meta" / "app.db"
    with Session(get_engine()) as session:
        admin = User(
            username="admin",
            password_hash=hash_password("CaseGen-Test1!"),
            role="admin",
            is_active=True,
        )
        member = User(
            username="member",
            password_hash=hash_password("CaseGen-Test1!"),
            role="user",
            is_active=True,
        )
        session.add(admin)
        session.add(member)
        session.flush()
        project = session.exec(select(Project)).first()
        own = AgentConversation(
            project_id=str(project.id), title="member chat", user_id=int(member.id)
        )
        session.add(own)
        session.commit()
        own_id = str(own.id)
        member_id = int(member.id)

    # Even with multiple users and repeated app construction, an already
    # owned conversation keeps its owner.
    for _ in range(3):
        reset_engine()
        init_db()
    with Session(get_engine()) as session:
        assert session.get(AgentConversation, own_id).user_id == member_id


def test_indexes_are_created(tmp_app_data):
    init_db()
    db_path = tmp_app_data / "meta" / "app.db"
    with sqlite3.connect(db_path) as conn:
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(projects)")}
        conv_indexes = {row[1] for row in conn.execute("PRAGMA index_list(agent_conversations)")}
    assert any("is_public" in name for name in indexes)
    assert any("user_id" in name for name in conv_indexes)
