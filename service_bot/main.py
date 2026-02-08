import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from decouple import config
from sqlalchemy import select, update, func, text  # <-- Добавь func сюда
from datetime import datetime

# Импорты из твоего проекта
from database.config import async_session
from database.models import Operator, PotentialPost, ContestPassport, VotingReport, TargetChannel
from service_bot.states import ContestForm

# Настройки
BOT_TOKEN = config('BOT_TOKEN')
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
TARGET_GROUP = -1003723379200 
MONITOR_STORAGE = -1003753624654
# --- ФУНКЦИИ БАЗЫ ДАННЫХ ---

async def get_operator(tg_id: int):
    """Проверка оператора в БД"""
    async with async_session() as session:
        result = await session.execute(
            select(Operator).where(Operator.tg_id == tg_id)
        )
        return result.scalars().first()

def get_conditions_kb(selected_conditions: list):
    builder = InlineKeyboardBuilder()
    options = {
        "sub": "Подписка 📢",
        "reac": "Реакция 👍",
        "comm": "Комментарий 💬",
        "repost": "Репост 🔄"
    }
    for code, name in options.items():
        mark = " ✅" if code in selected_conditions else ""
        builder.row(types.InlineKeyboardButton(
            text=f"{name}{mark}", 
            callback_data=f"cond_{code}"
        ))
    builder.row(types.InlineKeyboardButton(
        text="➡️ Далее (Дедлайн)", 
        callback_data="cond_done"
    ))
    return builder.as_markup()

def get_intensity_kb():
    builder = InlineKeyboardBuilder()
    levels = {
        "1": "1ур (1акк/20мин)",
        "2": "2ур (1акк/10мин)",
        "3": "3ур (1акк/5мин)",
        "4": "4ур (1акк/1мин)"
    }
    for k, v in levels.items():
        builder.row(types.InlineKeyboardButton(text=v, callback_data=f"int_{k}"))
    return builder.as_markup()

async def get_next_post(group_tag: str):
    async with async_session() as session:
        query = select(PotentialPost).where(
            PotentialPost.group_tag == group_tag,
            PotentialPost.is_claimed == False,
            PotentialPost.post_type != "monitoring" # СТРОГО ИГНОРИМ ЗЕРКАЛО
        ).order_by(PotentialPost.id.asc()).limit(1)
        
        result = await session.execute(query)
        return result.scalars().first()




# --- ОБРАБОТЧИКИ КОМАНД ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    op = await get_operator(message.from_user.id)
    
    if not op:
        await message.answer("❌ Доступ запрещен. Вас нет в списке операторов.")
        return

    # Создаем кнопки
    kb = [
        [types.KeyboardButton(text="📥 Получить новый пост")],
        [types.KeyboardButton(text="📋 Текущие конкурсы")],  # <--- КНОПКА ТУТ
        [types.KeyboardButton(text="🔍 Узнать ID реакции"), types.KeyboardButton(text="📊 Статистика")]
    ]
        # ✅ Добавляем кнопку админки ТОЛЬКО для ранга 2
    if op.rank >= 2:
        kb.append([types.KeyboardButton(text="🛡 Админ-панель")])
    
    keyboard = types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    await message.answer(f"👋 Привет, {'Старший ' if op.rank >= 2 else ''}Оператор!", reply_markup=keyboard)
    keyboard = types.ReplyKeyboardMarkup(
        keyboard=kb, 
        resize_keyboard=True,
        input_field_placeholder="Управление фермой..."
    )
    
    await message.answer(
        f"👋 Привет, оператор группы <b>{op.group_tag}</b>!\n"
        f"Выберите раздел для работы:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


# --- ВЫДАЧА ПОСТА ---

@dp.message(F.text == "📥 Получить новый пост")
async def send_new_post(message: types.Message):
    op = await get_operator(message.from_user.id)
    if not op: return

    post = await get_next_post(op.group_tag)
    
    if not post:
        await message.answer("☕️ Пока новых постов нет. Отдыхайте!")
        return

    TARGET_GROUP = -1003723379200 
    
    # Клавиатура выбора
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="✅ Оформить паспорт", callback_data=f"setup_{post.id}"),
        types.InlineKeyboardButton(text="❌ Мусор", callback_data=f"trash_{post.id}")
    )

    try:
        # Пересылка самого поста
        await bot.forward_message(message.chat.id, TARGET_GROUP, post.storage_msg_id)
        # Сообщение с кнопками управления
        await message.answer(
            f"🔎 Найдено по ключу: <b>{post.keyword_hit}</b>\n"
            f"ID поста в БД: {post.id}\n"
            "Определите, является ли это конкурсом:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка пересылки: {e}")

# --- FSM: ОФОРМЛЕНИЕ ПАСПОРТА ---
# --- ШАГ 1: ТИП КОНКУРСА ---
@dp.callback_query(F.data.startswith("setup_"))
async def start_setup(callback: types.CallbackQuery, state: FSMContext):
    post_id = int(callback.data.split("_")[1])
    await state.update_data(current_post_id=post_id)
    await state.set_state(ContestForm.choosing_type)
    
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🕹 АФК участие", callback_data="type_afk"))
    builder.row(types.InlineKeyboardButton(text="🗳 Голосование", callback_data="type_vote"))
    
    await callback.message.edit_text("📝 <b>Шаг 1: Тип конкурса</b>", reply_markup=builder.as_markup(), parse_mode="HTML")

# --- ШАГ 2: ПРИЗ ---
@dp.callback_query(ContestForm.choosing_type)
async def process_type(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(contest_type=callback.data.replace("type_", ""))
    await state.set_state(ContestForm.choosing_prize)
    
    builder = InlineKeyboardBuilder()
    prizes = ["Деньги 💵", "Звезды ⭐", "NFT 🖼", "Подарок 🎁", "Ценности 🎮", "Другое ⚙️"]
    for p in prizes:
        builder.add(types.InlineKeyboardButton(text=p, callback_data=f"prize_{p}"))
    builder.adjust(2)
    await callback.message.edit_text("📝 <b>Шаг 2: Приз</b>", reply_markup=builder.as_markup())

# --- ШАГ 2.1: ОБРАБОТКА ПРИЗА ---
@dp.callback_query(ContestForm.choosing_prize)
async def process_prize(callback: types.CallbackQuery, state: FSMContext):
    prize_raw = callback.data.replace("prize_", "")
    if "Другое" in prize_raw:
        await state.set_state(ContestForm.input_prize_custom)
        await callback.message.edit_text("⌨️ Введите название приза вручную:")
    else:
        await state.update_data(prize=prize_raw)
        await proceed_from_prize(callback.message, state)

@dp.message(ContestForm.input_prize_custom)
async def process_custom_prize(message: types.Message, state: FSMContext):
    await state.update_data(prize=message.text)
    await proceed_from_prize(message, state)

async def proceed_from_prize(message, state: FSMContext):
    data = await state.get_data()
    if data['contest_type'] == 'vote':
        await state.set_state(ContestForm.input_vote_executor)
        await message.answer("👤 <b>Шаг 3 (Голосование):</b> Введите Nickname/ID аккаунта-исполнителя для регистрации:", parse_mode="HTML")
    else:
        await state.set_state(ContestForm.filling_conditions)
        await message.answer("📝 <b>Шаг 3: Условия</b>", reply_markup=get_conditions_kb([]), parse_mode="HTML")

# --- ШАГ 3 (ГОЛОСОВАНИЕ): ДАННЫЕ РЕГИСТРАЦИИ ---
@dp.message(ContestForm.input_vote_executor)
async def vote_exec(message: types.Message, state: FSMContext):
    await state.update_data(vote_executor=message.text)
    await state.set_state(ContestForm.input_vote_data)
    await message.answer("📄 Введите данные для регистрации (Ник, текст или описание фото):")

@dp.message(ContestForm.input_vote_data)
async def vote_data(message: types.Message, state: FSMContext):
    await state.update_data(vote_reg_data=message.text)
    await state.set_state(ContestForm.input_vote_place)
    await message.answer("📍 Где регистрироваться? (Напр: ЛС @user или Комменты):")

@dp.message(ContestForm.input_vote_place)
async def vote_place(message: types.Message, state: FSMContext):
    await state.update_data(vote_reg_place=message.text)
    await ask_intensity(message, state)

# --- ШАГ 3 (АФК): УСЛОВИЯ ---
@dp.callback_query(ContestForm.filling_conditions)
async def process_conditions(callback: types.CallbackQuery, state: FSMContext):
    if callback.data == "cond_done":
        await check_afk_substeps(callback.message, state)
        return
    code = callback.data.replace("cond_", "")
    data = await state.get_data()
    selected = data.get("selected_conds", [])
    if code in selected: selected.remove(code)
    else: selected.append(code)
    await state.update_data(selected_conds=selected)
    await callback.message.edit_reply_markup(reply_markup=get_conditions_kb(selected))

async def check_afk_substeps(message, state: FSMContext):
    data = await state.get_data()
    conds = data.get("selected_conds", [])
    if "sub" in conds:
        await state.set_state(ContestForm.input_sub_links)
        await message.answer("🔗 Введите ссылки на ТГК для подписки:")
    elif "repost" in conds:
        await state.set_state(ContestForm.input_repost_count)
        await message.answer("🔄 Введите количество чатов для репоста:")
    else:
        await ask_intensity(message, state)

@dp.message(ContestForm.input_sub_links)
async def sub_links(message: types.Message, state: FSMContext):
    await state.update_data(sub_links=message.text)
    data = await state.get_data()
    if "repost" in data.get("selected_conds", []):
        await state.set_state(ContestForm.input_repost_count)
        await message.answer("🔄 Введите количество чатов для репоста:")
    else:
        await ask_intensity(message, state)

@dp.message(ContestForm.input_repost_count)
async def repost_count(message: types.Message, state: FSMContext):
    await state.update_data(repost_count=message.text)
    await ask_intensity(message, state)

# --- ШАГ 4: ИНТЕНСИВНОСТЬ ---
async def ask_intensity(message, state: FSMContext):
    await state.set_state(ContestForm.setting_intensity)
    await message.answer("🚀 <b>Шаг 4: Интенсивность</b>", reply_markup=get_intensity_kb(), parse_mode="HTML")

@dp.callback_query(ContestForm.setting_intensity)
async def process_intensity(callback: types.CallbackQuery, state: FSMContext):
    level = callback.data.replace("int_", "")
    await state.update_data(intensity=level)
    
    data = await state.get_data()
    summary = f"🏁 <b>Проверка паспорта</b>\nТип: {data['contest_type']}\nПриз: {data['prize']}\nИнтенсивность: {level} уровень"
    
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="✅ Запустить", callback_data="passport_confirm"))
    builder.row(types.InlineKeyboardButton(text="❌ Отмена", callback_data="passport_cancel"))
    
    await state.set_state(ContestForm.confirming)
    await callback.message.edit_text(summary, reply_markup=builder.as_markup(), parse_mode="HTML")

# --- ФИНАЛ: СОХРАНЕНИЕ ---
@dp.callback_query(ContestForm.confirming, F.data == "passport_confirm")
async def save_passport(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    op = await get_operator(callback.from_user.id)
    
    async with async_session() as session:
        # 1. Помечаем пост-триггер как отработанный
        await session.execute(
            update(PotentialPost)
            .where(PotentialPost.id == int(data['current_post_id']))
            .values(is_claimed=True, claimed_at=datetime.now())
        )
        
        # --- НОВЫЙ БЛОК: ШАГ 4 ---
        # 2. Узнаем ID канала из этого поста, чтобы включить "тотальный мониторинг"
        post_query = await session.execute(
            select(PotentialPost.source_tg_id).where(PotentialPost.id == int(data['current_post_id']))
        )
        source_channel_id = post_query.scalar()

        if source_channel_id:
            await session.execute(
                update(TargetChannel)
                .where(TargetChannel.tg_id == source_channel_id)
                .values(status="active_monitor") # Теперь start_work.py начнет пересылать ВСЁ
            )
        # -------------------------

        # 3. Собираем условия в JSON (это у тебя уже было)
        conditions_data = {
            "selected": data.get("selected_conds", []),
            "sub_links": data.get("sub_links", ""),
            "repost_count": data.get("repost_count", "0"),
            "vote_details": {
                "executor": data.get("vote_executor"),
                "reg_data": data.get("vote_reg_data"),
                "reg_place": data.get("vote_reg_place")
            } if data['contest_type'] == 'vote' else {}
        }

        # 4. Создаем запись паспорта
        new_passport = ContestPassport(
            post_id=int(data['current_post_id']),
            group_tag=op.group_tag,
            type=data['contest_type'],
            prize_type=data['prize'],
            conditions=conditions_data,
            intensity_level=int(data['intensity']),
            status="active"
        )
        
        session.add(new_passport)
        await session.commit()
    
    await state.clear()
    await callback.message.edit_text("🚀 <b>Паспорт успешно создан!</b>\nКанал переведен в режим активного мониторинга.", parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "passport_cancel")
async def cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Отменено.")

@dp.callback_query(F.data.startswith("trash_"))
async def trash(callback: types.CallbackQuery):
    post_id = int(callback.data.split("_")[1])
    async with async_session() as session:
        await session.execute(update(PotentialPost).where(PotentialPost.id == post_id).values(is_claimed=True))
        await session.commit()
    await callback.message.edit_text("🗑 В мусоре.")

# --- ФУНКЦИОНАЛ: УЗНАТЬ ID РЕАКЦИИ ---

@dp.message(F.text == "🔍 Узнать ID реакции")
async def start_reaction_id(message: types.Message, state: FSMContext):
    await state.set_state(ContestForm.waiting_for_reaction)
    await message.answer(
        "✨ <b>Режим определения ID реакции</b>\n\n"
        "Отправьте мне <b>Эмодзи</b> (одним сообщением), чтобы я выдал его технический ID для рапорта.\n"
        "<i>Для отмены просто напишите любое другое слово.</i>",
        parse_mode="HTML"
    )

@dp.message(ContestForm.waiting_for_reaction)
async def process_reaction_id(message: types.Message, state: FSMContext):
    # 1. Проверка на СЛОТЫ / КУБИКИ (🎰, 🎲, 🎯, 🏀)
    if message.dice:
        emoji_code = message.dice.emoji
        await message.answer(
            f"🎰 <b>Тип: Анимированный слот/кубик</b>\n"
            f"ID для рапорта: <code>{emoji_code}</code>\n\n"
            f"<i>Этот код заставит воркеров отправить именно такой игровой кубик.</i>",
            parse_mode="HTML"
        )
        await state.clear()
        return

    # 2. Проверка на КАСТОМНЫЕ ЭМОДЗИ (Premium)
    if message.entities:
        for entity in message.entities:
            if entity.type == "custom_emoji":
                custom_id = entity.custom_emoji_id
                await message.answer(
                    f"🌟 <b>Тип: Кастомный эмодзи (Premium)</b>\n"
                    f"ID для рапорта: <code>{custom_id}</code>\n\n"
                    f"<i>Используйте это числовое ID в рапорте голосования.</i>",
                    parse_mode="HTML"
                )
                await state.clear()
                return

    # 3. Проверка на ОБЫЧНЫЕ ЭМОДЗИ (Unicode)
    if message.text:
        # Просто берем первый символ, если прислали пачку
        emoji = message.text.strip()
        await message.answer(
            f"😀 <b>Тип: Обычный эмодзи</b>\n"
            f"ID для рапорта: <code>{emoji}</code>\n\n"
            f"<i>Стандартная реакция или текстовый символ.</i>",
            parse_mode="HTML"
        )
        await state.clear()
        return

    await message.answer("❌ Не удалось распознать тип. Отправьте эмодзи, кубик или кастомный смайл.")

@dp.message(F.text == "📋 Текущие конкурсы")
async def show_contests_types(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🕹 АФК", callback_data="cur_afk"))
    builder.row(types.InlineKeyboardButton(text="🗳 Голосование", callback_data="cur_vote"))
    await message.answer("Выберите тип активных конкурсов:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("cur_"))
async def list_active_channels(callback: types.CallbackQuery, state: FSMContext):
    c_type = callback.data.replace("cur_", "")
    op = await get_operator(callback.from_user.id)
    
    async with async_session() as session:
        # Сложный запрос: Берем каналы, считаем посты > last_read_post_id
        # Используем подзапрос для подсчета, чтобы не терять каналы с 0 новых постов
        query = (
            select(
                TargetChannel,
                func.count(PotentialPost.id).label("new_count")
            )
            .join(PotentialPost, PotentialPost.source_tg_id == TargetChannel.tg_id)
            .join(ContestPassport, ContestPassport.post_id == PotentialPost.id)
            .where(
                ContestPassport.group_tag == op.group_tag,
                ContestPassport.type == c_type,
                ContestPassport.status == "active"
            )
            .group_by(TargetChannel.id)
            .order_by(text("new_count DESC")) # Сортировка: сначала те, где больше новых
        )
        
        result = await session.execute(query)
        channels_data = result.all()

    if not channels_data:
        await callback.message.edit_text(f"📭 У группы {op.group_tag} нет активных конкурсов типа {c_type}.")
        return

    builder = InlineKeyboardBuilder()
    for ch, new_count in channels_data:
        # Кнопка всегда видна, даже если (+0)
        status_tag = f" (+{new_count})" if new_count > 0 else ""
        btn_text = f"{ch.username or ch.tg_id}{status_tag}"
        builder.row(types.InlineKeyboardButton(text=btn_text, callback_data=f"viewch_{ch.tg_id}_{c_type}"))
    
    await callback.message.edit_text(f"📡 Активные каналы ({c_type}):", reply_markup=builder.as_markup())
@dp.callback_query(F.data.startswith("viewch_"))
async def view_contest_details(callback: types.CallbackQuery, state: FSMContext):
    # 1. Разбор данных (viewch_ID_TYPE)
    _, tg_id_str, c_type = callback.data.split("_")
    tg_id = int(tg_id_str)
    op = await get_operator(callback.from_user.id)
    
    # Константы хранилищ
    TARGET_GROUP = -1003723379200   # Группа для находок
    MONITOR_STORAGE = -1003753624654 # Группа для отслеживаемых (ВСЁ подряд)
    
    async with async_session() as session:
        # Получаем объект канала
        ch_query = select(TargetChannel).where(TargetChannel.tg_id == tg_id)
        channel = (await session.execute(ch_query)).scalar_one_or_none()
        
        if not channel:
            await callback.answer("❌ Канал не найден.")
            return

        # 2. Ищем новые посты для этого канала
                # Ищем новые посты, но если пост продублирован (monitoring + keyword), 
        # берем только версию monitoring для красивого отображения в ленте
               # Теперь для ленты берем ТОЛЬКО мониторинговые посты этого канала
                # Находим посты для ленты
        posts_query = select(PotentialPost).where(
            PotentialPost.source_tg_id == tg_id,
            PotentialPost.source_msg_id > channel.last_read_post_id,
            PotentialPost.post_type == "monitoring" # БЕРЕМ ТОЛЬКО ЗЕРКАЛО
        ).order_by(PotentialPost.source_msg_id.asc())


        # Сортировка по типу заставит 'monitoring' быть приоритетнее при обработке ID

        
        new_posts = (await session.execute(posts_query)).scalars().all()

        # 3. Пересылка и пометка мониторинговых постов
        if new_posts:
            await callback.message.answer(f"⬇️ <b>Новые сообщения в канале ({len(new_posts)} шт):</b>", parse_mode="HTML")
            max_id = channel.last_read_post_id
            
            for p in new_posts:
                try:
                    source_chat = MONITOR_STORAGE if p.post_type == "monitoring" else TARGET_GROUP
                    await bot.forward_message(callback.message.chat.id, source_chat, p.storage_msg_id)
                    if p.source_msg_id > max_id:
                        max_id = p.source_msg_id
                except Exception as e:
                    print(f"❌ Ошибка пересылки: {e}")
                    if p.source_msg_id > max_id:
                        max_id = p.source_msg_id

            channel.last_read_post_id = max_id
            await session.commit()

        else:
            await callback.message.answer("🧐 Новых постов пока нет.")

        # 4. Получаем данные всех активных паспортов для этого канала
        p_query = select(ContestPassport).join(PotentialPost, ContestPassport.post_id == PotentialPost.id).\
            where(PotentialPost.source_tg_id == tg_id, 
                  ContestPassport.type == c_type,
                  ContestPassport.status == "active")
        passports = (await session.execute(p_query)).scalars().all()

    # 5. Выводим резюме и кнопки управления
    for passp in passports:
        summary = (
            f"📝 <b>Паспорт конкурса #{passp.id}</b>\n"
            f"🔹 Тип: <code>{passp.type}</code>\n"
            f"🔹 Приз: <code>{passp.prize_type}</code>\n"
            f"🔹 Интенсивность: <code>{passp.intensity_level} ур.</code>"
        )
        
        builder = InlineKeyboardBuilder()
        if c_type == "afk":
            builder.row(types.InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"edit_{passp.id}"))
            builder.add(types.InlineKeyboardButton(text="🛑 Остановить", callback_data=f"stop_{passp.id}"))
            builder.row(types.InlineKeyboardButton(text="👥 Добавить группы", callback_data=f"addgr_{passp.id}"))
            builder.row(types.InlineKeyboardButton(text="📢 Отправить другим группам", callback_data=f"share_{passp.id}"))
        else: # vote
            builder.row(types.InlineKeyboardButton(text="🗳 Голосование (Рапорт)", callback_data=f"v_rep_{passp.id}"))
            builder.add(types.InlineKeyboardButton(text="🛑 Остановить", callback_data=f"stop_{passp.id}"))
            builder.row(types.InlineKeyboardButton(text="👥 Добавить группы", callback_data=f"addgr_{passp.id}"))
        
        await callback.message.answer(summary, reply_markup=builder.as_markup(), parse_mode="HTML")
    
    await callback.answer()

@dp.callback_query(F.data.startswith("stop_"))
async def stop_contest(callback: types.CallbackQuery):
    passport_id = int(callback.data.split("_")[1])
    
    async with async_session() as session:
        # 1. Получаем паспорт и связанный с ним канал
        res = await session.execute(
            select(ContestPassport, PotentialPost.source_tg_id)
            .join(PotentialPost, ContestPassport.post_id == PotentialPost.id)
            .where(ContestPassport.id == passport_id)
        )
        passport, tg_id = res.first()
        
        if passport:
            # 2. Завершаем паспорт
            passport.status = "finished"
            
            # 3. Переводим канал в режим ожидания (выключаем зеркало)
            await session.execute(
                update(TargetChannel)
                .where(TargetChannel.tg_id == tg_id)
                .values(status="idle")
            )
            await session.commit()
            await callback.message.edit_text(f"🛑 Участие в конкурсе #{passport_id} остановлено. Мониторинг канала выключен.")
        else:
            await callback.answer("❌ Паспорт не найден.")
    await callback.answer()

# --- 1. ОСТАНОВКА УЧАСТИЯ ---
@dp.callback_query(F.data.startswith("stop_"))
async def stop_contest(callback: types.CallbackQuery):
    passport_id = int(callback.data.split("_")[1])
    async with async_session() as session:
        # Получаем паспорт и ID канала через связь с постом
        res = await session.execute(
            select(ContestPassport, PotentialPost.source_tg_id)
            .join(PotentialPost, ContestPassport.post_id == PotentialPost.id)
            .where(ContestPassport.id == passport_id)
        )
        row = res.first()
        if row:
            passport, tg_id = row
            passport.status = "finished" # Меняем статус паспорта
            # Выключаем зеркало для канала
            await session.execute(
                update(TargetChannel).where(TargetChannel.tg_id == tg_id).values(status="idle")
            )
            await session.commit()
            await callback.message.edit_text(f"🛑 Конкурс #{passport_id} остановлен. Мониторинг выключен.")
        else:
            await callback.answer("❌ Паспорт не найден.")

# --- 2. РЕДАКТИРОВАНИЕ (ВЫБОР ПОЛЯ) ---
@dp.callback_query(F.data.startswith("edit_"))
async def edit_contest_start(callback: types.CallbackQuery, state: FSMContext):
    passport_id = int(callback.data.split("_")[1])
    await state.update_data(edit_passport_id=passport_id)
    
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🚀 Интенсивность", callback_data="ed_field_int"))
    builder.row(types.InlineKeyboardButton(text="🔗 Ссылки подписки", callback_data="ed_field_sub"))
    builder.row(types.InlineKeyboardButton(text="🔄 Кол-во репостов", callback_data="ed_field_rep"))
    
    await callback.message.answer("⚙️ <b>Редактирование:</b> Что изменить?", reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("ed_field_"))
async def process_edit_choice(callback: types.CallbackQuery, state: FSMContext):
    field = callback.data.replace("ed_field_", "")
    await state.update_data(editing_target=field)
    
    if field == "int":
        # Используем твою готовую клавиатуру интенсивности
        await callback.message.edit_text("Выберите новый уровень интенсивности:", reply_markup=get_intensity_kb())
    else:
        await state.set_state(ContestForm.editing_field)
        await callback.message.answer("⌨️ Введите новые данные (текстом):")
    await callback.answer()

# --- 3. СОХРАНЕНИЕ ПРАВОК ---
@dp.callback_query(ContestForm.editing_field, F.data.startswith("int_")) # Если через кнопку
@dp.message(ContestForm.editing_field) # Если текстом
async def save_edit_data(event, state: FSMContext):
    data = await state.get_data()
    passport_id = data['edit_passport_id']
    target = data['editing_target']
    
    # Определяем новое значение
    if isinstance(event, types.CallbackQuery):
        new_val = event.data.replace("int_", "")
        message = event.message
    else:
        new_val = event.text
        message = event

    async with async_session() as session:
        res = await session.execute(select(ContestPassport).where(ContestPassport.id == passport_id))
        passport = res.scalar_one()
        
        # Обновляем нужные поля
        if target == "int":
            passport.intensity_level = int(new_val)
        else:
            # Работаем с JSON полем conditions
            current_conds = dict(passport.conditions) if passport.conditions else {}
            if target == "sub": current_conds['sub_links'] = new_val
            if target == "rep": current_conds['repost_count'] = new_val
            passport.conditions = current_conds
            
        await session.commit()
    
    await state.clear()
    await message.answer(f"✅ Данные паспорта #{passport_id} обновлены!")

# --- 1. СТАРТ СОЗДАНИЯ РАПОРТА ---
@dp.callback_query(F.data.startswith("v_rep_"))
async def start_voting_report(callback: types.CallbackQuery, state: FSMContext):
    passport_id = int(callback.data.split("_")[2])
    await state.update_data(v_passport_id=passport_id, selected_groups=[])
    
    await state.set_state(ContestForm.v_rep_fwd)
    await callback.message.answer(
        "🗳 <b>Новый рапорт голосования</b>\n\n"
        "Перешлите в этот чат пост из канала, на который нужно совершить накрутку.",
        parse_mode="HTML"
    )
    await callback.answer()

# --- 2. ПРИЕМ ПЕРЕСЛАННОГО ПОСТА ---
@dp.message(ContestForm.v_rep_fwd)
async def process_v_fwd(message: types.Message, state: FSMContext):
    if not message.forward_from_message_id:
        await message.answer("❌ Ошибка! Нужно именно <b>переслать</b> пост из канала.")
        return

    await state.update_data(
        v_target_msg_id=message.forward_from_message_id,
        v_target_chat_id=message.forward_from_chat.id
    )
    
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="📊 Опрос (Poll)", callback_data="v_meth_poll"))
    builder.row(types.InlineKeyboardButton(text="🔥 Реакция (Emoji)", callback_data="v_meth_reac"))
    
    await state.set_state(ContestForm.v_rep_method)
    await message.answer("Выберите способ накрутки:", reply_markup=builder.as_markup())

# --- 3. ВВОД ВАРИАНТА (ОПЦИИ) ---
@dp.callback_query(ContestForm.v_rep_method)
async def process_v_method(callback: types.CallbackQuery, state: FSMContext):
    method = callback.data.replace("v_meth_", "")
    await state.update_data(v_method=method)
    
    prompt = "Введите <b>номер варианта</b> (1, 2, 3...):" if method == "poll" else "Введите <b>ID/Эмодзи</b> реакции:"
    
    await state.set_state(ContestForm.v_rep_option)
    await callback.message.edit_text(prompt, parse_mode="HTML")
    await callback.answer()

# --- 3. ВЫБОР ГРУПП (ИСПРАВЛЕННЫЙ) ---
@dp.message(ContestForm.v_rep_option)
async def process_v_option(message: types.Message, state: FSMContext):
    await state.update_data(v_option=message.text, selected_groups=[])
    op = await get_operator(message.from_user.id)
    
    async with async_session() as session:
        # Берем все уникальные теги групп из таблицы воркеров
        result = await session.execute(text("SELECT DISTINCT group_tag FROM workers.workers"))
        # Извлекаем только значения строк (первый элемент кортежа)
        all_groups = [row[0] for row in result.all() if row[0]]

    # Если в базе нет воркеров, принудительно добавляем группу оператора для теста
    if not all_groups:
        all_groups = [op.group_tag]

    builder = InlineKeyboardBuilder()
    for g in all_groups:
        builder.row(types.InlineKeyboardButton(text=f"Группа {g}", callback_data=f"v_grp_{g}"))
    
    builder.row(types.InlineKeyboardButton(text="➡️ Далее (К интенсивности)", callback_data="v_grp_done"))
    
    await state.set_state(ContestForm.v_rep_choose_groups)
    await message.answer("Выберите группы для участия:", reply_markup=builder.as_markup())

# --- 4. ОБРАБОТКА КЛИКОВ (ИСПРАВЛЕННЫЙ) ---
@dp.callback_query(ContestForm.v_rep_choose_groups, F.data.startswith("v_grp_"))
async def process_v_groups(callback: types.CallbackQuery, state: FSMContext):
    if callback.data == "v_grp_done":
        data = await state.get_data()
        selected = data.get("selected_groups", [])
        if not selected:
            await callback.answer("⚠️ Выберите хотя бы одну группу!", show_alert=True)
            return

        if len(selected) == 1:
            await state.set_state(ContestForm.v_rep_count)
            await callback.message.edit_text("🔢 <b>Сколько аккаунтов</b> задействовать?", parse_mode="HTML")
        else:
            await state.update_data(v_rep_count=0)
            await ask_v_intensity(callback.message, state)
        await callback.answer()
        return

    group_tag = callback.data.replace("v_grp_", "")
    data = await state.get_data()
    selected = data.get("selected_groups", [])
    
    if group_tag in selected: selected.remove(group_tag)
    else: selected.append(group_tag)
    
    await state.update_data(selected_groups=selected)
    
    # Перерисовываем список групп
    op = await get_operator(callback.from_user.id)
    async with async_session() as session:
        result = await session.execute(text("SELECT DISTINCT group_tag FROM workers.workers"))
        all_groups = [row[0] for row in result.all() if row[0]]
    
    if not all_groups: all_groups = [op.group_tag]

    builder = InlineKeyboardBuilder()
    for g in all_groups:
        mark = " ✅" if g in selected else ""
        builder.row(types.InlineKeyboardButton(text=f"Группа {g}{mark}", callback_data=f"v_grp_{g}"))
    
    builder.row(types.InlineKeyboardButton(text="➡️ Далее", callback_data="v_grp_done"))
    await callback.message.edit_reply_markup(reply_markup=builder.as_markup())
    await callback.answer()

# --- 5. ВВОД КОЛИЧЕСТВА (ДЛЯ 1 ГРУППЫ) ---
@dp.message(ContestForm.v_rep_count)
async def process_v_count(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Введите число!")
        return
    await state.update_data(v_rep_count=int(message.text))
    await ask_v_intensity(message, state)

# --- 6. ИНТЕНСИВНОСТЬ РАПОРТА ---
async def ask_v_intensity(message, state: FSMContext):
    await state.set_state(ContestForm.v_rep_intensity)
    await message.answer("🚀 Выберите <b>интенсивность накрутки</b> для рапорта:", reply_markup=get_intensity_kb(), parse_mode="HTML")

@dp.callback_query(ContestForm.v_rep_intensity)
async def process_v_intensity(callback: types.CallbackQuery, state: FSMContext):
    intensity = callback.data.replace("int_", "")
    await state.update_data(v_intensity=intensity)
    
    # Финальное превью перед отправкой Старшему
    data = await state.get_data()
    summary = (
        "📊 <b>ПРЕДПРОСМОТР РАПОРТА</b>\n\n"
        f"📍 Пост ID: <code>{data['v_target_msg_id']}</code>\n"
        f"🛠 Метод: <code>{data['v_method']}</code>\n"
        f"🎯 Вариант: <code>{data['v_option']}</code>\n"
        f"👥 Группы: <code>{', '.join(data['selected_groups'])}</code>\n"
        f"🔢 Аккаунтов: <code>{'Все' if data['v_rep_count'] == 0 else data['v_rep_count']}</code>\n"
        f"🚀 Интенсивность: <code>{intensity} ур.</code>\n\n"
        "Отправить на подтверждение Старшему Оператору?"
    )
    
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="📤 Отправить", callback_data="v_rep_confirm"))
    builder.row(types.InlineKeyboardButton(text="❌ Отмена", callback_data="v_rep_cancel"))
    
    await state.set_state(ContestForm.v_rep_confirm)
    await callback.message.edit_text(summary, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(ContestForm.v_rep_confirm, F.data == "v_rep_confirm")
async def save_voting_report(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    
    async with async_session() as session:
        new_report = VotingReport(
            passport_id=data['v_passport_id'],
            target_msg_id=data['v_target_msg_id'],
            target_chat_id=data['v_target_chat_id'],
            vote_type=data['v_method'],
            option_id=data['v_option'],
            target_groups=data['selected_groups'],
            accounts_count=data['v_rep_count'],
            intensity=int(data['v_intensity']),
            created_by=callback.from_user.id,
            status="pending"
        )
        session.add(new_report)
        await session.commit()
    
    await state.clear()
    await callback.message.edit_text("✅ <b>Рапорт отправлен!</b>\nОжидайте подтверждения от Старшего Оператора.", parse_mode="HTML")
    
    # ТУТ МОЖНО ДОБАВИТЬ УВЕДОМЛЕНИЕ СТАРШЕМУ (если есть его ID)
    await callback.answer()

@dp.message(F.text == "🛡 Админ-панель")
async def admin_panel(message: types.Message):
    op = await get_operator(message.from_user.id)
    if op.rank < 2: return

    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="📥 Ожидающие рапорты (Pending)", callback_data="adm_view_pending"))
    
    await message.answer("🛠 <b>Кабинет Старшего Оператора</b>\nВыберите раздел:", reply_markup=builder.as_markup(), parse_mode="HTML")

# --- ЗАПУСК ---

async def main():
    print("🚀 Бот-интерфейс запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
