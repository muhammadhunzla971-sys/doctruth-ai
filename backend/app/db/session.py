from collections.abc import AsyncIterator

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

_maker: async_sessionmaker[AsyncSession] | None = None


def make_engine(url: str) -> AsyncEngine:
    """Serverless-friendly engine: no pooling, no prepared statements (Supabase pooler)."""
    if url.startswith("postgresql"):
        return create_async_engine(
            url,
            poolclass=NullPool,
            connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0},
        )
    return create_async_engine(url)


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _maker
    if _maker is None:
        engine = make_engine(get_settings().async_database_url)
        _maker = async_sessionmaker(engine, expire_on_commit=False)
    return _maker


async def get_session(
    maker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> AsyncIterator[AsyncSession]:
    async with maker() as session:
        yield session
