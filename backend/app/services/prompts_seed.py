from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, select

from app.models.entities import PromptTemplate

PROMPT_TYPES = (
    "generate",
    "review",
    "optimize",
    "wiki_analyze",
    "wiki_write",
)

DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "default_prompts"


def seed_default_prompts(session: Session) -> None:
    """Insert version=1 active default prompts when no active row exists for a type."""
    for ptype in PROMPT_TYPES:
        active = session.exec(
            select(PromptTemplate).where(
                PromptTemplate.type == ptype,
                PromptTemplate.is_active == True,  # noqa: E712
            )
        ).first()
        if active is not None:
            continue

        path = DEFAULT_PROMPTS_DIR / f"{ptype}.md"
        content = path.read_text(encoding="utf-8")
        session.add(
            PromptTemplate(
                name=f"default_{ptype}",
                type=ptype,
                content=content,
                version=1,
                is_active=True,
            )
        )
    session.commit()
