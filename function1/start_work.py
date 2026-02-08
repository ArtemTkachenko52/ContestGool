import asyncio
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from sqlalchemy import select
from datetime import datetime

# Импорты из обновленной базы
from database.config import async_session
from database.models import (
    Keyword, PotentialPost, WorkerAccount, 
    TargetChannel, ReaderAccount, ContestPassport
)

# Настройки группы (тарелки)
GROUP_TAG = 'A1' 
TARGET_GROUP = -1003723379200 
MONITOR_STORAGE = -1003753624654

# Глобальные кэши данных
KEYWORDS_DATA = {}
MY_WORKERS = []
CHANNELS_MAP = {}
client = None 

# --- ФУНКЦИИ БАЗЫ ДАННЫХ ---

async def load_all_data():
    """Загружает всё необходимое из БД для работы мониторинга"""
    async with async_session() as session:
        # 1. Ключевые слова
        kw_query = await session.execute(select(Keyword))
        keywords = {row.word.lower(): row.category for row in kw_query.scalars().all()}
        
        # 2. Список воркеров группы
        wrk = await session.execute(select(WorkerAccount.tg_id).where(WorkerAccount.group_tag == GROUP_TAG))
        
        # 3. Каналы для мониторинга
        chn_query = await session.execute(select(TargetChannel).where(TargetChannel.group_tag == GROUP_TAG))
        db_channels = chn_query.scalars().all()
        
        channels_map = {}
        for c in db_channels:
            # Приоритизируем ID, так как Username может меняться
            key = c.tg_id if c.tg_id else c.username.lower().replace('@', '')
            channels_map[key] = c.status
            
        return keywords, wrk.scalars().all(), channels_map

async def get_reader_from_db(group_tag):
    async with async_session() as session:
        result = await session.execute(select(ReaderAccount).where(ReaderAccount.group_tag == group_tag))
        return result.scalars().first()

async def save_potential_post(storage_id, source_chat_id, source_msg_id, keyword, p_type, pub_date):
    """Сохраняет найденный пост-кандидат на конкурс"""
    async with async_session() as session:
        new_post = PotentialPost(
            group_tag=GROUP_TAG,
            storage_msg_id=storage_id,
            source_tg_id=source_chat_id,
            source_msg_id=source_msg_id,
            keyword_hit=keyword,
            post_type=p_type,
            published_at=pub_date,
            is_claimed=False
        )
        session.add(new_post)
        await session.commit()

# --- ОБРАБОТЧИК СООБЩЕНИЙ ---
async def handler(event):
    global KEYWORDS_DATA, MY_WORKERS, CHANNELS_MAP, client
    current_chat_id = event.chat_id
    if event.message.fwd_from or current_chat_id not in CHANNELS_MAP:
        return 

    text = (event.message.message or "").lower()
    pub_date = event.message.date.replace(tzinfo=None)
 
    # --- БЛОК 1: ЗЕРКАЛО (Для ленты в Текущих) ---
    if CHANNELS_MAP[current_chat_id] == "active_monitor":
        try:
            fwd_m = await event.message.forward_to(MONITOR_STORAGE)
            await save_potential_post(
                storage_id=fwd_m.id, 
                source_chat_id=current_chat_id, 
                source_msg_id=event.message.id, 
                keyword="MONITORING", 
                p_type="monitoring", # <--- ВАЖНО
                pub_date=pub_date
            )
        except Exception as e:
            print(f"❌ Ошибка зеркала: {e}")

    # --- БЛОК 2: ФИЛЬТР (Для кнопки "Получить новый пост") ---
    hit_keyword = None
    post_type = "keyword"

    for word, category in KEYWORDS_DATA.items():
        if word in text:
            hit_keyword = word
            post_type = "fast" if category == "fast" else "keyword"
            break
            
    if not hit_keyword and event.message.reply_markup:
        hit_keyword = "AUTO: BUTTON_DETECTED"
        post_type = "button"

    if hit_keyword:
        try:
            fwd_t = await event.message.forward_to(TARGET_GROUP)
            await save_potential_post(
                storage_id=fwd_t.id, 
                source_chat_id=current_chat_id, 
                source_msg_id=event.message.id, 
                keyword=hit_keyword, 
                p_type=post_type, # <--- keyword / fast / button
                pub_date=pub_date
            )
            print(f"🔥 Найдена цель: {hit_keyword}")
        except Exception as e:
            print(f"❌ Ошибка сохранения цели: {e}")

# --- ЦИКЛ ОБНОВЛЕНИЯ ДАННЫХ ---

async def data_refresher():
    """Фоновая задача для частого обновления данных из БД"""
    global KEYWORDS_DATA, MY_WORKERS, CHANNELS_MAP
    while True:
        try:
            # Обновляем кэш каналов и ключей
            KEYWORDS_DATA, MY_WORKERS, CHANNELS_MAP = await load_all_data()
            # Можно оставить принт для тестов, потом закомментируешь
            # print("🔄 Данные синхронизированы") 
        except Exception as e:
            print(f"⚠️ Ошибка обновления данных: {e}")
        
        # Ставим 10-15 секунд вместо 300 (5 минут)
        await asyncio.sleep(5) 

# --- ЗАПУСК ---

async def main():
    global client, KEYWORDS_DATA, MY_WORKERS, CHANNELS_MAP
    
    print(f"📡 Запуск мониторинга группы {GROUP_TAG}...")
    
    # 1. Получаем аккаунт читателя
    acc = await get_reader_from_db(GROUP_TAG)
    if not acc: 
        print(f"❌ Читатель для группы {GROUP_TAG} не найден в БД!")
        return

       # 2. Инициализация Telethon с уникальными данными из БД
    client = TelegramClient(
        StringSession(acc.session_string), 
        acc.api_id, 
        acc.api_hash,
        device_model=acc.device_model,
        system_version=acc.os_version, # Поле из обновленной БД
        app_version=acc.app_version     # Поле из обновленной БД
    )


    
    await client.start()
    
    # 3. Первичная загрузка данных
    KEYWORDS_DATA, MY_WORKERS, CHANNELS_MAP = await load_all_data()
    
    # 4. Регистрация обработчика и запуск фонового обновления
    client.add_event_handler(handler, events.NewMessage())
    asyncio.create_task(data_refresher())
    
    print(f"🚀 Система онлайн. Слов: {len(KEYWORDS_DATA)}, Каналов: {len(CHANNELS_MAP)}")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Мониторинг остановлен пользователем.")
