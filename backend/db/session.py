from collections.abc import AsyncGenerator
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from config import Settings


def create_async_engine_from_settings(settings: Settings) -> AsyncEngine:
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not configured")

    return create_async_engine(
        settings.database_url,
        echo=settings.database_echo_sql,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_pre_ping=True,
        pool_recycle=settings.database_pool_recycle_seconds,
    )


@dataclass
class _DatabaseHolder:
    engine: AsyncEngine | None = None
    session_factory: async_sessionmaker[AsyncSession] | None = None


_holder = _DatabaseHolder()


def init_database_engine(settings: Settings) -> None:
    """Build engine and session factory once (call from app lifespan)."""
    if not settings.database_url:
        return

    if _holder.engine is not None:
        return

    _holder.engine = create_async_engine_from_settings(settings)
    _holder.session_factory = async_sessionmaker(
        _holder.engine,
        expire_on_commit=False,
        autoflush=False,
    )


async def close_database_engine() -> None:
    if _holder.engine is not None:
        await _holder.engine.dispose()
    _holder.engine = None
    _holder.session_factory = None


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: one transaction per request (commit on success)."""
    if _holder.session_factory is None:
        raise RuntimeError("Database is not initialized; set DATABASE_URL and ensure startup completed.")

    async with _holder.session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_engine() -> AsyncEngine | None:
    return _holder.engine


def has_session_factory() -> bool:
    return _holder.session_factory is not None


def get_async_session_maker() -> async_sessionmaker[AsyncSession] | None:
    """Return the app-wide session factory, or None if the database is not configured."""
    return _holder.session_factory
