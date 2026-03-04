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
    """Диспетчер Читателя: Мгновенно создает задачу 'КТО ПЕРВЫЙ'"""
    async with async_session() as session:
        # Используем явный SQL для скорости
        await session.execute(text("""
            INSERT INTO workers.fast_tasks (channel_id, post_id, group_tag, status, created_at)
            VALUES (:cid, :pid, :tag, 'pending', NOW())
        """), {"cid": chat_id, "pid": post_id, "tag": GROUP_TAG})
        await session.commit() # ВАЖНО: Сразу пушим в базу
    print(f"🚀 [FAST-SIGNAL] Сигнал подан для поста {post_id}!")
async def execute_button_click_raid(chat_id, post_id, msg_obj):
    """
    Пункт 2: Нажатие кнопки воркерами группы.
    Теперь просто создает запись в БД, чтобы воркеры нажали кнопку сами.
    """
    async with async_session() as session:
        # Помечаем в БД, что для этого поста нужен рейд на кнопку
        # Мы можем использовать таблицу fast_tasks или создать новую
        from sqlalchemy import insert
        await session.execute(text("""
            INSERT INTO workers.fast_tasks (channel_id, post_id, group_tag, status)
            VALUES (:cid, :pid, :tag, 'button_raid')
        """), {"cid": chat_id, "pid": post_id, "tag": GROUP_TAG})
        await session.commit()
    print(f"🔘 [BUTTON-SIGNAL] Сигнал на кнопку подан для поста {post_id}")

async def single_button_click_v2(w_client, w_id, chat_id, post_id, msg_obj, delay):
    """
    Персональный клик по кнопке с обработкой капчи.
    """
    if delay > 0:
        await asyncio.sleep(delay)
        
    try:
        # --- 1. ИЗВЛЕЧЕНИЕ КНОПКИ ---
        button = None
        if msg_obj.reply_markup and msg_obj.reply_markup.rows:
            button = msg_obj.reply_markup.rows[0].buttons[0]
        if not button: return

        url = getattr(button, 'url', None)
        
        # --- 2. ВЕБ-КАПЧА (Playwright) ---
        captcha_markers = ["verify", "captcha", "robot", "confirm", "startapp="]
        if url and any(marker in url.lower() for marker in captcha_markers):
            entity = await w_client.get_entity(chat_id)
            channel_username = entity.username if hasattr(entity, 'username') else str(chat_id)
            
            # Находим данные воркера для телефона (нужен для сессии браузера)
            async with async_session() as session:
                wrk = (await session.execute(select(WorkerAccount).where(WorkerAccount.tg_id == w_id))).scalar_one()
                w_phone = wrk.phone

            print(f"🔌 [ВЕБ-КАПЧА] Аккаунт {w_id} переходит в браузер...")
            # Важно: для Playwright ТГ-клиент лучше не отключать, если он в том же процессе, 
            # но мы используем headless, так что просто запускаем.
            success = await solve_web_captcha(w_phone, channel_username, post_id)
            print(f"{'✅' if success else '❌'} [ВЕБ] Результат аккаунта {w_id}: {success}")
            return 

        # --- 3. БОТ-КАПЧА ---
        if url and "t.me/" in url:
            bot_match = re.search(r"t.me/([\w_]+)\?start=([\w-]+)", url)
            if bot_match:
                bot_username = bot_match.group(1)
                start_param = bot_match.group(2)
                from telethon.tl.functions.messages import StartBotRequest
                await w_client(StartBotRequest(bot=bot_username, peer=bot_username, start_param=start_param))
                
                await asyncio.sleep(5) 
                async for message in w_client.iter_messages(bot_username, limit=1):
                    if message.photo:
                        photo_bytes = await w_client.download_media(message.photo, file=bytes)
                        import ddddocr
                        ocr = ddddocr.DdddOcr(show_ad=False)
                        captcha_digits = "".join(filter(str.isdigit, ocr.classification(photo_bytes)))
                        if captcha_digits:
                            await w_client.send_message(bot_username, captcha_digits)
                return

        # --- 4. ОБЫЧНЫЙ КЛИК (CALLBACK) ---
        try:
            await msg_obj.click(0)
            print(f"✅ [КНОПКА] Аккаунт {w_id} нажал успешно.")
        except: pass

    except Exception as e:
        print(f"❌ [КНОПКА-ERR] Аккаунт {w_id}: {e}")

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
async def worker_outgoing_loop(w_client, w_id):
    """Персональный цикл отправки ответов оператора"""
    while True:
        await asyncio.sleep(5)
        async with async_session() as session:
            from database.models import OutgoingMessage
            # Берем задачи ТОЛЬКО для этого воркера
            tasks = (await session.execute(select(OutgoingMessage).where(
                OutgoingMessage.worker_tg_id == w_id, 
                OutgoingMessage.status == "pending"
            ))).scalars().all()

            for task in tasks:
                try:
                    receiver = await w_client.get_input_entity(task.receiver_id)
                    await w_client.send_read_acknowledge(receiver)

                    if task.task_type == "reaction":
                        from telethon.tl.functions.messages import SendReactionRequest
                        from telethon.tl.types import ReactionEmoji
                        await w_client(SendReactionRequest(
                            peer=receiver,
                            msg_id=task.reply_to_msg_id,
                            reaction=[ReactionEmoji(emoticon=task.reaction_data)]
                        ))
                    
                    elif task.task_type == "text":
                        async with w_client.action(receiver, 'typing'):
                            await asyncio.sleep(random.randint(2, 5))
                            await w_client.send_message(receiver, task.text, reply_to=task.reply_to_msg_id)

                    elif task.task_type == "media":
                        storage_msg = await w_client.get_messages(MONITOR_STORAGE, ids=task.storage_msg_id)
                        await w_client.send_message(receiver, storage_msg, reply_to=task.reply_to_msg_id)

                    task.status = "sent"
                except Exception as e:
                    print(f"❌ [ОШИБКА CRM] Аккаунт {w_id}: {e}")
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
# --- ПУНКТ 3: ДЕСАНТ УДАЧИ (РЕЙДЫ) ---
async def worker_luck_raid_loop(w_client, w_id):
    """Персональный цикл участия в рейдах удачи"""
    while True:
        await asyncio.sleep(15) 
        async with async_session() as session:
            from database.models import LuckRaid
            active_raids = (await session.execute(select(LuckRaid).where(LuckRaid.status == "active"))).scalars().all()

            for raid in active_raids:
                # Шанс 30%, что именно ЭТОТ воркер вступит в рейд в этом цикле
                if random.random() > 0.3: continue

                try:
                    delay = random.randint(10, 60)
                    await asyncio.sleep(delay)

                    if raid.emoji in ['🎰', '🎯', '🎲', '🏀', '⚽️', '🎳']:
                        from telethon.tl.types import InputMediaDice
                        await w_client.send_message(
                            raid.channel_id,
                            file=InputMediaDice(raid.emoji),
                            comment_to=raid.post_id
                        )
                    else:
                        await w_client.send_message(raid.channel_id, raid.emoji, comment_to=raid.post_id)
                    print(f"✅ [РЕЙД] Аккаунт {w_id} высадился в пост {raid.post_id}")
                except: pass
# --- ЛОГИКА ВЫПОЛНЕНИЯ ЗАДАЧ ИЗ ПАСПОРТА (Пункт 1) ---

async def passport_execution_loop():
    """Диспетчер Читателя: закрывает паспорта, когда время вышло"""
    print(f"⚙️ [ДИСПЕТЧЕР {GROUP_TAG}] Контроль дедлайнов активен.")
    while True:
        await asyncio.sleep(60) 
        async with async_session() as session:
            # Ищем активные паспорта
            query = select(ContestPassport).where(ContestPassport.status == "active")
            active_passports = (await session.execute(query)).scalars().all()

            for passport in active_passports:
                # Считаем общее время: кол-во акков * интервал
                # Добавляем 20% запаса на рандомные паузы
                intensity_map = {1: 1200, 2: 600, 3: 300, 4: 60}
                slot = intensity_map.get(passport.intensity_level, 600)
                
                # Примерный расчет: если паспорт создан более чем (акки * слот * 1.2) секунд назад
                # Мы его закрываем. 
                # (Для простоты можно закрывать, если с момента создания прошло 24 часа)
                if passport.id in ACTIVE_TASKS_CACHE: # Если он вообще запускался
                     # Здесь можно добавить проверку по времени, но пока оставим ручное или 
                     # логику "все воркеры отписались"
                     pass
        await session.commit()

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

async def check_stars_balance_api(w_client, w_id):
    """Персональная проверка баланса звезд для конкретного воркера"""
    print(f"💰 [ЭКОНОМИКА] Контроль звезд запущен для аккаунта {w_id}.")
    
    while True:
        now = datetime.now()
        # ОПРЕДЕЛЯЕМ ОКНО (00-12 или 12-24)
        if now.hour < 12:
            current_window_end = now.replace(hour=12, minute=0, second=0, microsecond=0)
        else:
            current_window_end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

        async with async_session() as session:
            res = await session.execute(select(WorkerAccount).where(WorkerAccount.tg_id == w_id))
            worker = res.scalar_one_or_none()
            
            if not worker or worker.last_check_window_end == current_window_end:
                # Если уже проверяли в этом окне — спим до следующего + запас
                wait = (current_window_end - now).total_seconds() + random.randint(60, 300)
                await asyncio.sleep(wait)
                continue

            # Рандомное ожидание внутри окна (мимикрия)
            seconds_remaining = (current_window_end - now).total_seconds()
            await asyncio.sleep(random.uniform(0, min(seconds_remaining, 3600))) # Ждем до 1 часа макс для тестов

            try:
                from telethon.tl.functions.payments import GetStarsStatusRequest
                stars_status = await w_client(GetStarsStatusRequest(peer='me'))
                current_balance = int(stars_status.balance.amount) 
                is_ready = current_balance >= 40

                await session.execute(
                    update(WorkerAccount)
                    .where(WorkerAccount.tg_id == w_id)
                    .values(
                        stars_balance=current_balance,
                        is_financial_ready=is_ready,
                        last_check_window_end=current_window_end
                    )
                )
                await session.commit()
                print(f"✅ [БАЛАНС] Аккаунт {w_id}: {current_balance} ⭐. Готовность: {is_ready}")

            except Exception as e:
                print(f"❌ [БАЛАНС-ERR] Аккаунт {w_id}: {e}")
                await asyncio.sleep(600)

async def check_inventory_loop(w_id, w_phone):
    """Персональная проверка подарков (Web Playwright)"""
    print(f"📦 [ЭКОНОМИКА] Модуль инвентаря запущен для аккаунта {w_id}.")
    
    while True:
        now = datetime.now()
        # Аналогичная логика окон 00-12 / 12-24
        if now.hour < 12:
            current_window_end = now.replace(hour=12, minute=0, second=0, microsecond=0)
        else:
            current_window_end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

        async with async_session() as session:
            res = await session.execute(select(WorkerAccount).where(WorkerAccount.tg_id == w_id))
            worker = res.scalar_one_or_none()
            
            if not worker or worker.last_inventory_check_window_end == current_window_end:
                await asyncio.sleep(600)
                continue

            # Спим рандомно перед запуском тяжелого браузера
            await asyncio.sleep(random.randint(120, 1800))

            try:
                # ВЫЗЫВАЕМ ТВОЮ ФУНКЦИЮ ИЗ ПРОШЛЫХ ЧАСТЕЙ
                # Убедись, что process_gifts_inventory определена в файле
                await process_gifts_inventory(w_phone)
                
                await session.execute(
                    update(WorkerAccount).where(WorkerAccount.tg_id == w_id)
                    .values(last_inventory_check_window_end=current_window_end)
                )
                await session.commit()
                print(f"✅ [ИНВЕНТАРЬ] Аккаунт {w_id} очистил подарки.")
            except Exception as e:
                print(f"❌ [ИНВЕНТАРЬ-ERR] Аккаунт {w_id}: {e}")
                await asyncio.sleep(300)

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
            tasks.append(asyncio.create_task(execute_single_worker_tasks_v2(lead, passport, is_lead=True)))
    else:
        # Для АФК создаем задачи для всех воркеров
        for i, worker in enumerate(workers):
            wait_for_slot = i * slot_duration
            tasks.append(asyncio.create_task(delayed_worker_execution_v2(worker, passport, wait_for_slot, slot_duration)))
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
async def delayed_worker_execution_v2(w_client, w_id, passport, wait_time, slot_limit):
    """Ждет свою очередь в эстафете (Пункт 4: Единая интенсивность)"""
    print(f"⏳ [ОЧЕРЕДЬ] Аккаунт {w_id} начнет через {int(wait_time/60)} мин.")
    
    # 1. Основное ожидание по очереди
    await asyncio.sleep(wait_time)
    
    # 2. Рандомное смещение внутри своего слота (мимикрия)
    intra_slot = random.randint(5, int(slot_limit * 0.7))
    await asyncio.sleep(intra_slot)
    
    # 3. ЗАПУСК ВЫПОЛНЕНИЯ
    await execute_single_worker_tasks_v2(w_client, w_id, passport)

async def get_and_join_chat(w_client, w_id, channel_id, post_id):
    """
    Пункт 2: Проверяет чат обсуждения, вступает в него и обновляет БД.
    """
    try:
        # 1. Получаем актуальный ID чата обсуждения для конкретного поста
        from telethon.tl.functions.messages import GetDiscussionMessageRequest
        result = await w_client(GetDiscussionMessageRequest(peer=channel_id, msg_id=post_id))
        
        # Если чата нет (комменты закрыты) — выходим
        if not result or not result.chats:
            return None
            
        new_chat_id = result.chats[0].id
        # Приводим к формату -100...
        formatted_new_id = int(f"-100{abs(new_chat_id)}")

        async with async_session() as session:
            # 2. Проверяем, какой чат записан в БД для этого канала
            res = await session.execute(select(TargetChannel).where(TargetChannel.tg_id == channel_id))
            channel_db = res.scalar_one_or_none()
            
            old_chat_id = channel_db.comment_chat_id if channel_db else None

            # 3. Если чат в базе не совпадает с актуальным — выходим из старого
            if old_chat_id and old_chat_id != formatted_new_id:
                try:
                    from telethon.tl.functions.channels import LeaveChannelRequest
                    await w_client(LeaveChannelRequest(channel=old_chat_id))
                    print(f"🚪 [ЧАТ] Аккаунт {w_id} вышел из старого чата {old_chat_id}")
                except: pass # Если чат уже удален или мы не там

                # Обновляем ID чата в БД (только один раз, кто первый успел)
                channel_db.comment_chat_id = formatted_new_id
                await session.commit()

            # 4. Проверяем, состоит ли воркер в НОВОМ чате (через логи subscriptions)
            from database.models import WorkerSubscription
            sub_res = await session.execute(select(WorkerSubscription).where(
                WorkerSubscription.worker_tg_id == w_id,
                WorkerSubscription.channel_id == formatted_new_id
            ))
            sub_log = sub_res.scalar_one_or_none()

            if not sub_log or sub_log.status != 'joined':
                try:
                    from telethon.tl.functions.channels import JoinChannelRequest
                    await w_client(JoinChannelRequest(channel=formatted_new_id))
                    
                    # Фиксируем вступление в логах
                    if not sub_log:
                        sub_log = WorkerSubscription(worker_tg_id=w_id, channel_id=formatted_new_id, status='joined')
                        session.add(sub_log)
                    else:
                        sub_log.status = 'joined'
                    
                    # Если мы только что узнали ID чата (в базе было пусто) — записываем его
                    if not old_chat_id:
                        channel_db.comment_chat_id = formatted_new_id
                    
                    await session.commit()
                    print(f"✅ [ЧАТ] Аккаунт {w_id} вступил в чат обсуждения {formatted_new_id}")
                except Exception as e:
                    print(f"❌ [ЧАТ-ERR] Ошибка вступления {w_id}: {e}")
                    return None
                    
        return formatted_new_id
    except Exception as e:
        print(f"⚠️ [ЧАТ-INFO] У поста {post_id} нет чата обсуждения или ошибка: {e}")
        return None

# --- ОБНОВЛЕННАЯ ФУНКЦИЯ С БЛОКОМ КОММЕНТАРИЕВ ---
async def execute_single_worker_tasks_v2(w_client, w_id, passport, is_lead=False):
    """
    ПОЛНАЯ ВЕРСИЯ: Выполняет все задачи из паспорта через личный клиент воркера.
    Используется для АФК и для регистрации ЛИДА в голосованиях.
    """
    conds = passport.conditions
    actions = conds.get("selected", [])
    target_chat = conds.get("source_tg_id")
    target_msg = conds.get("source_msg_id")
    
    # Список фраз для имитации (можно расширить)
    COMMON_PHRASES = ["участвую", "+", "го", "хочу приз", "удачи всем", "🍀", "надеюсь на победу", "🔥"]

    try:
        # Перемешиваем действия для беспалевности
        random.shuffle(actions)
        
        for action in actions:
            # Пауза между разными действиями (подписка -> реакция и т.д.)
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
                    print(f"✅ [РЕАКЦИЯ] Аккаунт {w_id} поставил эмодзи.")
                except: pass

            # 3. РЕПОСТ
            elif action == "repost" and target_chat and target_msg:
                count = int(conds.get("repost_count", 1))
                # Вызываем обновленную функцию репостов (её тоже нужно будет поправить под w_client)
                await perform_network_reposts_v2(w_client, w_id, target_chat, target_msg, count)

            # 4. КОММЕНТАРИЙ
            elif action == "comm" and target_chat and target_msg:
                try:
                    # 1. Проверка/Вступление в чат
                    await get_and_join_chat(w_client, w_id, target_chat, target_msg)
                    
                    # 2. Отправка ОДНОГО комментария
                    await w_client.send_message(
                        target_chat, 
                        random.choice(COMMON_PHRASES), 
                        comment_to=target_msg
                    )
                    print(f"✅ [КОММЕНТ] Аккаунт {w_id} отписал в пост.")
                except Exception as e:
                    print(f"❌ [КОММЕНТ-ERR] Аккаунт {w_id}: {e}")


        # 5. НАЖАТИЕ КНОПКИ (Если АФК)
        if passport.type == "afk" and target_chat and target_msg:
            try:
                msg_obj = await w_client.get_messages(target_chat, ids=target_msg)
                if msg_obj and msg_obj.reply_markup:
                    # Важно: вызываем функцию клика, передавая w_client
                    await single_button_click_v2(w_client, w_id, target_chat, target_msg, msg_obj, 0)
            except Exception as e:
                print(f"❌ [КНОПКА-ERR] Аккаунт {w_id}: {e}")

        # 6. РЕГИСТРАЦИЯ ЛИДА (Если Голосование)
        if is_lead:
            details = conds.get("vote_details", {})
            place = details.get("reg_place", "")
            content = details.get("reg_data", "")
            media_id = details.get("reg_media_id")
            
            target = place.replace("ЛС ", "").replace("@", "")
            
            msg_to_send = content
            if media_id:
                # Берем медиа из хранилища через личный клиент
                storage_msg = await w_client.get_messages(MONITOR_STORAGE, ids=media_id)
                msg_to_send = storage_msg 

            if "Комментарии" in place:
                await w_client.send_message(target_chat, msg_to_send, comment_to=target_msg)
            else:
                await w_client.send_message(target, msg_to_send)
            print(f"✅ [ЛИД-РЕГА] Аккаунт {w_id} отправил заявку в {place}")

    except Exception as e:
        print(f"❌ [ИСПОЛНИТЕЛЬ-ERR] Аккаунт {w_id} упал: {e}")

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

async def perform_network_reposts_v2(w_client, w_id, chat_id, msg_id, count):
    """
    Репостит сообщение другим воркерам ГРУППЫ.
    w_client: личный клиент воркера, который делает репост.
    """
    async with async_session() as session:
        # 1. Ищем, кому из СВОЕЙ группы переслать (кроме себя)
        res = await session.execute(
            select(WorkerAccount.tg_id).where(
                WorkerAccount.group_tag == GROUP_TAG,
                WorkerAccount.tg_id != w_id,
                WorkerAccount.is_alive == True
            ).order_by(func.random()).limit(count)
        )
        targets = res.scalars().all()
        
        # 2. Если воркеров в группе мало — репостим себе в Saved Messages
        if len(targets) < count:
            try:
                await w_client.forward_messages('me', msg_id, chat_id)
                count -= 1 
            except: pass

        # 3. Рассылаем остаток по воркерам
        for target_id in targets:
            if count <= 0: break
            try:
                await asyncio.sleep(random.randint(3, 7)) # Пауза 'чтения'
                await w_client.forward_messages(target_id, msg_id, chat_id)
                count -= 1
                print(f"✅ [РЕПОСТ] Аккаунт {w_id} переслал пост воркеру {target_id}")
            except: pass

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

async def worker_star_gift_loop(w_id, w_phone):
    """Персональный цикл подарков: воркер проверяет только СВОИ одобренные рапорты"""
    print(f"⭐ [ВОРКЕР {w_id}] Модуль подарков (WEB) активен.")
    
    # Кэш для защиты от повторных запусков внутри одного воркера
    local_gift_cache = set()

    while True:
        await asyncio.sleep(60)
        try:
            async with async_session() as session:
                # Ищем одобренные рапорты, где ИМЕННО ЭТОТ воркер назначен исполнителем
                query = select(StarReport).where(
                    StarReport.status == 'approved',
                    StarReport.executor_id == w_id
                )
                reports = (await session.execute(query)).scalars().all()

                for report in reports:
                    if report.id in local_gift_cache: continue
                    
                    local_gift_cache.add(report.id)
                    print(f"💰 [WEB-PROCESS] Аккаунт {w_id} приступает к рапорту #{report.id}...")
                    
                    # Вызываем отправку через браузер, передавая телефон именно этого воркера
                    success = await send_gift_via_web(str(w_phone), report.target_user, report.method)
                    
                    # Обновляем статус рапорта
                    new_status = "completed" if success else "error"
                    await session.execute(
                        update(StarReport).where(StarReport.id == report.id).values(status=new_status)
                    )
                    await session.commit()
                    
                    local_gift_cache.discard(report.id)
        except Exception as e:
            print(f"⚠️ [WEB-LOOP-ERR] Аккаунт {w_id}: {e}")

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
    
    # 1. Лимит для конкретного АККАУНТА (воркера)
    w_res = await session.execute(select(DailyLimitCounter).where(
        DailyLimitCounter.entity_id == worker_id, 
        DailyLimitCounter.entity_type == 'worker',
        DailyLimitCounter.target_date == today
    ))
    w_count = w_res.scalar_one_or_none()
    if w_count and w_count.current_count >= 20: 
        return False
    
    # 2. Лимит для КАНАЛА
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
    """Обновляет счетчики после успешного вступления"""
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

async def subscription_manager_loop(w_client, w_id):
    """Персональный цикл вступления/выхода для конкретного воркера"""
    print(f"📡 [ПОДПИСКИ] Аккаунт {w_id} начал мониторинг задач на вступление.")
    
    while True:
        # Проверка раз в 10-20 минут
        await asyncio.sleep(random.randint(600, 1200)) 
        
        async with async_session() as session:
            # Ищем каналы, где нашей группе (A1) приказано 'join' или 'leave'
            # И где этот воркер (w_id) еще не завершил действие
            query = text("""
                SELECT c.tg_id, c.actions_config->>:tag as action 
                FROM watcher.channels c
                WHERE c.actions_config->>:tag IS NOT NULL 
                AND (c.sync_status->>:tag != 'ready' OR c.sync_status->>:tag IS NULL)
            """)
            res = await session.execute(query, {"tag": GROUP_TAG})
            targets = res.all()

            for ch_tg_id, action in targets:
                from database.models import WorkerSubscription
                
                # Проверяем статус в логах именно для этого воркера
                sub_res = await session.execute(select(WorkerSubscription).where(
                    WorkerSubscription.worker_tg_id == w_id,
                    WorkerSubscription.channel_id == ch_tg_id
                ))
                sub_log = sub_res.scalar_one_or_none()

                # Если записи нет — создаем её (in_progress)
                if not sub_log:
                    sub_log = WorkerSubscription(worker_tg_id=w_id, channel_id=ch_tg_id, status='in_progress')
                    session.add(sub_log)
                    await session.commit()
                    continue

                if sub_log.status != 'in_progress':
                    continue

                # ЛОГИКА ВСТУПЛЕНИЯ
                if action == 'join':
                    # 1. Проверяем лимиты
                    if await check_limits(session, w_id, ch_tg_id):
                        # 2. Рандомный шанс (имитация распределения на 24 часа)
                        if random.random() < 0.05:
                            try:
                                from telethon.tl.functions.channels import JoinChannelRequest
                                await w_client(JoinChannelRequest(channel=ch_tg_id))
                                
                                sub_log.status = 'joined'
                                await update_limit_count(session, w_id, ch_tg_id)
                                print(f"✅ [ПОДПИСКА] Аккаунт {w_id} успешно вступил в {ch_tg_id}")
                            except Exception as e:
                                print(f"❌ [ПОДПИСКА-ERR] Аккаунт {w_id}: {e}")
                
                elif action == 'leave':
                    try:
                        from telethon.tl.functions.channels import LeaveChannelRequest
                        # 1. Выходим из канала
                        await w_client(LeaveChannelRequest(channel=ch_tg_id))
                        
                        # 2. Выходим из связанного чата (Пункт 2 ТЗ)
                        async with async_session() as session_ch:
                            res_ch = await session_ch.execute(select(TargetChannel).where(TargetChannel.tg_id == ch_tg_id))
                            ch_data = res_ch.scalar_one_or_none()
                            if ch_data and ch_data.comment_chat_id:
                                try:
                                    await w_client(LeaveChannelRequest(channel=ch_data.comment_chat_id))
                                    print(f"🚪 [ВЫХОД] Аккаунт {w_id} покинул чат {ch_data.comment_chat_id}")
                                except: pass
                        
                        sub_log.status = 'left'
                    except Exception as e:
                        print(f"❌ [LEAVE-ERR] {w_id} не смог выйти: {e}")
                        sub_log.status = 'left' # Помечаем как выполненное, чтобы не зациклиться
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
    # 4. Регистрация обработчика и запуск фонового обновления
    client.add_event_handler(handler, events.NewMessage())
    asyncio.create_task(data_refresher())
    print(f"🚀 Система онлайн. Слов: {len(KEYWORDS_DATA)}, Каналов: {len(CHANNELS_MAP)}")
        # Запускаем "руки" в фоновом режиме
    asyncio.create_task(worker_outgoing_loop())
        # Запускаем десант в фоновом режиме
    asyncio.create_task(worker_luck_raid_loop())
    asyncio.create_task(worker_mention_task_loop())
    asyncio.create_task(passport_execution_loop()) 
    asyncio.create_task(resolve_channel_ids())
    asyncio.create_task(check_stars_balance_api()) 
    asyncio.create_task(check_inventory_loop())
    asyncio.create_task(subscription_manager_loop())
    await client.run_until_disconnected()
# --- ПУНКТ 3: ЗЕРКАЛО ЛС (ПРИЕМ СООБЩЕНИЙ) ---
# --- ПУНКТ 3: ЗЕРКАЛО ЛС (ПЕРСОНАЛЬНОЕ ДЛЯ КАЖДОГО ВОРКЕРА) ---

async def start_private_mirror(w_client, w_id):
    """Регистрирует слушателя ЛС для конкретного воркера"""
    
    @w_client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def handler_ls(event):
        sender = await event.get_sender()
        # Игнорируем ботов
        if sender and hasattr(sender, 'bot') and sender.bot: 
            return 
            
        msg_obj = event.message
        m_type = "text"
        s_media_id = None
        
        # Если есть медиа — пересылаем в MONITOR_STORAGE
        if msg_obj.photo or msg_obj.voice or msg_obj.video or msg_obj.document:
            try:
                fwd = await msg_obj.forward_to(MONITOR_STORAGE)
                s_media_id = fwd.id
                m_type = "photo" if msg_obj.photo else "media"
            except Exception as e:
                print(f"❌ [ЛС-ЗЕРКАЛО] Ошибка медиа: {e}")
        
        # Сохраняем в БД именно для этого воркера (w_id)
        async with async_session() as session_msg:
            from database.models import AccountMessage
            new_msg = AccountMessage(
                msg_id=msg_obj.id,
                worker_tg_id=w_id,        # ПРАВИЛЬНЫЙ ID ВОРКЕРА
                sender_id=event.sender_id,
                text=msg_obj.message or f"[{m_type.upper()}]",
                media_type=m_type,
                storage_media_id=s_media_id,
                is_read=False
            )
            session_msg.add(new_msg)
            await session_msg.commit()
            
        print(f"📩 [ЛС] Аккаунт {w_id} получил сообщение от {event.sender_id}")

async def execute_vote_task(w_client, w_id, r_id, msg_id, chat_id, v_type, opt_id, intensity):
    """Выполняет один рапорт голосования (накрутку) через личный клиент"""
    
    # 1. Расчет задержки по интенсивности
    delay_map = {1: 600, 2: 300, 3: 120, 4: 30}
    max_delay = delay_map.get(intensity, 60)
    
    # Рандомная пауза перед действием (имитация чтения)
    await asyncio.sleep(random.randint(10, max_delay))
    
    try:
        target_emoji = str(opt_id).strip()
        
        if v_type == "poll":
            from telethon.tl.functions.messages import SendVoteRequest
            msg_data = await w_client.get_messages(chat_id, ids=msg_id)
            
            if msg_data and msg_data.poll:
                try:
                    idx = int(target_emoji) - 1
                    if idx < 0: idx = 0
                    poll_answers = msg_data.poll.poll.answers
                    
                    if idx < len(poll_answers):
                        chosen_option_id = poll_answers[idx].option
                        await w_client(SendVoteRequest(
                            peer=chat_id,
                            msg_id=msg_id,
                            options=[chosen_option_id]
                        ))
                        print(f"✅ [ГОЛОС] Аккаунт {w_id} проголосовал в опросе #{r_id}")
                except: pass
        
        else: # РЕАКЦИИ
            from telethon.tl.functions.messages import SendReactionRequest
            from telethon.tl.types import ReactionEmoji, ReactionCustomEmoji
            
            if target_emoji.isdigit():
                reaction_obj = [ReactionCustomEmoji(document_id=int(target_emoji))]
            else:
                reaction_obj = [ReactionEmoji(emoticon=target_emoji)]

            await w_client(SendReactionRequest(
                peer=chat_id,
                msg_id=msg_id,
                reaction=reaction_obj
            ))
            print(f"✅ [РЕАКЦИЯ] Аккаунт {w_id} поставил накрутку в рапорт #{r_id}")
            
    except Exception as e:
        print(f"❌ [ГОЛОС-ERR] Аккаунт {w_id} (Рапорт {r_id}): {e}")

async def worker_contest_execution_loop(w_client, w_id):
    """Персональный цикл выполнения конкурсов и голосований для воркера"""
    print(f"🛠 [ВОРКЕР {w_id}] Модуль исполнения задач запущен.")
    
    # Кэш выполненных задач, чтобы не делать одно и то же в одном цикле
    processed_tasks = set()

    while True:
        await asyncio.sleep(30)
        async with async_session() as session:

            # 1. ПРОВЕРКА ПАСПОРТОВ (АФК / ЛИД-РЕГА)
            p_query = select(ContestPassport).where(
                ContestPassport.participating_groups.contains([GROUP_TAG]),
                ContestPassport.status == "active"
            )
            active_passports = (await session.execute(p_query)).scalars().all()

            for passport in active_passports:
                task_key = f"pass_{passport.id}"
                if task_key in processed_tasks: continue

                # РАСЧЕТ ЕДИНОЙ ОЧЕРЕДИ (Пункт 4)
                intensity_map = {1: 1200, 2: 600, 3: 300, 4: 60}
                slot_duration = intensity_map.get(passport.intensity_level, 600)
                
                # Собираем ВСЕХ воркеров участвующих групп для расчета тайминга
                res_all = await session.execute(
                    select(WorkerAccount.tg_id).where(
                        WorkerAccount.group_tag.in_(passport.participating_groups),
                        WorkerAccount.is_alive == True
                    ).order_by(WorkerAccount.id)
                )
                all_worker_ids = [r[0] for r in res_all.all()]

                if w_id in all_worker_ids:
                    my_index = all_worker_ids.index(w_id)
                    # Если тип 'vote', действие делает только Лид
                    if passport.type == "vote":
                        lead_id = passport.conditions.get("vote_details", {}).get("executor")
                        if str(w_id) == str(lead_id):
                            await execute_single_worker_tasks_v2(w_client, w_id, passport, is_lead=True)
                            processed_tasks.add(task_key)
                    else:
                        # АФК эстафета
                        wait_time = my_index * slot_duration
                        # Запускаем в фоне, чтобы не тормозить цикл
                        asyncio.create_task(delayed_worker_execution_v2(w_client, w_id, passport, wait_time, slot_duration))
                        processed_tasks.add(task_key)

            # 2. ПРОВЕРКА РАПОРТОВ ГОЛОСОВАНИЯ (НАКРУТКА)
            v_query = text("""
                SELECT id, target_msg_id, target_chat_id, vote_type, option_id, intensity, accounts_count
                FROM management.voting_reports
                WHERE status = 'approved' AND target_groups::jsonb @> :tag_json::jsonb
            """)
            v_res = await session.execute(v_query, {"tag_json": f'["{GROUP_TAG}"]'})
            for r_id, msg_id, chat_id, v_type, opt_id, intensity, acc_limit in v_res.all():
                task_key = f"vote_rep_{r_id}"
                if task_key in processed_tasks: continue
                
                # Логика очереди для накрутки аналогична АФК
                # ... (здесь будет вызов голосования через w_client)
                await execute_vote_task(w_client, w_id, r_id, msg_id, chat_id, v_type, opt_id, intensity)
                processed_tasks.add(task_key)
async def worker_fast_responder_loop(w_client, w_id):
    """
    Сверхбыстрый цикл (1с): 
    Персональный перехват задач 'Кто первый' и 'Рейд на кнопку'.
    """
    print(f"⚡️ [FAST-ENGINE] Аккаунт {w_id} включен в режим перехвата (1с).")
    
    FAST_PHRASES = ["+", ".", "!", "участвую", "тут", "я"]

    while True:
        # Проверка базы каждую секунду для мгновенной реакции
        await asyncio.sleep(1) 
        
        try:
            async with async_session() as session:
                # 1. АТОМАРНЫЙ ПОИСК СВОБОДНОЙ ЗАДАЧИ
                # FOR UPDATE SKIP LOCKED позволяет 30 воркерам не мешать друг другу
                find_q = text("""
                    SELECT id, channel_id, post_id, status 
                    FROM workers.fast_tasks 
                    WHERE group_tag = :tag 
                    AND status IN ('pending', 'button_raid') 
                    AND created_at > NOW() - INTERVAL '1 minute'
                    LIMIT 1 
                    FOR UPDATE SKIP LOCKED
                """)
                
                res = await session.execute(find_q, {"tag": GROUP_TAG})
                row = res.first()

                if not row:
                    continue

                f_id, f_cid, f_pid, f_status = row

                # 2. МГНОВЕННЫЙ ЗАХВАТ (Помечаем как 'completed' до выполнения, чтобы не было дублей)
                await session.execute(
                    text("UPDATE workers.fast_tasks SET status = 'completed' WHERE id = :tid"),
                    {"tid": f_id}
                )
                await session.commit() # Фиксируем захват в БД

                # 3. ВЫПОЛНЕНИЕ ЗАДАЧИ
                
                # ЛОГИКА: КЛЮЧЕВОЕ СЛОВО 'ПЕРВЫЙ'
                                # --- ЛОГИКА 1: КЛЮЧЕВОЕ СЛОВО (Пункт 9) ---
                if f_status == 'pending':
                    try:
                        # 1. Сначала вступаем в чат
                        await get_and_join_chat(w_client, w_id, f_cid, f_pid)
                        
                        # 2. Потом отправляем ОДИН быстрый комментарий
                        await w_client.send_message(
                            f_cid, 
                            random.choice(FAST_PHRASES), 
                            comment_to=f_pid
                        )
                        print(f"🚀 [FAST-WIN] Аккаунт {w_id} УСПЕЛ ПЕРВЫМ в пост {f_pid}!")
                    except Exception as e:
                        print(f"❌ [FAST-ERR] Аккаунт {w_id} не смог отправить текст: {e}")

                # ЛОГИКА: РЕЙД НА КНОПКУ
                elif f_status == 'button_raid':
                    try:
                        # Получаем сообщение для клика
                        msg_obj = await w_client.get_messages(f_cid, ids=f_pid)
                        if msg_obj:
                            # Случайная микро-задержка для имитации человека (1-5 сек)
                            delay = random.randint(1, 5)
                            await single_button_click_v2(w_client, w_id, f_cid, f_pid, msg_obj, delay)
                    except Exception as e:
                        print(f"❌ [BUTTON-RAID-ERR] Аккаунт {w_id} в посте {f_pid}: {e}")

        except Exception as e:
            # Игнорируем ошибки сессии, чтобы цикл не прерывался
            continue

# --- ФУНКЦИЯ ЗАПУСКА ИНСТАНСА (Для каждого воркера свой мир) ---
async def run_worker_instance(w_data):
    """Запускает индивидуальный клиент и все его циклы"""
    w_id = w_data.tg_id
    
    # Создаем персональный клиент
    w_client = TelegramClient(
        StringSession(w_data.session_string), 
        w_data.api_id, w_data.api_hash,
        device_model=w_data.device_model,
        system_version=w_data.os_version,
        app_version=w_data.app_version
    )
    
    try:
        await w_client.connect()
        if not await w_client.is_user_authorized():
            print(f"⚠️ [ВОРКЕР {w_id}] Не авторизован! Пропуск.")
            return

        # 1. ВКЛЮЧАЕМ СЛУШАТЕЛЯ ЛС (Зеркало)
        await start_private_mirror(w_client, w_id)

        # 2. ЗАПУСКАЕМ ВСЕ ПЕРСОНАЛЬНЫЕ ЦИКЛЫ
        tasks = [
            asyncio.create_task(subscription_manager_loop(w_client, w_id)),
            asyncio.create_task(check_stars_balance_api(w_client, w_id)),
            asyncio.create_task(check_inventory_loop(w_id, w_data.phone)),
            asyncio.create_task(worker_outgoing_loop(w_client, w_id)),
            asyncio.create_task(worker_luck_raid_loop(w_client, w_id)),
            asyncio.create_task(worker_contest_execution_loop(w_client, w_id)),
            asyncio.create_task(worker_star_gift_loop(w_id, w_data.phone)),
            asyncio.create_task(worker_fast_responder_loop(w_client, w_id)),
            # Добавь сюда остальные циклы, если они есть (например, mention_loop)
        ]
        
        print(f"✅ [ВОРКЕР {w_id}] Все модули активны.")
        
        # Держим соединение этого воркера
        await w_client.run_until_disconnected()
        
    except Exception as e:
        print(f"❌ [ВОРКЕР {w_id}] Критическая ошибка: {e}")
    finally:
        if w_client.is_connected():
            await w_client.disconnect()

# --- ГЛАВНАЯ ФУНКЦИЯ (ОРКЕСТРАТОР) ---

async def main():
    global client, KEYWORDS_DATA, MY_WORKERS, CHANNELS_MAP

    print(f"📡 [ГРУППА {GROUP_TAG}] Полный запуск системы...")

    # 1. ЗАПУСК ЧИТАТЕЛЯ (Глобальный мониторинг)
    acc = await get_reader_from_db(GROUP_TAG)
    if not acc:
        print(f"❌ Читатель для {GROUP_TAG} не найден!")
        return

    client = TelegramClient(
        StringSession(acc.session_string), 
        acc.api_id, acc.api_hash,
        device_model=acc.device_model,
        system_version=acc.os_version,
        app_version=acc.app_version
    )
    await client.start()
    
    # Загружаем кэш данных
    KEYWORDS_DATA, MY_WORKERS, CHANNELS_MAP = await load_all_data()
    
    # Обработчик постов (Только читатель видит каналы!)
    client.add_event_handler(handler, events.NewMessage())
    
    # Фоновые задачи читателя
    asyncio.create_task(data_refresher())
    asyncio.create_task(resolve_channel_ids())
    asyncio.create_task(passport_execution_loop()) # Двигатель паспортов (один на группу)

    # 2. ЗАПУСК ВСЕХ ВОРКЕРОВ ГРУППЫ
    async with async_session() as session:
        res = await session.execute(
            select(WorkerAccount).where(
                WorkerAccount.group_tag == GROUP_TAG, 
                WorkerAccount.is_alive == True
            )
        )
        workers_list = res.scalars().all()
        
    print(f"🚀 [ОРКЕСТРАТОР] Найдено {len(workers_list)} живых воркеров. Запуск...")

    for w_data in workers_list:
        # Запускаем каждого воркера как отдельную задачу
        asyncio.create_task(run_worker_instance(w_data))
        # Спим 3 секунды между входами, чтобы не словить бан за массовый логин
        await asyncio.sleep(3) 

    print(f"✨ [СИСТЕМА] Группа {GROUP_TAG} полностью развернута. Мониторинг активен.")
    
    # Основной цикл держит Читатель
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Мониторинг остановлен пользователем.")
