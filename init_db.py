import asyncio
from sqlalchemy import text
from database.config import engine
from database.models import Base

async def init():
    async with engine.begin() as conn:
        # Создаем все нужные схемы
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS watcher"))
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS management"))
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS workers"))
        
        # Создаем таблицы во всех схемах
        await conn.run_sync(Base.metadata.create_all)
    print("✅ Все схемы и таблицы успешно созданы!")

if __name__ == "__main__":
    asyncio.run(init())
