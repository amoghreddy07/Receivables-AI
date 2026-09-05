"""Engine + session helpers.

The app uses SQLite for development/evaluation and is SQLAlchemy-2 compatible
with Postgres for production. Tests/eval pass their own in-memory engines so
runs are fully isolated and reproducible.
"""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models import Base


def _configure_sqlite(engine) -> None:
    @event.listens_for(engine, "connect")
    def _fk_pragma(dbapi_conn, _record):  # pragma: no cover - trivial
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def make_engine(url: str | None = None, *, echo: bool = False):
    url = url or settings.db_url
    engine = create_engine(url, echo=echo, future=True)
    if url.startswith("sqlite"):
        _configure_sqlite(engine)
    return engine


def init_db(engine=None) -> None:
    engine = engine or make_engine()
    Base.metadata.create_all(engine)


def make_session(engine=None) -> Session:
    engine = engine or make_engine()
    return Session(engine, expire_on_commit=False)


_session_factory = None


def get_session_factory(engine=None):
    global _session_factory
    if engine is not None:
        return sessionmaker(bind=engine, expire_on_commit=False)
    if _session_factory is None:
        _session_factory = sessionmaker(bind=make_engine(), expire_on_commit=False)
    return _session_factory


# FastAPI dependency
def get_db():
    db = make_session()
    try:
        yield db
    finally:
        db.close()
