from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

# Адрес твоего Postgres в Docker
DATABASE_URL = "postgresql+asyncpg://admin:password123@localhost:5432/contest_monitor"

engine = create_async_engine(DATABASE_URL, echo=False)

# Фабрика сессий (чтобы микросервисы могли открывать "окно" в базу)
async_session = async_sessionmaker(engine, expire_on_commit=False)
