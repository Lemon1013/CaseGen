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
    "wiki_analyze",
    "wiki_write",
)

DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "default_prompts"

# Bump this only when the bundled prompt contract changes. Recognized v1 hashes
# let existing installations upgrade without replacing a user-created prompt.
BUNDLED_PROMPT_VERSION = 6
_LEGACY_DEFAULT_HASHES: dict[str, frozenset[str]] = {
    "generate": frozenset(
        {
            "06e195415664f40e05100de9058f6e43858286771ad687dbe9154df6e3dd7f6f",
            "167b82bf890c61ada53f09f2ee922d5288956afe605e79c156307b5b9f1ba3c0",
        }
    ),
    "review": frozenset(
        {
            "3081b648c1625d6c9645f2fd9648c49cff262dbeb3928d60c7418185e13ab473",
            "d0880132a4281dbfc3f2b8e5b0e9e25fac0f9ccbf2a367175f926ba5498fb74b",
        }
    ),
    "test_points": frozenset(),
    "optimize": frozenset(
        {
            "ac8acfdc68fa6e00979b853dfcd33214892912444e629d9afbba0289e3d8a66b",
            "161d4dfe7adffa4d3a99ce23264b806f1c1353850b45eb2d72e578f4c36f879f",
        }
    ),
    "requirement_optimize": frozenset(),
    "wiki_analyze": frozenset(
        {
            "7db70a093e6d4a15be629f8c4c5e75cc6d3cefb925b76a3daec0c7adb5bab288",
            "4af93ae3ee527524b3bbc27d8e6d7ce9669a34e8e555d30028e60c7323f998bf",
            "8aefbcb5b8959326387c78335d6318d36f4e3acfc30f53a92c82266ed55a9397",
            "5fbdba77c3d43afb47cb99d0c01329acad5b42df5409faf9f884fbd1cfea60f8",
            "1c38aac6841de2a97220a358d8f5da4c1a1faef1be7b37a769ce3f9b46d753b9",
            "6581ab52f86efbca195c611fc76eab99eb90148a3c2465215618f3d0b00c9961",
            "cc08ca177d6a7191cd31bb41271562088e413b296a2e8231837316c32f53d731",
        }
    ),
    "wiki_write": frozenset(
        {
            "0966049ceb104bc68a17dc86b642b3c1d2675617f7149bdccd1936c5aad4e389",
            "e96d1f69912b41a529c2eddbebf0918c43779e39cc4ac0238986a9feec151c2e",
            "420ebfb6d629549baf52339af9b1d3856d8a123d6491ebcd7ddcd3574fde6ab1",
            "a4efa9887fbf78d0df936f95a9b5c569f9b42de3e6c144940bac29806abb62ea",
            "2d270e67722abd4e2d8de9f521e02980e9f621fc2af3db197da045de8c52d89e",
            "5a80006f3b6929cc98f8d70ffc2255cc53d3a6ebd43266631b8fe15d50a1017f",
            "b94d9bb0b10ebbb755e20161149ab5e258bfb9d7a77645358c488b135ea8c437",
        }
    ),
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

        session.add(
            PromptTemplate(
                name=f"default_{ptype}",
                type=ptype,
                content=content,
                version=_next_version(session, ptype),
                is_active=True,
            )
        )
    session.commit()
