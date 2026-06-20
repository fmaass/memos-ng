from collections.abc import AsyncIterator

from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

# Connection strategy splits by environment:
#  - PROD: a BOUNDED pool. memos-ng shares Postgres17 with production; the old unbounded NullPool
#    let a burst of concurrent requests (a feed page fires many asset requests at once, each
#    holding its connection for the file transfer) exhaust the shared connection slots
#    (TooManyConnectionsError), which also starved prod. Cap at pool_size + max_overflow; pre_ping
#    discards stale connections; recycle avoids long-lived ones.
#  - DEV/TEST: NullPool (fresh connection per checkout). asyncpg connections are event-loop-bound
#    and pytest-asyncio uses a per-test loop — a pooled connection would cross loops and error.
if settings.is_production:
    engine = create_async_engine(
        settings.DATABASE_URL,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_recycle=settings.DB_POOL_RECYCLE,
        pool_pre_ping=True,
        future=True,
    )
else:
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool, future=True)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session() as session:
        yield session
