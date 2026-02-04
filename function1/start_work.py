import asyncio
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from sqlalchemy import select
from datetime import datetime

# Все импорты
from database.config import async_session
from database.models import (
    Keyword, PotentialPost, WorkerAccount, 
    TargetChannel, ReaderAccount, DiscoveryChannel, MonitoringPost
)

# Настройки
GROUP_TAG = 'A1' 
TARGET_GROUP = -1003723379200 
KEYWORDS_DATA, MY_WORKERS, CHANNELS_MAP = [], [], []
client = None 

# --- ФУНКЦИИ БАЗЫ ДАННЫХ ---

async def get_reader_from_db(group_tag):
    async with async_session() as session:
        result = await session.execute(select(ReaderAccount).where(ReaderAccount.group_tag == group_tag))
        return result.scalars().first()

async def load_all_data():
    """Загружает всё необходимое из БД за один раз"""
    async with async_session() as session:
        # 1. Ключевые слова с категориями
        kw_query = await session.execute(select(Keyword))
        keywords_data = {row.word.lower(): row.category for row in kw_query.scalars().all()}
        
        # 2. Наши воркеры (ID)
        wrk = await session.execute(select(WorkerAccount.tg_id).where(WorkerAccount.group_tag == GROUP_TAG))
        
        # 3. Каналы (Берем объекты целиком для проверки статуса)
        chn_query = await session.execute(select(TargetChannel).where(TargetChannel.group_tag == GROUP_TAG))
        db_channels = chn_query.scalars().all()
        
        # Словарь {id/username: статус}
        channels_map = {}
        for c in db_channels:
            key = c.username.lower().replace('@', '') if c.username else int(c.tg_id)
            channels_map[key] = c.status
            
        return keywords_data, wrk.scalars().all(), channels_map


async def save_discovery_channel(tg_id, username, reason):
    try:
        async with async_session() as session:
            new_disc = DiscoveryChannel(tg_id=tg_id, username=username, found_from_group=GROUP_TAG, reason=reason)
            session.add(new_disc)
            await session.commit()
    except Exception: pass

async def save_potential_post(storage_id, source_chat_id, source_msg_id, keyword, p_type, pub_date):
    async with async_session() as session:
        new_post = PotentialPost(
            group_tag=GROUP_TAG,
            storage_msg_id=storage_id,
            source_tg_id=source_chat_id,
            source_msg_id=source_msg_id,
            keyword_hit=keyword,
            post_type=p_type, # Новое поле
            published_at=pub_date,
            is_claimed=False
        )
        session.add(new_post)
        await session.commit()

async def save_discovery_channel(tg_id, reason):
    """Сохраняет найденный сторонний канал в резерв"""
    try:
        async with async_session() as session:
            new_disc = DiscoveryChannel(
                tg_id=tg_id,
                found_from_group=GROUP_TAG,
                reason=reason
            )
            session.add(new_disc)
            await session.commit()
    except Exception:
        pass # Игнорируем дубли

async def save_monitoring_post(channel_id, storage_id):
    """Сохраняет посты из каналов с активным конкурсом"""
    async with async_session() as session:
        new_mon = MonitoringPost(
            channel_id=channel_id,
            storage_msg_id=storage_id
        )
        session.add(new_mon)
        await session.commit()

# --- ЕДИНЫЙ ОБРАБОТЧИК ---

async def handler(event):
    global KEYWORDS_DATA, MY_WORKERS, CHANNELS_MAP
    
    current_chat_id = event.chat_id
    current_username = event.chat.username.lower() if event.chat.username else None
    # Определяем ключ для поиска в нашей базе
    chat_key = current_username if current_username in CHANNELS_MAP else current_chat_id

    # 1. ЛОГИКА РЕЗЕРВА (Discovery) - если это пересланный пост
    if event.message.fwd_from:
        fwd = event.message.fwd_from
        if fwd.from_id and hasattr(fwd.from_id, 'channel_id'):
            asyncio.create_task(save_discovery_channel(fwd.from_id.channel_id, "forward"))

    # 2. ПРОВЕРКА: НАШ ЛИ КАНАЛ?
    if chat_key not in CHANNELS_MAP:
        return 

    # 3. МОНИТОРИНГ АКТИВНЫХ (Контроль условий)
    if CHANNELS_MAP[chat_key] == "active_monitor":
        # Пересылаем всё подряд из этого канала для контекста оператору
        fwd = await event.message.forward_to(TARGET_GROUP)
        await save_monitoring_post(current_chat_id, fwd.id)

    # 4. ФИЛЬТРАЦИЯ НАХОДОК
    text = event.message.message or ""
    pub_date = event.message.date.replace(tzinfo=None)
    hit_keyword = None
    post_type = "keyword"

    # Сначала ищем слова категории "fast" (Первый) и обычные
    for word, category in KEYWORDS_DATA.items():
        if word in text.lower():
            hit_keyword = word
            post_type = "fast" if category == "fast" else "keyword"
            break
            
    # Если слов нет, проверяем наличие кнопок
    if not hit_keyword and event.message.reply_markup:
        hit_keyword = "AUTO: BUTTON_DETECTED"
        post_type = "button"

    # Если нашли цель — сохраняем
    if hit_keyword:
        try:
            fwd = await event.message.forward_to(TARGET_GROUP)
            await save_potential_post(
                storage_id=fwd.id, 
                source_chat_id=current_chat_id, 
                source_msg_id=event.message.id, 
                keyword=hit_keyword, 
                p_type=post_type,
                pub_date=pub_date
            )
            print(f"✅ [{post_type.upper()}] Найдено: {hit_keyword}")
        except Exception as e:
            print(f"❌ Ошибка сохранения: {e}")

# --- ЗАПУСК ---

async def main():
    global client, KEYWORDS_DATA, MY_WORKERS, CHANNELS_MAP
    try:
        print(f"📡 Инициализация группы {GROUP_TAG}...")
        
        # 1. Берем аккаунт из БД
        acc = await get_reader_from_db(GROUP_TAG)
        if not acc: 
            print(f"❌ Аккаунт не найден!")
            return

        # 2. Настройка клиента
        client = TelegramClient(
            StringSession(acc.session_string), 
            acc.api_id, 
            acc.api_hash,
            device_model=acc.device_model, 
            system_version=acc.os_version, 
            app_version=acc.app_version
        )
        await client.start()
        
        # 3. Загружаем данные (словари вместо списков)
        KEYWORDS_DATA, MY_WORKERS, CHANNELS_MAP = await load_all_data()
        
        # 4. Регистрация обработчика БЕЗ фильтра chats (проверка теперь внутри handler)
        client.add_event_handler(handler, events.NewMessage())
        
        print(f"🚀 Мониторинг запущен. Слов: {len(KEYWORDS_DATA)}, Каналов: {len(CHANNELS_MAP)}")
        await client.run_until_disconnected()
        
    except Exception as e: 
        print(f"‼️ Ошибка: {e}")


if __name__ == "__main__":
    asyncio.run(main())
