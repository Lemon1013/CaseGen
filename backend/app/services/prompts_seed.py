from __future__ import annotations

import hashlib
from pathlib import Path

from sqlmodel import Session, col, select

from app.models.entities import PromptTemplate

PROMPT_TYPES = (
    "generate",
    "test_points",
    "review",
    "optimize",
    "requirement_optimize",
    "platform_example_render",
    "data_description_parse",
)

# `generate` supports per-task template selection.  Every other type must
# keep exactly one active row so prompt resolution is deterministic.
MULTI_ACTIVE_PROMPT_TYPES = frozenset({"generate"})
USER_MANAGEABLE_PROMPT_TYPES = frozenset({"generate"})
ADMIN_ONLY_PROMPT_TYPES = frozenset(PROMPT_TYPES) - USER_MANAGEABLE_PROMPT_TYPES
PROMPT_VISIBILITIES = frozenset({"system", "shared", "private"})

DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "default_prompts"

# Bump this only when the bundled prompt contract changes. Recognized v1 hashes
# let existing installations upgrade without replacing a user-created prompt.
BUNDLED_PROMPT_VERSION = 11
_LEGACY_DEFAULT_HASHES: dict[str, frozenset[str]] = {
    "generate": frozenset(
        {
            "06e195415664f40e05100de9058f6e43858286771ad687dbe9154df6e3dd7f6f",
            "167b82bf890c61ada53f09f2ee922d5288956afe605e79c156307b5b9f1ba3c0",
            "eabd30ec9639b5d445ad766c1161e24b583625c0c50841214694475251d9eb22",
        }
    ),
    "review": frozenset(
        {
            "3081b648c1625d6c9645f2fd9648c49cff262dbeb3928d60c7418185e13ab473",
            "d0880132a4281dbfc3f2b8e5b0e9e25fac0f9ccbf2a367175f926ba5498fb74b",
            "e27f9ec05a49ed093dc64b875496a9db8fd7771cd8bdbb5db9fcc5de8496f541",
        }
    ),
    "test_points": frozenset(
        {
            "109697d4cf72d2390c16b802abcc5c157777910f0594264497e162d4c66f4ba3",
        }
    ),
    "optimize": frozenset(
        {
            "ac8acfdc68fa6e00979b853dfcd33214892912444e629d9afbba0289e3d8a66b",
            "161d4dfe7adffa4d3a99ce23264b806f1c1353850b45eb2d72e578f4c36f879f",
            "abf6a1cc528de1901b5fd74310711a0e708692b74cab7af2e9b46952e7733c62",
        }
    ),
    "requirement_optimize": frozenset(),
    "platform_example_render": frozenset(
        {
            "d3dcd2304d18ddfb1e36b23b5d846727b790e5cad07db7c3f93627fefdc5e60b",
            "6b60bc2f8ffe530b48ead870b71c83372d2fc66e215dd4979ebf467f7d0194c3",
            "35c7f110c4ff6f52afc3b792dd2c72062d1acabcba9919d11bc32e20384da3fe",
        }
    ),
    "data_description_parse": frozenset(),
}


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _next_version(session: Session, prompt_type: str) -> int:
    latest = session.exec(
        select(PromptTemplate)
        .where(PromptTemplate.type == prompt_type)
        .order_by(col(PromptTemplate.version).desc(), col(PromptTemplate.id).desc())
    ).first()
    return max(BUNDLED_PROMPT_VERSION, (latest.version + 1) if latest else 1)


def seed_default_prompts(session: Session) -> None:
    """Seed or safely upgrade bundled prompts without overriding custom actives."""
    for ptype in PROMPT_TYPES:
        path = DEFAULT_PROMPTS_DIR / f"{ptype}.md"
        content = path.read_text(encoding="utf-8")
        active_rows = session.exec(
            select(PromptTemplate).where(
                PromptTemplate.type == ptype,
                PromptTemplate.visibility == "system",
                PromptTemplate.is_active == True,  # noqa: E712
            ).order_by(col(PromptTemplate.id).desc())
        ).all()

        if any(row.content == content for row in active_rows):
            continue

        custom_active = [
            row
            for row in active_rows
            if not (
                row.name == f"default_{ptype}"
                and _content_hash(row.content) in _LEGACY_DEFAULT_HASHES[ptype]
            )
        ]
        if custom_active:
            # Multiple active templates are intentional.  A custom active
            # template must not be disabled or hidden by startup seeding.
            # Keep the refreshed bundled test-point contract available as an
            # inactive option so users can switch to it explicitly.
            if ptype == "test_points" and not session.exec(
                select(PromptTemplate).where(
                    PromptTemplate.type == ptype,
                    PromptTemplate.visibility == "system",
                    PromptTemplate.content == content,
                )
            ).first():
                session.add(
                    PromptTemplate(
                        name=f"default_{ptype}",
                        type=ptype,
                        content=content,
                        version=_next_version(session, ptype),
                        is_active=False,
                        owner_user_id=None,
                        visibility="system",
                    )
                )
            continue

        legacy_active = [
            row
            for row in active_rows
            if row.name == f"default_{ptype}"
            and _content_hash(row.content) in _LEGACY_DEFAULT_HASHES[ptype]
        ]
        if active_rows and not legacy_active:
            continue

        for row in legacy_active:
            row.is_active = False
            session.add(row)
        if legacy_active:
            # Deactivate the old default before inserting the replacement;
            # the partial unique index is checked per statement.
            session.flush()

        session.add(
            PromptTemplate(
                name=f"default_{ptype}",
                type=ptype,
                content=content,
                version=_next_version(session, ptype),
                is_active=True,
                owner_user_id=None,
                visibility="system",
            )
        )
    session.commit()
    normalize_single_active_prompts(session)


def normalize_single_active_prompts(session: Session) -> None:
    """Keep only the most recently updated active row for single-active types."""
    prompt_types = session.exec(select(PromptTemplate.type).distinct()).all()
    changed = False
    for prompt_type in prompt_types:
        if prompt_type in MULTI_ACTIVE_PROMPT_TYPES:
            continue
        active_rows = session.exec(
            select(PromptTemplate)
            .where(
                PromptTemplate.type == prompt_type,
                PromptTemplate.is_active == True,  # noqa: E712
            )
            .order_by(
                col(PromptTemplate.updated_at).desc(),
                col(PromptTemplate.id).desc(),
            )
        ).all()
        for row in active_rows[1:]:
            row.is_active = False
            session.add(row)
            changed = True
    if changed:
        session.commit()
