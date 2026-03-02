import asyncio
import random
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import MessageEntityMentionName, MessageEntityMention, ReactionEmoji
from sqlalchemy import select, func, text, update  # <-- ДОБАВИЛИ update
from datetime import datetime, timedelta
from telethon import functions, types
import re
import ddddocr
import io
from PIL import Image, ImageOps, ImageEnhance
from telethon.tl.functions.payments import GetStarsStatusRequest
import os
from playwright.async_api import async_playwright
from telethon.tl import functions
from playwright_stealth import stealth_async
# Импорты из обновленной базы
from database.config import async_session
from database.models import (
    Keyword, PotentialPost, WorkerAccount, 
    TargetChannel, ReaderAccount, ContestPassport, 
    LuckEvent, OutgoingMessage, StarReport, GroupChannelRelation  # <-- ДОБАВИЛИ StarReport
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
# Кэш для отслеживания запущенных паспортов, чтобы не запускать их дважды
ACTIVE_TASKS_CACHE = set() 
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
    """
    ЛОГИКА: Вступление + Нажатие + Определение Капчи (Браузер/Бот)
    """
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
        # --- 1. ИЗВЛЕЧЕНИЕ КНОПКИ ---
        button = None
        if msg_obj.reply_markup and msg_obj.reply_markup.rows:
            button = msg_obj.reply_markup.rows[0].buttons[0]
        if not button:
            print(f"⚠️ [BUTTON] Кнопка в посте {post_id} не найдена.")
            return
        url = getattr(button, 'url', None)
        # --- 2. ПРОВЕРКА НА КАПЧУ / MINI APP (БРАУЗЕР) ---
        captcha_markers = ["verify", "captcha", "robot", "confirm", "startapp="]
        if url and any(marker in url.lower() for marker in captcha_markers):
            print(f"🔗 [КНОПКА] Ссылка {url} похожа на капчу. Подготовка браузера...")
            entity = await w_client.get_entity(chat_id)
            channel_username = entity.username if hasattr(entity, 'username') else str(chat_id)
            # !!! ВАЖНО: Отключаем ТГ перед тяжелым браузером !!!
            await w_client.disconnect()
            print(f"🔌 [TG] Клиент отключен. Запуск Playwright для @{channel_username}...")
            # Запуск браузера (3 аргумента)
            success = await solve_web_captcha(worker.phone, channel_username, post_id)
            if success:
                print(f"✅ [ВЕБ-УСПЕХ] Воркер {worker.tg_id} прошел проверку.")
            else:
                print(f"❌ [ВЕБ-ПРОВАЛ] Воркер {worker.tg_id} не справился.")
            return 
        # --- 3. ЛОГИКА ТЕЛЕГРАМ-БОТОВ (START PARAM) ---
        if url and "t.me/" in url:
            bot_match = re.search(r"t.me/([\w_]+)\?start=([\w-]+)", url)
            if bot_match:
                bot_username = bot_match.group(1)
                start_param = bot_match.group(2)
                from telethon.tl.functions.messages import StartBotRequest
                await w_client(StartBotRequest(bot=bot_username, peer=bot_username, start_param=start_param))
                print(f"🤖 [BOT] Воркер {worker.tg_id} запустил бота @{bot_username}.")
                await asyncio.sleep(5) 
                async for message in w_client.iter_messages(bot_username, limit=1):
                    if message.photo:
                        photo_bytes = await w_client.download_media(message.photo, file=bytes)
                        import ddddocr
                        ocr = ddddocr.DdddOcr(show_ad=False)
                        captcha_text = ocr.classification(photo_bytes)
                        captcha_digits = "".join(filter(str.isdigit, captcha_text))
                        if captcha_digits:
                            await w_client.send_message(bot_username, captcha_digits)
                return
        # --- 4. ОБЫЧНЫЙ КЛИК (CALLBACK) ---
        try:
            await msg_obj.click(0)
            print(f"✅ [BUTTON] Воркер {worker.tg_id} нажал кнопку.")
        except Exception as e:
            print(f"⚠️ [BUTTON] Клик не удался: {e}")
    except Exception as e:
        print(f"❌ [BUTTON-ERR] Ошибка воркера {worker.tg_id}: {e}")
    finally:
        # Проверяем соединение перед закрытием, чтобы не ловить ошибки в логах
        if w_client and w_client.is_connected():
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

# --- ЛОГИКА ВЫПОЛНЕНИЯ ЗАДАЧ ИЗ ПАСПОРТА (Пункт 1) ---

async def passport_execution_loop():
    """
    Улучшенный цикл выполнения задач. 
    Запускает стратегию для паспорта только один раз.
    """
    print(f"⚙️ [ВОРКЕР {GROUP_TAG}] Двигатель задач запущен.")
    
    while True:
        await asyncio.sleep(30) # Проверяем чуть чаще
        try:
            async with async_session() as session:
                query = select(ContestPassport).where(
                    ContestPassport.group_tag == GROUP_TAG,
                    ContestPassport.status == "active"
                )
                active_passports = (await session.execute(query)).scalars().all()

            for passport in active_passports:
                # ПРОВЕРКА: Если этот паспорт уже запущен в работу — пропускаем
                if passport.id in ACTIVE_TASKS_CACHE:
                    continue
                
                print(f"🚀 [ЗАПУСК] Начинаю выполнение паспорта #{passport.id}")
                ACTIVE_TASKS_CACHE.add(passport.id)
                
                # Запускаем выполнение
                asyncio.create_task(run_passport_strategy(passport))
                
        except Exception as e:
            print(f"❌ [LOOP] Ошибка в цикле паспортов: {e}")
async def process_gifts_inventory(worker_phone):
    clean_phone = str(worker_phone).replace("+", "")
    user_data_dir = f"/var/lib/browser_sessions/session_{clean_phone}"
    
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir, headless=True, args=['--no-sandbox', '--disable-setuid-sandbox']
        )
        page = await context.new_page()
        await stealth_async(page)
        
        try:
            print(f"🌐 [ИНВЕНТАРЬ] Загрузка Web для {clean_phone}...")
            await page.goto("https://web.telegram.org", wait_until="networkidle", timeout=60000)
            await asyncio.sleep(8) 

            # 1. Menu -> My Profile
            await page.get_by_role("button", name="Open menu").first.click()
            await asyncio.sleep(2)
            await page.get_by_role("menuitem", name="My Profile").first.click()
            await asyncio.sleep(4)

            # 2. Gifts Tab
            await page.get_by_text("Gifts").first.click()
            await asyncio.sleep(5)

            while True:
                # 1. Клик по Canvas (уже работает)
                canvas = page.locator("#RightColumn canvas, .gifts-list canvas").first
                if await canvas.is_visible(timeout=7000):
                    print(f"📦 [ИНВЕНТАРЬ] Открываю карточку подарка через Canvas...")
                    await canvas.click(position={"x": 68, "y": 31})
                    await asyncio.sleep(5) # Даем модалке время на анимацию

                    # 2. Поиск кнопки продажи (Convert)
                    # Используем get_by_text, так как твой codegen нашел её именно так
                    convert_btn = page.get_by_text(re.compile(r"Convert to \d+ Stars", re.IGNORECASE)).first
                    
                    if await convert_btn.is_visible(timeout=5000):
                        print(f"💰 [ИНВЕНТАРЬ] Кнопка найдена. Нажимаю Convert...")
                        await convert_btn.click()
                        await asyncio.sleep(3)
                        
                        # 3. Подтверждение (Confirm)
                        confirm_btn = page.get_by_role("button", name="Confirm").first
                        if await confirm_btn.is_visible(timeout=3000):
                            await confirm_btn.click()
                            print(f"✅ [ИНВЕНТАРЬ] Продажа подтверждена.")
                            await asyncio.sleep(5)
                            
                            # 4. Закрытие окна баланса (Close)
                            close_btn = page.get_by_role("button", name="Close").first
                            if await close_btn.is_visible():
                                await close_btn.click()
                                print(f"🔘 [ИНВЕНТАРЬ] Окно баланса закрыто.")
                            else:
                                await page.keyboard.press("Escape")
                        else:
                            await page.keyboard.press("Escape")
                    else:
                        print("ℹ️ [ИНВЕНТАРЬ] Кнопка Convert не найдена в открытой карточке.")
                        await page.keyboard.press("Escape")
                        break 
                    
                    await asyncio.sleep(3)
                else:
                    print(f"📭 [ИНВЕНТАРЬ] Подарков в профиле больше нет.")
                    break

        except Exception as e:
            print(f"❌ [ИНВЕНТАРЬ-ERR] Ошибка: {e}")
        finally:
            await context.close()

async def check_stars_balance_api():
    """
    Пункт 1: Проверка баланса звезд в рамках 12-часовых окон (00-12 и 12-24).
    """
    print(f"💰 [ЭКОНОМИКА] Модуль контроля баланса (ОКНА) запущен для {GROUP_TAG}.")
    while True:
        now = datetime.now()
        # 1. ОПРЕДЕЛЯЕМ ГРАНИЦУ ТЕКУЩЕГО ОКНА
        # Если сейчас меньше 12:00, окно закончится в 12:00 сегодня.
        # Если больше 12:00, окно закончится в 00:00 следующего дня.
                # ТЕСТОВЫЙ РЕЖИМ: Окно закрывается каждую минуту
                # ВОЗВРАЩАЕМ: Окна 00-12 и 12-24
        if now.hour < 12:
            current_window_end = now.replace(hour=12, minute=0, second=0, microsecond=0)
        else:
            current_window_end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


        async with async_session() as session:
            me = await client.get_me()
            res = await session.execute(
                select(WorkerAccount).where(WorkerAccount.tg_id == me.id)
            )
            worker = res.scalar_one_or_none()
            if not worker: 
                await asyncio.sleep(60)
                continue
            # 2. ПРОВЕРЯЕМ: Отработано ли текущее окно?
            # Если в базе записан конец текущего окна, значит в этом промежутке мы уже проверялись.
            if worker.last_check_window_end == current_window_end:
                # Ждем до начала следующего окна + небольшой запас (10-30 сек)
                wait_until_next = (current_window_end - now).total_seconds() + random.randint(10, 30)
                print(f"💤 [БАЛАНС] {me.id} уже проверялся в этом окне. Сон до {current_window_end}")
                await asyncio.sleep(wait_until_next)
                continue
            # 3. ВЫБИРАЕМ РАНДОМНЫЙ МОМЕНТ В ОСТАВШЕМСЯ ВРЕМЕНИ ОКНА
            # Считаем сколько секунд осталось до конца окна
            seconds_remaining = (current_window_end - now).total_seconds()
            # Выбираем случайное время ожидания ОТ ТЕКУЩЕГО МОМЕНТА до конца окна
            seconds_remaining = (current_window_end - now).total_seconds()
            random_wait = random.uniform(0, seconds_remaining)


            print(f"⏳ [БАЛАНС] {me.id} выставил проверку через {int(random_wait/60)} мин (внутри окна до {current_window_end})")
            await asyncio.sleep(random_wait)
            # 4. ВЫПОЛНЯЕМ ПРОВЕРКУ
            try:
                # 1. Используем GetStarsStatusRequest (он дает статус баланса)
                stars_status = await client(GetStarsStatusRequest(peer='me'))
                
                # 2. ИСПРАВЛЕНИЕ ОШИБКИ: берем .amount (это целое число)
                # balance.amount — это то, что нужно для сравнения >= 40
                current_balance = int(stars_status.balance.amount) 
                
                is_ready = current_balance >= 40
                
                await session.execute(
                    update(WorkerAccount)
                    .where(WorkerAccount.tg_id == me.id)
                    .values(
                        stars_balance=current_balance,
                        is_financial_ready=is_ready,
                        last_check_window_end=current_window_end
                    )
                )
                await session.commit()
                print(f"✅ [БАЛАНС] {me.id}: {current_balance} ⭐. Статус готовности: {is_ready}")

            except Exception as e:
                print(f"❌ [БАЛАНС-ERR] {me.id}: {e}")
                await asyncio.sleep(300) # При ошибке спим 5 минут и пробуем снова
async def check_inventory_loop():
    """Фоновый цикл для проверки подарков раз в 12 часов (по окнам)"""
    print(f"📦 [ЭКОНОМИКА] Модуль инвентаризации запущен для {GROUP_TAG}.")
    while True:
        now = datetime.now()
        if now.hour < 12:
            current_window_end = now.replace(hour=12, minute=0, second=0, microsecond=0)
        else:
            current_window_end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

        async with async_session() as session:
            me = await client.get_me()
            res = await session.execute(select(WorkerAccount).where(WorkerAccount.tg_id == me.id))
            worker = res.scalar_one_or_none()
            if not worker or worker.last_inventory_check_window_end == current_window_end:
                await asyncio.sleep(600) # Проверка раз в 10 мин
                continue
            seconds_remaining = (current_window_end - now).total_seconds()
            random_wait = random.uniform(0, seconds_remaining)

            print(f"⏳ [ИНВЕНТАРЬ] {me.id} проверит подарки через {int(random_wait/60)} мин.")
            await asyncio.sleep(random_wait)
            try:
                await process_gifts_inventory(me.phone)
                await session.execute(
                    update(WorkerAccount).where(WorkerAccount.tg_id == me.id)
                    .values(last_inventory_check_window_end=current_window_end)
                )
                await session.commit()
            except Exception as e:
                print(f"❌ [ИНВЕНТАРЬ-LOOP-ERR] {e}")
async def run_passport_strategy(passport):
    """
    Рассчитывает 'эстафету' и ЗАВЕРШАЕТ паспорт после выполнения.
    """
    intensity_map = {1: 1200, 2: 600, 3: 300, 4: 60}
    slot_duration = intensity_map.get(passport.intensity_level, 600)
    async with async_session() as session:
        res = await session.execute(
            select(WorkerAccount).where(
                WorkerAccount.group_tag == GROUP_TAG,
                WorkerAccount.is_alive == True
            ).order_by(WorkerAccount.id)
        )
        workers = res.scalars().all()
    if not workers: 
        # Если воркеров нет, выкидываем паспорт из кэша, чтобы попробовать позже
        ACTIVE_TASKS_CACHE.discard(passport.id)
        return
    # Список задач (фьючерсов) для отслеживания
    tasks = []
    if passport.type == "vote":
        target_id = passport.conditions.get("vote_details", {}).get("executor")
        lead = next((w for w in workers if str(w.tg_id) == str(target_id)), None)
        if lead:
            # Создаем задачу и добавляем в список
            tasks.append(asyncio.create_task(execute_single_worker_tasks(lead, passport, is_lead=True)))
    else:
        # Для АФК создаем задачи для всех воркеров
        for i, worker in enumerate(workers):
            wait_for_slot = i * slot_duration
            tasks.append(asyncio.create_task(delayed_worker_execution(worker, passport, wait_for_slot, slot_duration)))
    # --- НОВАЯ ЛОГИКА ЗАВЕРШЕНИЯ ---
    # Ждем, пока ВСЕ запущенные задачи (воркеры) в этой эстафете закончат работу
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
        # Когда все закончили — меняем статус в БД на finished
        async with async_session() as session_fin:
            await session_fin.execute(
                update(ContestPassport)
                .where(ContestPassport.id == passport.id)
                .values(status="finished")
            )
            await session_fin.commit()
        print(f"🏁 [ПАСПОРТ] Все задачи по паспорту #{passport.id} ВЫПОЛНЕНЫ. Статус: finished.")
        # Удаляем из локального кэша, чтобы освободить память
        ACTIVE_TASKS_CACHE.discard(passport.id)
async def delayed_worker_execution(worker, passport, initial_delay, slot_limit):
    """Ждет свою очередь в эстафете и запускает выполнение"""
    await asyncio.sleep(initial_delay)
    # Внутри своего 10-минутного окна воркер тоже ждет рандомное время (мимикрия)
    # Например, если окно 600 сек, он начнет в любую секунду от 5-й до 480-й.
    intra_slot_delay = random.randint(5, int(slot_limit * 0.8))
    await asyncio.sleep(intra_slot_delay)
    await execute_single_worker_tasks(worker, passport)
# --- ОБНОВЛЕННАЯ ФУНКЦИЯ С БЛОКОМ КОММЕНТАРИЕВ ---
async def execute_single_worker_tasks(worker, passport, is_lead=False):
    conds = passport.conditions
    actions = conds.get("selected", [])
    target_chat = conds.get("source_tg_id")
    target_msg = conds.get("source_msg_id")
    # Список фраз для обычных комментариев (мимикрия)
    # Можно будет позже вынести в БД
    COMMON_PHRASES = ["участвую", "+", "го", "хочу приз", "удачи всем", "🍀", "надеюсь на победу", "🔥", "инвест"]
    w_client = TelegramClient(
        StringSession(worker.session_string), 
        worker.api_id, worker.api_hash,
        device_model=worker.device_model,
        system_version=worker.os_version,
        app_version=worker.app_version
    )
    try:
        await w_client.connect()
        random.shuffle(actions)
        for action in actions:
            await asyncio.sleep(random.randint(15, 45))
            # 1. ПОДПИСКА
            if action == "sub":
                links = conds.get("sub_links", "").split()
                for link in links:
                    await join_channel_smart(w_client, link)
            # 2. РЕАКЦИЯ
            elif action == "reac" and target_chat and target_msg:
                try:
                    from telethon.tl.functions.messages import SendReactionRequest
                    from telethon.tl.types import ReactionEmoji
                    await w_client(SendReactionRequest(
                        peer=target_chat,
                        msg_id=target_msg,
                        reaction=[ReactionEmoji(emoticon=random.choice(["👍", "❤️", "🔥", "🤩"]))]
                    ))
                    print(f"✅ [РЕАКЦИЯ] Воркер {worker.tg_id} поставил эмодзи.")
                except: pass
            # 3. РЕПОСТ
            elif action == "repost" and target_chat and target_msg:
                count = int(conds.get("repost_count", 1))
                await perform_network_reposts(w_client, target_chat, target_msg, count)
            # 4. КОММЕНТАРИЙ (ТО, ЧЕГО НЕ ХВАТАЛО)
            elif action == "comm" and target_chat and target_msg:
                try:
                    # Отправляем рандомную фразу как комментарий к посту
                    await w_client.send_message(
                        target_chat, 
                        random.choice(COMMON_PHRASES), 
                        comment_to=target_msg
                    )
                    print(f"✅ [КОММЕНТ] Воркер {worker.tg_id} оставил комментарий.")
                except Exception as e:
                    print(f"❌ [КОММЕНТ] Ошибка воркера {worker.tg_id}: {e}")
        # Если АФК - нажимаем кнопку участия
        if passport.type == "afk" and target_chat and target_msg:
            try:
                msg_obj = await w_client.get_messages(target_chat, ids=target_msg)
                if msg_obj and msg_obj.reply_markup:
                    # Вызываем нашу функцию клика (убедись, что она определена выше в коде)
                    await single_button_click(worker, target_chat, target_msg, msg_obj, 0)
            except Exception as e:
                print(f"❌ [КНОПКА] Ошибка: {e}")
                # Если ГОЛОСОВАНИЕ (лид-регистрация)
        if is_lead:
            details = conds.get("vote_details", {})
            place = details.get("reg_place", "")
            content = details.get("reg_data", "")
            media_id = details.get("reg_media_id") # Получаем ID из хранилища
            target = place.replace("ЛС ", "").replace("@", "")
            # Подготовка объекта для отправки (текст или медиа из хранилища)
            msg_to_send = content
            if media_id:
                # Берем медиа из мониторинга
                storage_msg = await w_client.get_messages(MONITOR_STORAGE, ids=media_id)
                msg_to_send = storage_msg # Весь объект сообщения (фото + текст)
            if "Комментарии" in place:
                await w_client.send_message(target_chat, msg_to_send, comment_to=target_msg)
            else:
                await w_client.send_message(target, msg_to_send)
            print(f"✅ [ГОЛОС] Лид {worker.tg_id} отправил заявку (с медиа: {bool(media_id)}) в {place}")
    except Exception as e:
        print(f"❌ [ИСПОЛНИТЕЛЬ {worker.tg_id}] Критическая ошибка: {e}")
    finally:
        await w_client.disconnect()
async def join_channel_smart(client, link):
    """Проверяет подписку перед тем как подписаться (Пункт 1)"""
    try:
        # Пытаемся получить инфо о канале
        channel = await client.get_entity(link)
        # Если мы тут, значит канал доступен. Пытаемся вступить.
        # Telethon сам проигнорирует, если мы уже там, но для стелса можно усложнить.
        from telethon.tl.functions.channels import JoinChannelRequest
        await client(JoinChannelRequest(channel=channel))
        print(f"✅ Успешная подписка на {link}")
    except Exception as e:
        print(f"❌ Ошибка подписки на {link}: {e}")

# --- ОБНОВЛЕННАЯ ЛОГИКА РЕПОСТОВ (Пункт 2 + Защита) ---

async def perform_network_reposts(client, chat_id, msg_id, count):
    """
    Репостит сообщение другим воркерам группы. 
    Если воркеров мало — репостит в 'Избранное' (Saved Messages).
    """
    async with async_session() as session:
        me = await client.get_me()
        
        # 1. Ищем потенциальных получателей в нашей группе (кроме себя)
        res = await session.execute(
            select(WorkerAccount.tg_id).where(
                WorkerAccount.group_tag == GROUP_TAG,
                WorkerAccount.tg_id != me.id,
                WorkerAccount.is_alive == True
            ).order_by(func.random()).limit(count)
        )
        targets = res.scalars().all()
        
        # 2. ПРОВЕРКА: Если целей меньше, чем нужно репостов
        if len(targets) < count:
            print(f"⚠️ [РЕПОСТ] Мало воркеров ({len(targets)}). Добиваю репостом в Избранное.")
            try:
                # 'me.id' или 'me' в качестве цели в Telethon — это отправка в Saved Messages
                await client.forward_messages('me', msg_id, chat_id)
                # Уменьшаем счетчик нужных репостов, так как один уже ушел в Избранное
                count -= 1 
            except Exception as e:
                print(f"❌ [РЕПОСТ] Ошибка в Избранное: {e}")

        # 3. Рассылаем остаток по живым воркерам
        for target_id in targets:
            if count <= 0: break
            try:
                # Имитируем 'чтение' перед пересылкой
                await asyncio.sleep(random.randint(3, 7))
                await client.forward_messages(target_id, msg_id, chat_id)
                count -= 1
                print(f"✅ [РЕПОСТ] Воркер {me.id} переслал пост воркеру {target_id}")
            except Exception as e:
                print(f"❌ [РЕПОСТ] Не удалось отправить {target_id}: {e}")

async def invite_handler_loop():
    """
    Пункт 4: Авто-инвайтинг группы по одобренному рапорту.
    Воркеры вступают в канал с разбросом в 24 часа.
    """
    print(f"👥 [ВОРКЕР {GROUP_TAG}] Цикл инвайтинга запущен.")
    while True:
        await asyncio.sleep(300) # Проверка раз в 5 минут
        async with async_session() as session:
            # Ищем задачи на инвайт для нашей группы
            query = select(GroupChannelRelation).where(
                GroupChannelRelation.group_tag == GROUP_TAG,
                GroupChannelRelation.status == 'inviting'
            )
            invites = (await session.execute(query)).scalars().all()

            for inv in invites:
                # 1. Проверяем, прошло ли 24 часа с момента старта
                start_time = inv.invite_started_at
                if datetime.now() > start_time + timedelta(hours=24):
                    inv.status = 'joined'
                    await session.commit()
                    continue

                # 2. Логика вступления текущего аккаунта
                # Считаем, сколько воркеров в группе (например 30)
                # Каждый должен вступить в свой случайный момент внутри этих 24 часов
                me = await client.get_me()
                
                # Хитрый расчет: шанс вступления в этом цикле (раз в 5 мин)
                # Чтобы за 24 часа вступили все 30 человек
                if random.random() < 0.05: 
                    try:
                        from telethon.tl.functions.channels import JoinChannelRequest
                        await client(JoinChannelRequest(channel=inv.channel_id))
                        print(f"✅ [ИНВАЙТ] Аккаунт {me.id} успешно вступил в канал {inv.channel_id}")
                    except Exception as e:
                        print(f"❌ [ИНВАЙТ] Ошибка вступления: {e}")
            
            await session.commit()
# --- ЕДИНЫЙ И ИСПРАВЛЕННЫЙ МОДУЛЬ ПОДАРКОВ (Вставлять один раз!) ---

# Кэш, чтобы не запускать один и тот же подарок дважды в параллель
# --- ЕДИНЫЙ МОДУЛЬ ПОДАРКОВ (БЕЗ ДУБЛИКАТОВ) ---

# Кэш для защиты от повторных запусков одного и того же рапорта
ACTIVE_GIFTS_CACHE = set()

async def send_gift_via_web(worker_phone, target_username, gift_type):
    """
    ОТПРАВКА ПОДАРКА ЧЕРЕЗ TELEGRAM WEB /A/ (ПО КОДУ CODEGEN)
    """
    clean_phone = str(worker_phone).replace("+", "")
    user_data_dir = f"/var/lib/browser_sessions/session_{clean_phone}"

    print(f"📂 [WEB] Запуск браузера /A/ для {clean_phone}...")

    async with async_playwright() as p:
        context = None
        try:
            context = await p.chromium.launch_persistent_context(
                user_data_dir,
                headless=True,
                slow_mo=1200, # Немного медленнее для стабильности
                args=['--no-sandbox', '--disable-setuid-sandbox']
            )
            page = await context.new_page()

            # 1. ЗАХОДИМ В /A/
            await page.goto("https://web.telegram.org/a/", wait_until="networkidle", timeout=60000)
            await asyncio.sleep(6)

            # 2. ПОИСК ПО ТВОЕМУ МЕТОДУ
            print(f"🔍 [WEB] Ищу {target_username}...")
            search_box = page.get_by_role("textbox", name="Search")
            await search_box.wait_for(state="visible", timeout=15000)
            await search_box.click()
            await search_box.fill(target_username)
            await search_box.press("Enter")
            await asyncio.sleep(4)

            # Выбор чата из результатов
            # Используем твой селектор "Fedor Maslo last" (универсально через 'last')
            await page.get_by_role("button").filter(has_text=re.compile(r"last", re.IGNORECASE)).first.click()
            await asyncio.sleep(2)

            # 3. ОТКРЫТИЕ МЕНЮ
            await page.get_by_role("button", name="More actions").click()
            await page.get_by_role("menuitem", name="Send a Gift").click()
            await asyncio.sleep(5)

            # 4. ВЫБОР ПОДАРКА (ПО ТВОИМ ИНДЕКСАМ)
            # Мы сопоставим твой выбор с индексами из записи
            # 🧸 Медведь (в записи был 5-й по счету ️)
            # 🌹 Роза (️25, 2-й) | 💐 Букет (️50, 2-й) | 🏆 Кубок (️100, 1-й)
            
            print(f"🎁 [WEB] Выбираю подарок: {gift_type}")
            
            if "Медведь" in gift_type:
                await page.get_by_role("button", name="️").nth(5).click()
            elif "Роза" in gift_type:
                await page.get_by_role("button", name="️25").nth(2).click()
            elif "Букет" in gift_type:
                await page.get_by_role("button", name="️50").nth(2).click()
            elif "Кубок" in gift_type:
                await page.get_by_role("button", name="️100").first.click()
            else:
                # Если не совпало, просто кликаем первый доступный
                await page.get_by_role("button", name="️").first.click()

            await asyncio.sleep(3)

            # 5. ФИНАЛЬНАЯ КНОПКА (ТВОЙ СЕЛЕКТОР)
            # Ты нажал на "Send a Gift for ️"
            send_btn = page.get_by_role("button", name=re.compile(r"Send a Gift for", re.IGNORECASE))
            
            if await send_btn.is_visible():
                print("🔘 [WEB] Нажимаю финальную кнопку отправки...")
                await send_btn.click()
                await asyncio.sleep(5)
                
                # Проверка: если кнопка всё еще видна — значит баланс 0 или ошибка
                if await send_btn.is_visible():
                    print("❌ [WEB] Подарок не ушел (Баланс звезд 0 или ошибка оплаты)")
                    return False
                
                print(f"✅ [WEB] РАПОРТ ВЫПОЛНЕН.")
                return True
            
            return False

        except Exception as e:
            print(f"❌ [WEB-ERR] Ошибка: {e}")
            if 'page' in locals():
                await page.screenshot(path=f"/app/DEBUG_GIFT_{clean_phone}.png")
            return False
        finally:
            if context:
                await context.close()

async def star_execution_loop():
    """Бесконечный цикл проверки рапортов на подарки"""
    print(f"⭐ [ВОРКЕР {GROUP_TAG}] Модуль подарков (WEB) активен.")
    while True:
        await asyncio.sleep(60)
        try:
            async with async_session() as session:
                me = await client.get_me()
                query = select(StarReport).where(
                    StarReport.status == 'approved',
                    StarReport.executor_id == me.id
                )
                reports = (await session.execute(query)).scalars().all()

                for report in reports:
                    if report.id in ACTIVE_GIFTS_CACHE: continue
                    
                    ACTIVE_GIFTS_CACHE.add(report.id)
                    print(f"💰 [WEB-PROCESS] Обработка рапорта #{report.id}...")
                    
                    success = await send_gift_via_web(str(me.phone), report.target_user, report.method)
                    
                    # Финальное обновление статуса
                    async with async_session() as session_upd:
                        new_status = "completed" if success else "error"
                        await session_upd.execute(
                            update(StarReport).where(StarReport.id == report.id).values(status=new_status)
                        )
                        await session_upd.commit()
                    
                    ACTIVE_GIFTS_CACHE.discard(report.id)
        except Exception as e:
            print(f"⚠️ [WEB-LOOP-ERR] {e}")
async def human_click(page, selector):
    """Находит кнопку, наводит на неё и кликает в случайную точку внутри кнопки"""
    element = page.locator(selector).first
    box = await element.bounding_box()
    if box:
        x = box['x'] + box['width'] * random.uniform(0.2, 0.8)
        y = box['y'] + box['height'] * random.uniform(0.2, 0.8)
        await page.mouse.move(x, y, steps=random.randint(5, 15))
        await asyncio.sleep(random.uniform(0.5, 1.5))
        await page.mouse.click(x, y)
async def solve_web_captcha(worker_phone, target_channel_username, post_id):
    """
    Входные данные: телефон воркера, юзернейм канала и ID поста с кнопкой.
    """
    clean_phone = str(worker_phone).replace("+", "")
    user_data_dir = f"/var/lib/browser_sessions/session_{clean_phone}"
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir,
            headless=True, 
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-blink-features=AutomationControlled']
        )
        page = await context.new_page()
        await stealth_async(page)
        try:
            # 1. ТВОЯ ОРИГИНАЛЬНАЯ ЛОГИКА ВХОДА
            await page.goto("https://web.telegram.org", wait_until="networkidle", timeout=60000)
            await asyncio.sleep(8) 
            await page.screenshot(path="/app/step1_web_opened.png")
            # 2. ТВОЙ ОРИГИНАЛЬНЫЙ ПЕРЕХОД
            print(f"🌐 [WEB] Переход в канал @{target_channel_username}...")
            await page.goto(f"https://web.telegram.org#?tgaddr=tg%3A%2F%2Fresolve%3Fdomain%3D{target_channel_username}")
            await asyncio.sleep(6)
            await page.screenshot(path="/app/step2_channel_opened.png")
                        # 3. УЛУЧШЕННЫЙ ПОИСК КНОПКИ
            print(f"⏳ [WEB] Ожидание появления кнопки в посте {post_id}...")
            button_selector = "button, .btn, .reply-markup-button, [role='button']"
            try:
                await page.wait_for_selector(button_selector, timeout=10000)
            except:
                print("⚠️ [WEB] Кнопки долго не появляются, пробую искать по тексту...")
            keywords = ['Участвовать', 'Принять участие', 'Участвую', 'Join', 'Participate', 'Check']
            button = None
            for word in keywords:
                found = page.locator(f"button:has-text('{word}'), .btn:has-text('{word}')").last
                if await found.is_visible():
                    button = found
                    print(f"✅ [WEB] Найдена кнопка с текстом: {word}")
                    break
            if button:
                await button.scroll_into_view_if_needed()
                await asyncio.sleep(1)
                await button.click()
                print("🔘 [WEB] Клик по кнопке выполнен.")
                await page.screenshot(path="/app/step3_after_click.png")
            else:
                print("❌ [WEB] Кнопка не найдена. Делаю скриншот для диагностики.")
                await page.screenshot(path="/app/step3_not_found.png")
                return False
                       # 4. ПОДТВЕРЖДЕНИЕ ЗАПУСКА (Launch)
            print("⏳ [WEB] Ожидание окна Launch...")
            confirm_selector = "button:has-text('Launch'), button:has-text('OK'), button:has-text('Открыть'), button.btn-primary"
            try:
                confirm_btn = page.locator(confirm_selector).first
                await confirm_btn.wait_for(state="visible", timeout=10000)
                print("🚀 [WEB] Кнопка Launch найдена. Нажимаю...")
                await confirm_btn.click(delay=500)
            except:
                print("⚠️ [WEB] Модалка Launch не появилась, возможно приложение открылось сразу.")
            # 5. ОЖИДАНИЕ И КЛИК ПО IFRAME
            print("⏳ [WEB] Ожидание появления Iframe (капчи)...")
            try:
                await page.wait_for_selector("iframe", timeout=20000)
                iframe_element = page.locator("iframe").first
                print("🖼 [WEB] Iframe обнаружен!")
            except:
                print("❌ [WEB] Iframe так и не появился.")
                await page.screenshot(path="/app/5_no_iframe_error.png")
                return False
            await asyncio.sleep(5)
            try:
                frame = page.frame_locator("iframe").first
                target = frame.locator("button, input[type='checkbox'], canvas, [role='button']").first
                await target.scroll_into_view_if_needed()
                await target.evaluate("node => node.click()") 
                print("🎯 [WEB] JS-клик внутри Iframe выполнен успешно.")
            except Exception as e:
                print(f"⚠️ [WEB] Ошибка JS-клика: {e}. Пробую силовой клик по центру.")
                box = await iframe_element.bounding_box()
                if box:
                    await page.mouse.click(box['x'] + box['width']/2, box['y'] + box['height']/2)
            print("⏳ [WEB] Ожидание завершения (15 сек)...")
            await asyncio.sleep(15) 
            await page.screenshot(path="/app/step6_final_check.png")
            return True
        except Exception as e:
            print(f"❌ [WEB-ERR] Ошибка Playwright: {e}")
            try:
                await page.screenshot(path=f"/app/error_{clean_phone}.png")
            except:
                pass
            return False
        finally:
            await page.close()
            await context.close()
async def resolve_channel_ids():
    """Фоновая задача: превращает ссылки в реальные tg_id с префиксом -100"""
    # 1. ИМПОРТ ВНУТРИ (чтобы точно не было ошибки)
    from telethon.tl.functions.channels import JoinChannelRequest
    
    while True:
        try:
            async with async_session() as session:
                res = await session.execute(
                    select(TargetChannel).where(TargetChannel.tg_id == None)
                )
                unknown_channels = res.scalars().all()

                for ch in unknown_channels:
                    try:
                        print(f"🔍 [ID-RESOLVER] Пробую узнать ID для: {ch.username}")
                        entity = await client.get_entity(ch.username)
                        
                        # 2. ПРАВИЛЬНЫЙ ФОРМАТ ID ДЛЯ BOT API
                        # Telethon выдает 212345678, ботам нужно -100212345678
                        raw_id = entity.id
                        if not str(raw_id).startswith("-100"):
                            # Убираем минус если он есть и лепим -100
                            formatted_id = int(f"-100{abs(raw_id)}")
                        else:
                            formatted_id = raw_id
                        
                        ch.tg_id = formatted_id
                        
                        # 3. ВСТУПЛЕНИЕ (теперь импорт виден)
                        try:
                            await client(JoinChannelRequest(channel=entity))
                            print(f"✅ [ID-RESOLVER] Читатель вступил в {ch.username}")
                        except Exception as je:
                            print(f"⚠️ [ID-RESOLVER] Ошибка вступления: {je}")

                        print(f"✅ [ID-RESOLVER] Успех! ID сохранен как: {ch.tg_id}")
                    except Exception as e:
                        print(f"❌ [ID-RESOLVER] Ошибка для {ch.username}: {e}")
                
                await session.commit()
        except Exception as e:
            print(f"⚠️ [ID-RESOLVER-LOOP] Критическая ошибка: {e}")
            
        await asyncio.sleep(60)

# --- ПУНКТ 1: ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ЛИМИТОВ ---

async def check_limits(session, worker_id, channel_id):
    """Проверяет суточные лимиты (20 на акк, 30 на канал)"""
    from database.models import DailyLimitCounter
    today = datetime.now().date()
    
    # Лимит для аккаунта
    w_res = await session.execute(select(DailyLimitCounter).where(
        DailyLimitCounter.entity_id == worker_id, 
        DailyLimitCounter.entity_type == 'worker',
        DailyLimitCounter.target_date == today
    ))
    w_count = w_res.scalar_one_or_none()
    if w_count and w_count.current_count >= 20: 
        return False
    
    # Лимит для канала
    c_res = await session.execute(select(DailyLimitCounter).where(
        DailyLimitCounter.entity_id == channel_id, 
        DailyLimitCounter.entity_type == 'channel',
        DailyLimitCounter.target_date == today
    ))
    c_count = c_res.scalar_one_or_none()
    if c_count and c_count.current_count >= 30: 
        return False
    
    return True

async def update_limit_count(session, worker_id, channel_id):
    """Обновляет счетчики после вступления"""
    from database.models import DailyLimitCounter
    today = datetime.now().date()
    for eid, etype in [(worker_id, 'worker'), (channel_id, 'channel')]:
        res = await session.execute(select(DailyLimitCounter).where(
            DailyLimitCounter.entity_id == eid, 
            DailyLimitCounter.entity_type == etype,
            DailyLimitCounter.target_date == today
        ))
        counter = res.scalar_one_or_none()
        if not counter:
            session.add(DailyLimitCounter(entity_id=eid, entity_type=etype, target_date=today, current_count=1))
        else:
            counter.current_count += 1

# --- САМ МЕНЕДЖЕР ПОДПИСОК ---

async def subscription_manager_loop():
    """Фоновый цикл: вступление/выход с рандомизацией"""
    print(f"📡 [ВОРКЕР {GROUP_TAG}] Модуль контроля подписок запущен.")
    while True:
        await asyncio.sleep(random.randint(300, 900)) 
        async with async_session() as session:
            me = await client.get_me()
            query = text("""
                SELECT tg_id, actions_config->>:tag as action 
                FROM watcher.channels 
                WHERE actions_config->>:tag IS NOT NULL 
                AND (sync_status->>:tag != 'ready' OR sync_status->>:tag IS NULL)
            """)
            res = await session.execute(query, {"tag": GROUP_TAG})
            targets = res.all()

            for ch_tg_id, action in targets:
                from database.models import WorkerSubscription
                sub_res = await session.execute(select(WorkerSubscription).where(
                    WorkerSubscription.worker_tg_id == me.id,
                    WorkerSubscription.channel_id == ch_tg_id
                ))
                sub_log = sub_res.scalar_one_or_none()

                if not sub_log:
                    sub_log = WorkerSubscription(worker_tg_id=me.id, channel_id=ch_tg_id, status='in_progress')
                    session.add(sub_log)
                    await session.commit()
                    continue

                if sub_log.status != 'in_progress':
                    continue

                if action == 'join':
                    if await check_limits(session, me.id, ch_tg_id):
                        if random.random() < 0.05:
                            try:
                                from telethon.tl.functions.channels import JoinChannelRequest
                                await client(JoinChannelRequest(channel=ch_tg_id))
                                sub_log.status = 'joined'
                                await update_limit_count(session, me.id, ch_tg_id)
                                print(f"✅ [ПОДПИСКА] Аккаунт {me.id} вступил в {ch_tg_id}")
                            except Exception as e:
                                print(f"❌ [ПОДПИСКА-ERR] {e}")
                
                elif action == 'leave':
                    try:
                        from telethon.tl.functions.channels import LeaveChannelRequest
                        await client(LeaveChannelRequest(channel=ch_tg_id))
                        sub_log.status = 'left'
                        print(f"🚪 [ВЫХОД] Аккаунт {me.id} покинул {ch_tg_id}")
                    except: pass
            await session.commit()

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
    asyncio.create_task(passport_execution_loop()) 
    asyncio.create_task(star_execution_loop())
    asyncio.create_task(resolve_channel_ids())
    asyncio.create_task(check_stars_balance_api()) 
    asyncio.create_task(check_inventory_loop())
    asyncio.create_task(subscription_manager_loop())
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
    print("🗳 [ВОРКЕР] Модуль голосований ВКЛЮЧЕН в очередь...")
    await asyncio.sleep(5) # Даем время на подключение основного клиента
    executed_reports = set()
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
                    target_emoji = str(opt_id).strip()
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
