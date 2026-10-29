from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.engine import create_engine, create_session_factory
from app.db.migrate import upgrade_to_head


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture
def migrated_settings(settings: Settings) -> Settings:
    settings.data_dir.mkdir(parents=True)
    upgrade_to_head(settings)
    return settings


@pytest.fixture
async def engine(migrated_settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(migrated_settings.db_path)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    async with factory() as session:
        yield session
