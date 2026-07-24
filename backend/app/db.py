from sqlmodel import SQLModel, Session, create_engine
from app import config
from app.config import ensure_data_dirs

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        ensure_data_dirs()
        _engine = create_engine(
            f"sqlite:///{config.DB_PATH}",
            connect_args={"check_same_thread": False},
        )
    return _engine


def init_db() -> None:
    from app.models import entities  # noqa: F401

    SQLModel.metadata.create_all(get_engine())


def get_session():
    with Session(get_engine()) as session:
        yield session


def reset_engine() -> None:
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None
