import asyncio
import re
import random
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityMentionName, MessageEntityMention
from sqlalchemy import select
from datetime import datetime


# Импорты из обновленной базы
from database.config import async_session
from database.models import (
    Keyword, PotentialPost, WorkerAccount, 
    TargetChannel, ReaderAccount, ContestPassport, LuckEvent
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

async def check_and_save_reserve(msg, source_id):
    """Улучшенная логика: достаем ссылку даже если её нет в тексте (Пункт 5)"""
    text_content = (msg.message or "").lower()
    has_button = msg.reply_markup is not None
    
    # 1. Сначала ищем ссылку в тексте через регулярку
    invite_links = re.findall(r"t.me/(?:\+|joinchat/|[\w_]+)", text_content)
    final_link = invite_links[0] if invite_links else None

    # 2. Если в тексте пусто, но это публичный канал — вытягиваем юзернейм из метаданных
    if not final_link and msg.fwd_from and msg.fwd_from.from_id:
        try:
            # Пытаемся получить инфо о канале из кэша или API
            entity = await client.get_entity(msg.fwd_from.from_id)
            if hasattr(entity, 'username') and entity.username:
                final_link = f"https://t.me{entity.username}"
        except Exception:
            pass # Если приватный или ошибка доступа — оставляем None

    hit = None
    for word in KEYWORDS_DATA.keys():
        if word in text_content:
            hit = word
            break
    if not hit and has_button:
        hit = "кнопка"

    if hit:
        async with async_session() as session:
            from database.models import TargetChannel, ReserveChannel
            exists = await session.execute(select(TargetChannel).where(TargetChannel.tg_id == source_id))
            if not exists.scalar():
                exists_res = await session.execute(select(ReserveChannel).where(ReserveChannel.tg_id == source_id))
                res_obj = exists_res.scalar()
                
                if not res_obj:
                    new_res = ReserveChannel(
                        tg_id=source_id, 
                        source_group_tag=GROUP_TAG, 
                        reason=hit,
                        username=final_link # Теперь тут будет либо ссылка из текста, либо юзернейм
                    )
                    session.add(new_res)
                    await session.commit()
                    print(f"📡 [РЕЗЕРВ] Сохранен ID: {source_id} | Ссылка: {final_link} | Повод: {hit}")


async def monitor_luck_emojis(chat_id, post_id):
    """Динамический анализ комментариев на 'Удачу' (Пункт 2)"""
    print(f"📊 [УДАЧА] Начало мониторинга поста {post_id}. Окно: 5 минут.")
    # Список текстовых эмодзи
    LUCK_TEXT_EMOJIS = ['🎰', '🏀', '🎯', '🎲', '🎳', '⚽️']
    
    start_time = datetime.now()
    timeout = 300 

    while (datetime.now() - start_time).total_seconds() < timeout:
        await asyncio.sleep(20) 
        
        unique_users = set()
        emoji_stats = {}
        found_any = 0
        
        try:
            async for msg in client.iter_messages(chat_id, reply_to=post_id, limit=200):
                found_any += 1
                hit_emoji = None
                
                # 1. Проверка на Dice (анимированные игровые кости/слоты)
                if msg.media and hasattr(msg.media, 'emoticon'):
                    if msg.media.emoticon in LUCK_TEXT_EMOJIS:
                        hit_emoji = msg.media.emoticon
                
                # 2. Проверка на Текст (включая если эмодзи внутри предложения)
                if not hit_emoji and msg.message:
                    for emo in LUCK_TEXT_EMOJIS:
                        if emo in msg.message: # Используем 'in', а не strip()
                            hit_emoji = emo
                            break

                # 3. Проверка на СТИКЕРЫ (если у стикера есть привязанный эмодзи удачи)
                if not hit_emoji and msg.sticker:
                    if msg.file.emoji in LUCK_TEXT_EMOJIS:
                        hit_emoji = msg.file.emoji

                if hit_emoji and msg.sender_id:
                    unique_users.add(msg.sender_id)
                    emoji_stats[hit_emoji] = emoji_stats.get(hit_emoji, 0) + 1

            print(f"🔍 [DEBUG] Пост {post_id}: Комментов {found_any}, Юзеров с удачей {len(unique_users)}, Всего эмодзи {sum(emoji_stats.values())}")

            if len(unique_users) >= 2 and sum(emoji_stats.values()) >= 5:
                top_emoji = max(emoji_stats, key=emoji_stats.get)
                print(f"🔥 [УДАЧА] ТРИГГЕР ПРОБИТ! Пост: {post_id}. Эмодзи: {top_emoji}")
                
                # --- НОВЫЙ БЛОК СОХРАНЕНИЯ В БД ---
                async with async_session() as session_luck:
                    new_event = LuckEvent(
                        chat_id=chat_id,
                        post_id=post_id,
                        emoji=top_emoji,
                        status="detected"
                    )
                    session_luck.add(new_event)
                    await session_luck.commit()
                print(f"💾 [БАЗА] Событие удачи для поста {post_id} сохранено в luck_events.")
                # ----------------------------------
                return 


        except Exception as e:
            print(f"⚠️ [УДАЧА] Ошибка анализа: {e}")
            break

    print(f"💤 [УДАЧА] Время вышло для поста {post_id}.")

# --- ОБРАБОТЧИК СООБЩЕНИЙ ---
async def handler(event):
    global KEYWORDS_DATA, MY_WORKERS, CHANNELS_MAP, client
    msg = event.message 
    current_chat_id = event.chat_id
    pub_date = msg.date.replace(tzinfo=None)

    # --- ПУНКТ 5: РЕЗЕРВНЫЕ КАНАЛЫ (РЕПОСТЫ) ---
    if msg.fwd_from:
        if hasattr(msg.fwd_from.from_id, 'channel_id'):
            asyncio.create_task(check_and_save_reserve(msg, msg.fwd_from.from_id.channel_id))
        return # Репосты не мониторим как основные посты

    # --- ПРОВЕРКА КАНАЛА ---
    if current_chat_id not in CHANNELS_MAP:
        return

        # --- ПУНКТ 1: ПОИСК УПОМИНАНИЯ ---
    if msg.entities:
        for ent in msg.entities:
            target_id = None
            if isinstance(ent, MessageEntityMentionName):
                target_id = ent.user_id
            elif isinstance(ent, MessageEntityMention):
                mention_text = msg.text[ent.offset + 1:ent.offset + ent.length]
                try:
                    user_entity = await client.get_entity(mention_text)
                    target_id = user_entity.id
                except: continue

            if target_id and target_id in MY_WORKERS:
                print(f"🎯 [МЕНШЕН] Наш воркер {target_id} упомянут в посте {msg.id}!")
                
                # --- ЗАПИСЬ В БАЗУ ДАННЫХ ---
                from database.models import MentionTask
                async with async_session() as session_ment:
                    new_task = MentionTask(
                        worker_tg_id=target_id,
                        channel_id=current_chat_id,
                        post_id=msg.id,
                        status="pending"
                    )
                    session_ment.add(new_task)
                    await session_ment.commit()
                print(f"💾 [БАЗА] Задача на ответ для воркера {target_id} создана в mention_tasks.")
                # ----------------------------

                if not (msg.replies and msg.replies.replies is not None):
                    print(f"⚠️ [ВНИМАНИЕ] Комментарии закрыты! Оператор, воркер не сможет ответить.")

    # --- ПУНКТ 2: ЗАПУСК МОНИТОРИНГА УДАЧИ ---
    asyncio.create_task(monitor_luck_emojis(current_chat_id, msg.id))

    # --- ТВОЯ ЛОГИКА (Блок 1 и 2) ---
    text = (msg.message or "").lower()

    # --- БЛОК 1: ЗЕРКАЛО (Для ленты в Текущих) ---
    if CHANNELS_MAP[current_chat_id] == "active_monitor":
        try:
            fwd_m = await msg.forward_to(MONITOR_STORAGE)
            await save_potential_post(
                storage_id=fwd_m.id, 
                source_chat_id=current_chat_id, 
                source_msg_id=msg.id, 
                keyword="MONITORING", 
                p_type="monitoring",
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

    if not hit_keyword and msg.reply_markup:
        hit_keyword = "AUTO: BUTTON_DETECTED"
        post_type = "button"

    if hit_keyword:
        try:
            fwd_t = await msg.forward_to(TARGET_GROUP)
            await save_potential_post(
                storage_id=fwd_t.id, 
                source_chat_id=current_chat_id, 
                source_msg_id=msg.id, 
                keyword=hit_keyword, 
                p_type=post_type,
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

# --- ПУНКТ 3: РУКИ (ОТПРАВКА ИСХОДЯЩИХ) ---
async def worker_outgoing_loop():
    """Фоновая проверка очереди сообщений"""
    print("🦾 [РУКИ] Модуль отправки запущен и слушает базу...")
    while True:
        await asyncio.sleep(10) # Проверяем базу каждые 10 секунд
        
        async with async_session() as session_out:
            from database.models import OutgoingMessage
            
            # Узнаем свой ID
            me = await client.get_me()
            
            # Ищем сообщения для НАС со статусом pending
            query = select(OutgoingMessage).where(
                OutgoingMessage.worker_tg_id == me.id,
                OutgoingMessage.status == "pending"
            )
            result = await session_out.execute(query)
            tasks = result.scalars().all()
            
            for task in tasks:
                try:
                    myself = await client.get_me()
                    
                    # --- ЛОГ ДЛЯ ТЕБЯ ---
                    print(f"DEBUG: Мой ID {myself.id} | ID в базе {task.receiver_id}")
                    # --------------------

                    # Самый надежный способ для теста на одном аккаунте: 
                    # если ID совпадает ИЛИ если мы ловим ошибку сущности при отправке себе
                    if str(task.receiver_id) == str(myself.id):
                        receiver = 'me'
                        print("📝 [РУКИ] Определен как 'САМ СЕБЕ'. Использую 'me'.")
                    else:
                        try:
                            receiver = await client.get_entity(int(task.receiver_id))
                        except:
                            receiver = await client.get_input_entity(int(task.receiver_id))

                    delay = random.randint(2, 5)
                    print(f"⏳ [РУКИ] Отправка для {task.receiver_id}...")
                    
                    await client.send_message(
                        receiver, 
                        task.text, 
                        reply_to=task.reply_to_msg_id
                    )
                    
                    task.status = "sent"
                    print(f"✅ [РУКИ] УСПЕХ! Сообщение доставлено.")
                    
                except Exception as e:
                    # Если всё равно ошибка сущности - пробуем отправить в 'me' как последний шанс
                    if "Could not find the input entity" in str(e):
                         print("🛠 [РУКИ] Попытка форсированной отправки в 'me'...")
                         await client.send_message('me', f"ФОРС-ОТПРАВКА: {task.text}")
                         task.status = "sent"
                    else:
                        print(f"❌ [РУКИ] Критическая ошибка: {e}")
                        task.status = "error"
                
                await session_out.commit()


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
    client.add_event_handler(incoming_private_handler, events.NewMessage(incoming=True, func=lambda e: e.is_private))

    
    # 4. Регистрация обработчика и запуск фонового обновления
    client.add_event_handler(handler, events.NewMessage())
    asyncio.create_task(data_refresher())
    
    print(f"🚀 Система онлайн. Слов: {len(KEYWORDS_DATA)}, Каналов: {len(CHANNELS_MAP)}")
        # Запускаем "руки" в фоновом режиме
    asyncio.create_task(worker_outgoing_loop())
    await client.run_until_disconnected()

# --- ПУНКТ 3: ЗЕРКАЛО ЛС (ПРИЕМ СООБЩЕНИЙ) ---
async def incoming_private_handler(event):
    sender = await event.get_sender()
    if sender.bot: return 

    msg_obj = event.message
    m_type = "text"
    s_media_id = None
    
    # Если есть медиа — пересылаем в MONITOR_STORAGE
    if msg_obj.photo or msg_obj.voice or msg_obj.video or msg_obj.document:
        try:
            # Пересылаем в твое хранилище (из config.py)
            fwd = await msg_obj.forward_to(MONITOR_STORAGE)
            s_media_id = fwd.id
            m_type = "photo" if msg_obj.photo else "media" # Упростим для примера
        except Exception as e:
            print(f"❌ Ошибка зеркала медиа: {e}")

    me = await client.get_me()
    async with async_session() as session_msg:
        from database.models import AccountMessage
        new_msg = AccountMessage(
            msg_id=msg_obj.id,
            worker_tg_id=me.id,
            sender_id=event.sender_id,
            text=msg_obj.message or f"[{m_type.upper()}]",
            media_type=m_type,
            storage_media_id=s_media_id, # Тот самый ID из хранилища
            is_read=False
        )
        session_msg.add(new_msg)
        await session_msg.commit()
    print(f"📩 [ЛС] Сообщение (тип: {m_type}) сохранено.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Мониторинг остановлен пользователем.")
