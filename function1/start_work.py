import asyncio
import random
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import MessageEntityMentionName, MessageEntityMention, ReactionEmoji
from sqlalchemy import select, func, text, update  # <-- ДОБАВИЛИ update
from datetime import datetime
from telethon import functions, types
import re
import ddddocr
import io
from PIL import Image, ImageOps, ImageEnhance

# Импорты из обновленной базы
from database.config import async_session
from database.models import (
    Keyword, PotentialPost, WorkerAccount, 
    TargetChannel, ReaderAccount, ContestPassport, 
    LuckEvent, OutgoingMessage, StarReport  # <-- ДОБАВИЛИ StarReport
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

# --- ПУНКТ 1: СПИСОК ФРАЗ ДЛЯ БЫСТРОГО КОММЕНТА ---
FAST_PHRASES = ["+", ".", "!", "участвую", "тут", "готов", "я", "участвую!", "админ красава"]

async def execute_fast_comment(chat_id, post_id):
    """
    Выбирает 1 случайного воркера из БД и отправляет быстрый комментарий.
    Вызывается мгновенно, если найден ключ категории 'fast'.
    """
    async with async_session() as session:
        # 1. Ищем 1 живого воркера именно из нашей группы (GROUP_TAG)
        res = await session.execute(
            select(WorkerAccount).where(
                WorkerAccount.group_tag == GROUP_TAG,
                WorkerAccount.is_alive == True
            ).order_by(func.random()).limit(1)
        )
        worker = res.scalar()

        if not worker:
            print(f"⚠️ [FAST] Нет живых воркеров в группе {GROUP_TAG} для быстрого ответа.")
            return

        # 2. Создаем временное соединение от лица воркера
        # Используем StringSession и данные железа из БД для мимикрии
        w_client = TelegramClient(
            StringSession(worker.session_string), 
            worker.api_id, 
            worker.api_hash,
            device_model=worker.device_model,
            system_version=worker.os_version,
            app_version=worker.app_version
        )
        
        try:
            await w_client.connect()
            # 3. Отправляем сообщение именно как комментарий к посту (comment_to)
            await w_client.send_message(
                chat_id, 
                random.choice(FAST_PHRASES), 
                comment_to=post_id
            )
            print(f"⚡️ [FAST] Воркер {worker.tg_id} успешно отписал первым в пост {post_id}")
        except Exception as e:
            print(f"❌ [FAST] Ошибка при отправке от воркера {worker.tg_id}: {e}")
        finally:
            await w_client.disconnect()
            
async def execute_button_click_raid(chat_id, post_id, msg_obj):
    """
    Пункт 2: Нажатие кнопки 5-10 воркерами с рандомной задержкой до 60с.
    """
    # 1. Выбираем случайное количество участников (от 5 до 10)
    count = random.randint(5, 10)
    
    async with async_session() as session:
        # 2. Берем случайных живых воркеров именно нашей группы
        res = await session.execute(
            select(WorkerAccount).where(
                WorkerAccount.group_tag == GROUP_TAG,
                WorkerAccount.is_alive == True
            ).order_by(func.random()).limit(count)
        )
        workers = res.scalars().all()

    if not workers:
        print(f"⚠️ [BUTTON] Нет доступных воркеров для нажатия кнопки в посте {post_id}")
        return

    print(f"🔘 [BUTTON] Запуск рейда на кнопку для поста {post_id}. Участников: {len(workers)}")

    # 3. Для каждого воркера запускаем задачу с индивидуальной задержкой (имитация реальных людей)
    for worker in workers:
        delay = random.randint(5, 55) # Разброс в течение минуты
        asyncio.create_task(single_button_click(worker, chat_id, post_id, msg_obj, delay))
        

async def single_button_click(worker, chat_id, post_id, msg_obj, delay):
    """Логика: вступление + нажатие + бесплатное распознавание ddddocr (исправлено)"""
    await asyncio.sleep(delay)
    
    w_client = TelegramClient(
        StringSession(worker.session_string), 
        worker.api_id, worker.api_hash,
        device_model=worker.device_model,
        system_version=worker.os_version,
        app_version=worker.app_version
    )
    
    try:
        await w_client.connect()
        
        # 1. Анализируем структуру кнопок
        button = None
        if msg_obj.reply_markup and msg_obj.reply_markup.rows:
            # Берем первую кнопку из первого ряда (индексы [0].buttons[0])
            button = msg_obj.reply_markup.rows[0].buttons[0]
        
        if not button:
            print(f"⚠️ [BUTTON] Кнопки в посте {post_id} не найдены.")
            return

        url = getattr(button, 'url', None)
        
        if url and "t.me/" in url:
            bot_match = re.search(r"t.me/([\w_]+)\?start=([\w-]+)", url)
            if bot_match:
                bot_username = bot_match.group(1)
                start_param = bot_match.group(2)
                
                from telethon.tl.functions.messages import StartBotRequest
                await w_client(StartBotRequest(bot=bot_username, peer=bot_username, start_param=start_param))
                print(f"🤖 [DDDD] Воркер {worker.tg_id} зашел в @{bot_username}")

                # Ожидаем капчу
                await asyncio.sleep(5) 
                async for message in w_client.iter_messages(bot_username, limit=1):
                    if message.photo:
                        print(f"🖼 [DDDD] Фото получено, распознаю...")
                        photo_bytes = await w_client.download_media(message.photo, file=bytes)
                        
                        ocr = ddddocr.DdddOcr(show_ad=False)
                        captcha_text = ocr.classification(photo_bytes)
                        captcha_digits = "".join(filter(str.isdigit, captcha_text))
                        
                        if captcha_digits:
                            print(f"🔢 [DDDD] Распознано: {captcha_digits}. Отправляю...")
                            await w_client.send_message(bot_username, captcha_digits)
                        else:
                            print(f"❌ [DDDD] Не удалось выделить цифры в тексте: {captcha_text}")
            
            # Если это Mini App (Randomize)
            elif "startapp=" in url:
                # (Тут остается твоя логика Mini App из прошлых шагов)
                pass 
        else:
            # Обычный клик (Callback)
            await msg_obj.click(0)
            print(f"✅ [BUTTON] Callback-кнопка нажата.")
            
    except Exception as e:
        print(f"❌ [BUTTON] Ошибка: {e}")
    finally:
        await w_client.disconnect()

async def monitor_luck_emojis(chat_id, post_id):
    """Динамический анализ: запускает десант и останавливает его (Миротворец)"""
    from database.models import LuckRaid
    from sqlalchemy import update
    
    print(f"📊 [УДАЧА] Начало мониторинга поста {post_id}. Окно: 5 минут.")
    LUCK_TEXT_EMOJIS = ['🎰', '🏀', '🎯', '🎲', '🎳', '⚽️']
    
    start_time = datetime.now()
    timeout = 300 
    raid_activated = False # Флаг, чтобы не создавать рейд повторно в одном цикле

    while (datetime.now() - start_time).total_seconds() < timeout:
        await asyncio.sleep(20) 
        
        unique_users = set()
        emoji_stats = {}
        
        try:
            async for msg in client.iter_messages(chat_id, reply_to=post_id, limit=100):
                hit_emoji = None
                
                # 1. Проверка на Dice (анимированные)
                if msg.media and hasattr(msg.media, 'emoticon'):
                    if msg.media.emoticon in LUCK_TEXT_EMOJIS:
                        hit_emoji = msg.media.emoticon
                
                # 2. Проверка на Текст
                if not hit_emoji and msg.message:
                    for emo in LUCK_TEXT_EMOJIS:
                        if emo in msg.message:
                            hit_emoji = emo
                            break

                if hit_emoji and msg.sender_id:
                    # Игнорируем наших воркеров при подсчете активности людей
                    if msg.sender_id not in MY_WORKERS:
                        unique_users.add(msg.sender_id)
                        emoji_stats[hit_emoji] = emoji_stats.get(hit_emoji, 0) + 1

            # --- ЛОГИКА ЗАПУСКА ---
            if not raid_activated:
                # Твои тестовые условия: 1 юзер и 3 эмодзи
                if len(unique_users) >= 1 and sum(emoji_stats.values()) >= 3:
                    top_emoji = max(emoji_stats, key=emoji_stats.get)
                    print(f"🔥 [УДАЧА] ТРИГГЕР ПРОБИТ! Начинаю десант {top_emoji}...")
                    
                    async with async_session() as session_start:
                        new_raid = LuckRaid(
                            channel_id=chat_id,
                            post_id=post_id,
                            emoji=top_emoji,
                            status="active"
                        )
                        session_start.add(new_raid)
                        await session_start.commit()
                    raid_activated = True

            # --- ЛОГИКА ОСТАНОВКИ (МИРОТВОРЕЦ) ---
            else:
                # Если рейд идет, но живые люди прислали меньше 2 эмодзи за последние 20 сек
                if sum(emoji_stats.values()) < 2:
                    async with async_session() as session_stop:
                        await session_stop.execute(
                            update(LuckRaid).where(
                                LuckRaid.post_id == post_id, 
                                LuckRaid.status == "active"
                            ).values(status="finished")
                        )
                        await session_stop.commit()
                    print(f"🏳️ [УДАЧА] Активность людей спала. Рейд для поста {post_id} ОСТАНОВЛЕН.")
                    return # Полностью выходим из мониторинга поста

        except Exception as e:
            print(f"⚠️ [УДАЧА] Ошибка мониторинга: {e}")
            break

    # Если вышли по таймауту (5 мин), на всякий случай закрываем рейд
    async with async_session() as session_final:
        await session_final.execute(
            update(LuckRaid).where(LuckRaid.post_id == post_id).values(status="finished")
        )
        await session_final.commit()
    print(f"💤 [УДАЧА] Время мониторинга истекло для поста {post_id}.")

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
            # --- РЕАЛИЗАЦИЯ ПУНКТА 1 ---
            if category == "fast":
                post_type = "fast"
                # Запускаем фоновую задачу БЕЗ await, чтобы не тормозить мониторинг
                asyncio.create_task(execute_fast_comment(current_chat_id, msg.id))
            else:
                post_type = "keyword"
            # ---------------------------
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
            
            # --- РЕАЛИЗАЦИЯ ПУНКТА 2 (КНОПКИ) ---
            # Если в посте есть кнопки и это НЕ просто зеркало мониторинга
            if msg.reply_markup and post_type != "monitoring":
                # Запускаем фоновую задачу рейда
                asyncio.create_task(execute_button_click_raid(current_chat_id, msg.id, msg))
            # ------------------------------------

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
    while True:
        await asyncio.sleep(5)
        async with async_session() as session:
            me = await client.get_me()
            # Берем задачи для текущего воркера
            tasks = (await session.execute(select(OutgoingMessage).where(
                OutgoingMessage.worker_tg_id == me.id, 
                OutgoingMessage.status == "pending"
            ))).scalars().all()

            for task in tasks:
                try:
                    receiver = await client.get_input_entity(task.receiver_id)
                    
                    # ПУНКТ 1: ПОМЕТКА ПРОЧИТАННЫМ (Всегда при ответе)
                    await client.send_read_acknowledge(receiver)

                    # ПУНКТ 3: РЕАКЦИИ
                    if task.task_type == "reaction":
                        await client(SendReactionRequest(
                            peer=receiver,
                            msg_id=task.reply_to_msg_id,
                            reaction=[ReactionEmoji(emoticon=task.reaction_data)]
                        ))
                        print(f"✅ [РЕАКЦИЯ] Поставил {task.reaction_data}")

                    # ПУНКТ 2: ТЕКСТ И МЕДИА
                    elif task.task_type == "text":
                        async with client.action(receiver, 'typing'):
                            await asyncio.sleep(random.randint(3, 7))
                            await client.send_message(receiver, task.text, reply_to=task.reply_to_msg_id)
                    
                                        # ПУНКТ 2: ТЕКСТ
                    elif task.task_type == "text":
                        if not task.text: raise Exception("Пустое текстовое сообщение")
                        async with client.action(receiver, 'typing'):
                            await asyncio.sleep(random.randint(3, 7))
                            await client.send_message(receiver, task.text, reply_to=task.reply_to_msg_id)
                    
                    # ПУНКТ 4: МЕДИА (ФОТО/ГС/ВИДЕО)
                    elif task.task_type == "media":
                        print(f"🖼 [РУКИ] Пересылка медиа из хранилища для {task.receiver_id}...")
                        
                        # Копируем сообщение из хранилища напрямую пользователю
                        # send_message с объектом сообщения — это самый чистый способ
                        storage_msg = await client.get_messages(MONITOR_STORAGE, ids=task.storage_msg_id)
                        
                        await client.send_message(
                            receiver,
                            storage_msg, # Передаем весь объект сообщения (фото+текст)
                            reply_to=task.reply_to_msg_id
                        )


                    task.status = "sent"
                except Exception as e:
                    print(f"❌ [ОШИБКА РУК]: {e}")
                    task.status = "error"
            await session.commit()


# --- ПУНКТ 1: РУКИ (АВТО-КОММЕНТАРИЙ ПРИ УПОМИНАНИИ) ---
async def worker_mention_task_loop():
    """Следит за таблицей упоминаний и отвечает в комменты"""
    print("💬 [РУКИ] Модуль авто-комментариев запущен.")
    # Список фраз для рандома (потом вынесем в БД)
    RANDOM_PHRASES = ["мать те трахал", "здохни", "сука", "да", "тут", "бабку помой", "бля тут"]

    while True:
        await asyncio.sleep(15) # Проверка раз в 15 секунд
        async with async_session() as session:
            from database.models import MentionTask
            me = await client.get_me()
            
            # Ищем задачи для нашего аккаунта
            query = select(MentionTask).where(
                MentionTask.worker_tg_id == me.id,
                MentionTask.status == "pending"
            )
            tasks = (await session.execute(query)).scalars().all()

            for task in tasks:
                try:
                    # Рандомная задержка (мимикрия)
                    delay = random.randint(10, 45)
                    print(f"⏳ [КОММЕНТ] Отвечу в пост {task.post_id} через {delay}с...")
                    await asyncio.sleep(delay)

                    # Пишем комментарий
                    # Telethon автоматически находит группу обсуждения через reply_to
                    await client.send_message(
                        entity=task.channel_id,
                        message=random.choice(RANDOM_PHRASES),
                        comment_to=task.post_id
                    )
                    
                    task.status = "completed"
                    print(f"✅ [КОММЕНТ] Успешно ответил на упоминание в посте {task.post_id}")
                except Exception as e:
                    print(f"❌ [КОММЕНТ] Ошибка: {e}")
                    task.status = "error"
            
            await session.commit()

# --- ПУНКТ 2: РУКИ (ДЕСАНТ УДАЧИ) ---
async def worker_luck_raid_loop():
    print("🎯 [РУКИ] Модуль десанта удачи запущен.")
    while True:
        await asyncio.sleep(15) 
        async with async_session() as session:
            from database.models import LuckRaid
            # Ищем только активные рейды
            active_raids = (await session.execute(select(LuckRaid).where(LuckRaid.status == "active"))).scalars().all()

            for raid in active_raids:
                me = await client.get_me()
                
                # ИМИТАЦИЯ: Шанс 30%, что этот воркер вступит в этот цикл (так мы получим 3-5 юзеров)
                if random.random() > 0.3: 
                    continue

                try:
                    delay = random.randint(15, 60) # Увеличили паузы для беспалевности
                    print(f"🎰 [ДЕСАНТ] Аккаунт {me.id} подкинет {raid.emoji} через {delay}с...")
                    await asyncio.sleep(delay)
                    
                    # ПУНКТ 3: ОТПРАВКА АНИМИРОВАННОГО КУБИКА (УНИВЕРСАЛЬНО)
                    if raid.emoji in ['🎰', '🎯', '🎲', '🏀', '⚽️', '🎳']:
                        from telethon.tl.types import InputMediaDice
                        await client.send_message(
                            raid.channel_id,
                            file=InputMediaDice(raid.emoji), # Отправка анимации
                            comment_to=raid.post_id
                        )
                    else:
                        await client.send_message(
                            raid.channel_id, 
                            raid.emoji, 
                            comment_to=raid.post_id
                        )

                        
                    print(f"✅ [ДЕСАНТ] Аккаунт {me.id} успешно высадился.")
                except Exception as e:
                    print(f"❌ [ДЕСАНТ] Ошибка: {e}")

async def star_execution_loop():
    """Фоновая задача: отправка подарков (Медведь, Роза и т.д.)"""
    print(f"⭐ [ВОРКЕР {GROUP_TAG}] Модуль подарков запущен.")
    
    # Словарь соответствия: Название в боте -> Технический slug подарка в TG
    # Внимание: Slug-и могут меняться Telegram-ом. 
    GIFT_SLUGS = {
        "🧸 Медведь": "bear",
        "🌹 Роза": "rose",
        "💐 Букет": "bouquet",
        "🏆 Кубок": "cup"
    }

    while True:
        await asyncio.sleep(30)
        try:
            async with async_session() as session:
                me = await client.get_me()
                
                # Ищем одобренные рапорты
                stmt = text("""
                    SELECT id, target_user, method, star_count 
                    FROM management.star_reports 
                    WHERE status = 'approved' AND executor_id = :my_id
                """)
                res = await session.execute(stmt, {"my_id": me.id})
                reports = res.all()

                for r_id, target, gift_name, count in reports:
                    print(f"💰 [ЗВЕЗДЫ] Начинаю процесс отправки '{gift_name}' для {target}...")
                    
                    try:
                        # 1. Получаем ID получателя
                        peer = await client.get_input_entity(target)
                        
                        from telethon import functions, types
                        
                        # 2. Получаем список подарков через универсальный запрос
                        # Если GetStarsGiftsRequest не виден как атрибут, вызываем его через класс
                        try:
                            # Попытка вызвать через общий конструктор
                            all_gifts = await client(functions.payments.GetStarsGiftsRequest())
                        except AttributeError:
                            # Если Telethon "не видит" имя, используем альтернативный путь
                            from telethon.tl.functions.payments import GetStarsGiftsRequest as GSG
                            all_gifts = await client(GSG())

                        # Ищем подарок
                        target_gift = all_gifts.gifts[0] # Берем первый (самый дешевый) по умолчанию
                        for g in all_gifts.gifts:
                            if "bear" in g.slug.lower() or "bear" in gift_name.lower():
                                target_gift = g
                                break

                        # 3. ОТПРАВЛЯЕМ ФОРМУ ОПЛАТЫ (ЗВЕЗДНЫЙ ПОДАРК)
                        await client(functions.payments.SendStarsFormRequest(
                            purpose=types.InputStorePaymentStarsGift(
                                user_id=peer,
                                gift=target_gift
                            )
                        ))

                        print(f"✅ [ЗВЕЗДЫ] Подарок {gift_name} успешно отправлен через StarsForm!")
                        
                        await session.execute(
                            update(StarReport).where(StarReport.id == r_id).values(status="completed")
                        )
                        await session.commit()
                        
                    except Exception as e:
                        print(f"❌ [ЗВЕЗДЫ] Ошибка: {e}")
                        await session.execute(update(StarReport).where(StarReport.id == r_id).values(status="failed"))
                        await session.commit()

        except Exception as e:
            print(f"⚠️ [ЗВЕЗДЫ] Ошибка цикла: {e}")

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
        # Запускаем десант в фоновом режиме
    asyncio.create_task(worker_luck_raid_loop())
    asyncio.create_task(worker_mention_task_loop())
    asyncio.create_task(vote_execution_loop())
    asyncio.create_task(star_execution_loop())

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

async def vote_execution_loop():
    print("🗳 [ВОРКЕР] Модуль голосований ВКЛЮЧЕН в очередь...") # ЭТОТ ПРИНТ ДОЛЖЕН БЫТЬ
    await asyncio.sleep(5) # Даем время на подключение основного клиента
    executed_reports = set()
    # ... далее остальной код ...


    while True:
        await asyncio.sleep(20)
        try:
            async with async_session() as session:
                # Используем экранирование \:\: чтобы SQLAlchemy не путала это с параметрами
                sql_query = text("""
                    SELECT id, target_msg_id, target_chat_id, vote_type, option_id, intensity, accounts_count
                    FROM management.voting_reports
                    WHERE status = 'approved' 
                    AND target_groups\:\:jsonb @> :tag_json\:\:jsonb
                """)
            
                # Параметр передаем как обычно
                results = await session.execute(sql_query, {"tag_json": f'["{GROUP_TAG}"]'})
                active_reports = results.all()




                for r_id, msg_id, chat_id, v_type, opt_id, intensity, acc_limit in active_reports:
                    if r_id in executed_reports:
                        continue

                    # --- ЗАЩИТА: Проверяем, что вариант (opt_id) не пустой ---
                    if opt_id is None:
                        print(f"⚠️ [ГОЛОС] Пропуск рапорта #{r_id}: не указан вариант (option_id в БД пусто)")
                        executed_reports.add(r_id) # Чтобы не спамить ошибкой
                        continue
                    
                    target_emoji = str(opt_id).strip() # Теперь точно будет строка, даже если там число
                    # -------------------------------------------------------

                    # Мимикрия (паузы)
                    # --- ИСПРАВЛЕННЫЕ ТАЙМИНГИ (ИНТЕНСИВНОСТЬ) ---
                    delay_map = {1: 600, 2: 300, 3: 120, 4: 30}
                    max_delay = delay_map.get(intensity, 60)
                    
                    # Гарантируем, что нижняя граница (5с) всегда меньше верхней (max_delay)
                    lower_bound = 5
                    upper_bound = max(max_delay, lower_bound + 1)
                    
                    wait_before = random.randint(lower_bound, upper_bound)
                    print(f"⏳ [ГОЛОС] Аккаунт {GROUP_TAG} 'читает' канал, подождет {wait_before}с...")
                    await asyncio.sleep(wait_before)


                    try:
                        await asyncio.sleep(random.uniform(1.5, 4.2))

                        if v_type == "poll":
                            from telethon.tl.functions.messages import SendVoteRequest
                            # Используем наш новый метод получения реального ID варианта из сообщения
                            msg_data = await client.get_messages(chat_id, ids=msg_id)
                            
                            if msg_data and msg_data.poll:
                                try:
                                    idx = int(target_emoji) - 1 # Оператор ввел 1 -> индекс 0
                                    if idx < 0: idx = 0
                                    poll_answers = msg_data.poll.poll.answers
                                    
                                    if idx < len(poll_answers):
                                        chosen_option_id = poll_answers[idx].option
                                        await client(SendVoteRequest(
                                            peer=chat_id,
                                            msg_id=msg_id,
                                            options=[chosen_option_id]
                                        ))
                                        executed_reports.add(r_id)
                                        print(f"✅ [ГОЛОС] Опрос выполнен в рапорте #{r_id}")
                                    else:
                                        print(f"❌ [ГОЛОС] Индекс {idx+1} вне диапазона опроса")
                                except ValueError:
                                    print(f"❌ [ГОЛОС] Ошибка: вариант в опросе должен быть числом, а пришло: {target_emoji}")
                        
                        else: # РЕАКЦИИ
                            from telethon.tl.functions.messages import SendReactionRequest
                            from telethon.tl.types import ReactionEmoji, ReactionCustomEmoji

                            if target_emoji.isdigit():
                                reaction_obj = [ReactionCustomEmoji(document_id=int(target_emoji))]
                            else:
                                reaction_obj = [ReactionEmoji(emoticon=target_emoji)]

                            await client(SendReactionRequest(
                                peer=chat_id,
                                msg_id=msg_id,
                                reaction=reaction_obj
                            ))
                            executed_reports.add(r_id)
                            print(f"✅ [РЕАКЦИЯ] Поставлена в рапорте #{r_id}")

                    except Exception as e:
                        print(f"❌ [ГОЛОС] Ошибка выполнения рапорта #{r_id}: {e}")


        except Exception as e:
            print(f"⚠️ [ГОЛОС] Ошибка цикла: {e}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Мониторинг остановлен пользователем.")
