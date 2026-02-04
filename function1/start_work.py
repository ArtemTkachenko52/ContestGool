import asyncio
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from sqlalchemy import select
from datetime import datetime

# Все импорты
from database.config import async_session
from database.models import Keyword, PotentialPost, WorkerAccount, TargetChannel, ReaderAccount, DiscoveryChannel

# Настройки
GROUP_TAG = 'A1' 
TARGET_GROUP = -1003723379200 
KEYWORDS, MY_WORKERS, CHANNELS = [], [], []
client = None 

# --- ФУНКЦИИ БАЗЫ ДАННЫХ ---

async def get_reader_from_db(group_tag):
    async with async_session() as session:
        result = await session.execute(select(ReaderAccount).where(ReaderAccount.group_tag == group_tag))
        return result.scalars().first()

async def load_all_data():
    async with async_session() as session:
        kw = await session.execute(select(Keyword.word).where(Keyword.is_active == True))
        wrk = await session.execute(select(WorkerAccount.tg_id).where(WorkerAccount.group_tag == GROUP_TAG))
        chn_query = await session.execute(select(TargetChannel).where(TargetChannel.group_tag == GROUP_TAG))
        db_channels = chn_query.scalars().all()
        
        final_channels = []
        for c in db_channels:
            if c.username: final_channels.append(c.username.lower().replace('@', ''))
            if c.tg_id: final_channels.append(int(c.tg_id))
        return kw.scalars().all(), wrk.scalars().all(), final_channels

async def save_discovery_channel(tg_id, username, reason):
    try:
        async with async_session() as session:
            new_disc = DiscoveryChannel(tg_id=tg_id, username=username, found_from_group=GROUP_TAG, reason=reason)
            session.add(new_disc)
            await session.commit()
    except Exception: pass

async def save_potential_post(storage_id, source_chat_id, source_msg_id, keyword, pub_date):
    async with async_session() as session:
        new_post = PotentialPost(
            group_tag=GROUP_TAG, storage_msg_id=storage_id,
            source_tg_id=source_chat_id, source_msg_id=source_msg_id,
            keyword_hit=keyword, published_at=pub_date, is_claimed=False
        )
        session.add(new_post)
        await session.commit()

# --- ЕДИНЫЙ ОБРАБОТЧИК ---

async def handler(event):
    global KEYWORDS, MY_WORKERS, CHANNELS
    
    current_chat_id = event.chat_id
    current_username = event.chat.username.lower() if event.chat.username else None
    
    # Отлов резервных каналов (если это пересланный пост)
    if event.message.fwd_from:
        fwd = event.message.fwd_from
        if fwd.from_id and hasattr(fwd.from_id, 'channel_id'):
            asyncio.create_task(save_discovery_channel(
                tg_id=fwd.from_id.channel_id, 
                username=current_username, 
                reason='forward'
            ))

    # Проверка: наш ли это канал?
    if not (current_chat_id in CHANNELS or (current_username and current_username in CHANNELS)):
        return 

    print(f"📩 [DEBUG] Целевое сообщение из @{current_username}: {event.text[:30]}")

    text = event.message.message or ""
    pub_date = event.message.date.replace(tzinfo=None)
    hit_keyword = None

    # Поиск ключевых слов
    for kw in KEYWORDS:
        if kw.lower() in text.lower():
            hit_keyword = kw
            break
            
    # Поиск по кнопкам
    if not hit_keyword and event.message.reply_markup:
        hit_keyword = "AUTO: BUTTON_DETECTED"

    # Поиск по воркерам
    if not hit_keyword and event.message.entities:
        for ent in event.message.entities:
            u_id = getattr(ent, 'user_id', None)
            if u_id and u_id in MY_WORKERS:
                hit_keyword = f"WINNER_FOUND: {u_id}"
                break

    if hit_keyword:
        try:
            fwd = await event.message.forward_to(TARGET_GROUP)
            await save_potential_post(fwd.id, current_chat_id, event.message.id, hit_keyword, pub_date)
            print(f"✅ [{datetime.now().strftime('%H:%M:%S')}] ПОСТ СОХРАНЕН!")
        except Exception as e:
            print(f"❌ Ошибка сохранения: {e}")

# --- ЗАПУСК ---

async def main():
    global client, KEYWORDS, MY_WORKERS, CHANNELS
    try:
        print(f"📡 Инициализация группы {GROUP_TAG}...")
        acc = await get_reader_from_db(GROUP_TAG)
        if not acc: return print(f"❌ Аккаунт не найден!")

        client = TelegramClient(StringSession(acc.session_string), acc.api_id, acc.api_hash,
                                device_model=acc.device_model, system_version=acc.os_version, app_version=acc.app_version)
        await client.start()
        KEYWORDS, MY_WORKERS, CHANNELS = await load_all_data()
        client.add_event_handler(handler, events.NewMessage())
        print(f"🚀 Мониторинг запущен. Слов: {len(KEYWORDS)}, Каналов: {len(CHANNELS)}")
        await client.run_until_disconnected()
    except Exception as e: print(f"‼️ Ошибка: {e}")

if __name__ == "__main__":
    asyncio.run(main())
