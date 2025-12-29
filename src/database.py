"""
Gestion de la base de donnees SQLite.
"""

from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session

from .models import Base
from .config import get_config, AppConfig


_engine = None
_SessionLocal = None


def get_engine(config: Optional[AppConfig] = None):
    """Retourne le moteur SQLAlchemy (singleton)."""
    global _engine

    if _engine is None:
        if config is None:
            config = get_config()

        db_path = config.database.db_path

        # Creer le repertoire si necessaire
        db_path.parent.mkdir(parents=True, exist_ok=True)

        # Creer le moteur SQLite avec timeout pour eviter "database is locked"
        _engine = create_engine(
            f"sqlite:///{db_path}",
            echo=config.database.echo_sql,
            connect_args={
                "check_same_thread": False,  # Pour multi-thread
                "timeout": 30,  # 30 secondes timeout pour lock
            },
        )

        # Configurer SQLite pour meilleure concurrence
        @event.listens_for(_engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")  # Write-Ahead Logging
            cursor.execute("PRAGMA busy_timeout=30000")  # 30 sec timeout
            cursor.close()

    return _engine


def get_session_factory(config: Optional[AppConfig] = None) -> sessionmaker:
    """Retourne la factory de sessions."""
    global _SessionLocal

    if _SessionLocal is None:
        engine = get_engine(config)
        _SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    return _SessionLocal


def init_db(config: Optional[AppConfig] = None) -> None:
    """Initialise la base de donnees (cree les tables)."""
    engine = get_engine(config)
    Base.metadata.create_all(bind=engine)


def drop_db(config: Optional[AppConfig] = None) -> None:
    """Supprime toutes les tables (DANGER)."""
    engine = get_engine(config)
    Base.metadata.drop_all(bind=engine)


def reset_db(config: Optional[AppConfig] = None) -> None:
    """Reset la base (drop + create)."""
    drop_db(config)
    init_db(config)


@contextmanager
def get_session(config: Optional[AppConfig] = None) -> Generator[Session, None, None]:
    """Context manager pour obtenir une session."""
    SessionLocal = get_session_factory(config)
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db_session(config: Optional[AppConfig] = None) -> Session:
    """Retourne une nouvelle session (a fermer manuellement)."""
    SessionLocal = get_session_factory(config)
    return SessionLocal()


# Reset singleton pour les tests
def reset_engine() -> None:
    """Reset le singleton engine (pour tests)."""
    global _engine, _SessionLocal
    if _engine:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
