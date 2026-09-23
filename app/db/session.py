"""Engine and session management.

The engine is created lazily and cached, so importing this module never opens a
connection - which keeps test collection and Alembic imports cheap.
"""

from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


@lru_cache
def get_engine(url: str | None = None) -> Engine:
    settings = get_settings()
    return create_engine(
        url or settings.sqlalchemy_url,
        echo=settings.sql_echo,
        pool_pre_ping=True,  # transparently replaces connections dropped by the server
        future=True,
    )


@lru_cache
def get_sessionmaker(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(
        bind=get_engine(url),
        autoflush=False,
        expire_on_commit=False,  # objects stay usable after commit
        future=True,
    )


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    """Transactional scope: commits on success, rolls back on any exception.

    Callers that manage their own transaction should use `get_sessionmaker()`
    directly instead.
    """
    session = get_sessionmaker(url)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
