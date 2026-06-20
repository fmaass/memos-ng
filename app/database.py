from collections.abc import AsyncIterator

from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

# NullPool: a fresh asyncpg connection per checkout. At family scale against a local
# Postgres the overhead is negligible, and it avoids cross-event-loop connection reuse
# (asyncpg connections are loop-bound — pooling them breaks under per-request loops).
engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool, future=True)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session() as session:
        yield session
