import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

# 1. Проверяем переменную окружения из docker-compose
# 2. Если её нет (запуск локально), используем localhost
DATABASE_URL = os.getenv(
    "DATABASE_URL", 
    "postgresql+asyncpg://admin:password123@localhost:5432/contest_monitor"
)

# Для Docker заменяем localhost на db автоматически, если мы внутри сети
if os.path.exists('/.dockerenv') and "localhost" in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("localhost", "db")

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)
