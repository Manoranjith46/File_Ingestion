"""Database helpers for the FastAPI application."""

from functools import lru_cache
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from helpers.get_env import get_env


@lru_cache(maxsize=1)
def get_engine():
    """
        Create and cache the SQLAlchemy engine.
        it returns the engine from cache instead of creating a new one each time it's called.
        This is useful for performance optimization, especially in applications that make frequent database queries.
    """
    return create_engine(get_env("Connection_String", required=True), pool_pre_ping=True)


@lru_cache(maxsize=1)
def get_session_local():
    """
        Create and cache the SQLAlchemy session factory.
        it returns the session factory from cache instead of creating a new one each time it's called.
    """
    return sessionmaker(autocommit=False, autoflush=False, bind=get_engine())


def get_db() -> Generator[Session, None, None]:
    """
        Yield a database session for request-scoped dependencies.
        This function is used as a dependency in FastAPI routes to provide a database session for each request.
        It ensures that the session is properly closed after the request is processed, preventing resource leaks.
    """
    db = get_session_local()()
    try:
        yield db
    finally:
        db.close()


def Check_db_Connection():
    """
        Connect to the database and verify the configured connection string.
    """
    try:
        engine = get_engine()
        with engine.connect():
            print("✅ Database Connected Successfully")
    except Exception as e:
        print(f"❌ Failed to Connect to Database: {e}")
        return None
