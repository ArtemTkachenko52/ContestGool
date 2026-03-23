import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from decouple import config
from sqlalchemy import select, update, func, text  
from datetime import datetime, timedelta
# Импорты из проекта
from database.config import async_session
from database.models import (
    Operator, PotentialPost, ContestPassport, 
    TargetChannel, VotingReport, StarReport, 
    GroupChannelRelation, OutgoingMessage, WorkerAccount,  ChannelSubmission, ExtraChat
)
from service_bot.states import ContestForm, ScalerForm
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
        [types.KeyboardButton(text="➕ Добавить новый ТГК")],
        [types.KeyboardButton(text="📋 Текущие конкурсы")],  # <--- КНОПКА ТУТ
        [types.KeyboardButton(text="📬 ЛС исполнителей")],
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
    # --- МАСШТАБИРОВАНИЕ: ПРИЕМ ССЫЛКИ ---
@dp.message(F.text == "➕ Добавить новый ТГК")
async def start_add_channel(message: types.Message, state: FSMContext):
    await state.set_state(ScalerForm.waiting_for_link)
    await message.answer("🔗 Отправьте <b>ссылку</b> на канал или его <b>@username</b>:", parse_mode="HTML")
@dp.message(ScalerForm.waiting_for_link)
async def process_channel_link(message: types.Message, state: FSMContext):
    link = message.text.strip()
    # Чистим ссылку до юзернейма для проверки дублей
    clean_username = link.replace("https://t.me", "").replace("@", "")
    async with async_session() as session:
        # Проверяем, нет ли уже такого канала в рабочих или в заявках
        q_check = text("SELECT id FROM watcher.channels WHERE username LIKE :u OR username LIKE :u2")
        exists = await session.execute(q_check, {"u": f"%{clean_username}%", "u2": f"%{link}%"})
        if exists.scalar():
            await message.answer("❌ Этот канал уже есть в работе!")
            await state.clear()
            return
        # Создаем заявку для Старшего
        new_sub = ChannelSubmission(
            username=link,
            operator_id=message.from_user.id,
            status="pending"
        )
        session.add(new_sub)
        await session.commit()
    await message.answer("✅ <b>Заявка отправлена!</b>\nСтарший оператор проверит её.")
    await state.clear()
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
    prizes = ["Звезды ⭐", "NFT 🖼", "Подарок 🎁", "Другое ⚙️"]
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
        # Передаем callback.message (само сообщение с кнопками) и callback.from_user.id
        await proceed_from_prize(callback.message, state, callback.from_user.id)
    await callback.answer()
@dp.message(ContestForm.input_prize_custom)
async def process_custom_prize(message: types.Message, state: FSMContext):
    await state.update_data(prize=message.text)
    await proceed_from_prize(message, state)
async def proceed_from_prize(message: types.Message, state: FSMContext, user_id: int):
    data = await state.get_data()
    op = await get_operator(user_id)
    if not op:
        await message.answer("❌ Ошибка: оператор не найден в БД.")
        return
    if data['contest_type'] == 'vote':
        async with async_session() as session:
            # Получаем воркеров этой группы
            res = await session.execute(
                select(WorkerAccount.tg_id).where(WorkerAccount.group_tag == op.group_tag)
            )
            # row[0] достает само число из кортежа БД
            workers = [row[0] for row in res.all()]
        if not workers:
            # Редактируем старое сообщение вместо отправки нового
            await message.edit_text(f"❌ В вашей группе ({op.group_tag}) нет исполнителей!")
            await state.clear()
            return
        builder = InlineKeyboardBuilder()
        for w_id in workers:
            builder.row(types.InlineKeyboardButton(text=f"🤖 Аккаунт {w_id}", callback_data=f"vexec_{w_id}"))
        await state.set_state(ContestForm.vote_choose_executor)
        # РЕДАКТИРУЕМ сообщение
        await message.edit_text("👤 <b>Шаг 3: Кто участвует?</b>\nВыберите исполнителя для регистрации:", 
                                reply_markup=builder.as_markup(), parse_mode="HTML")
    else:
        await state.set_state(ContestForm.filling_conditions)
        await message.edit_text("📝 <b>Шаг 3: Условия</b>", 
                                reply_markup=get_conditions_kb([]), parse_mode="HTML")
# --- ШАГ 4 (ГОЛОСОВАНИЕ): ДАННЫЕ ДЛЯ РЕГИСТРАЦИИ ---
@dp.callback_query(ContestForm.vote_choose_executor, F.data.startswith("vexec_"))
async def process_vote_executor(callback: types.CallbackQuery, state: FSMContext):
    executor_id = callback.data.replace("vexec_", "")
    await state.update_data(vote_executor=executor_id)
    await state.set_state(ContestForm.input_vote_reg_data)
    await callback.message.edit_text(
        "📝 <b>Шаг 4: Данные для регистрации</b>\n"
        "Введите ник, текст или отправьте изображение, которое исполнитель использует для заявки:",
        parse_mode="HTML"
    )
    await callback.answer()
@dp.message(ContestForm.input_vote_reg_data)
async def process_vote_reg_data(message: types.Message, state: FSMContext):
    reg_text = message.text or message.caption or ""
    storage_id = None
    # Если прислали фото/видео/документ - пересылаем в хранилище
    if message.photo or message.document or message.video:
        # Пересылаем в MONITOR_STORAGE (как в CRM)
        fwd = await message.forward(MONITOR_STORAGE)
        storage_id = fwd.message_id
        if not reg_text:
            reg_text = "[Медиа-заявка]"
    # Сохраняем и текст, и ID медиа
    await state.update_data(vote_reg_data=reg_text, vote_reg_media_id=storage_id)
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="💬 Комментарии", callback_data="vplace_comm"))
    builder.row(types.InlineKeyboardButton(text="👤 ЛС Организатора", callback_data="vplace_ls"))
    await state.set_state(ContestForm.vote_choose_place)
    await message.answer("📍 <b>Шаг 5: Куда писать?</b>\nГде исполнитель должен оставить заявку?", 
                         reply_markup=builder.as_markup(), parse_mode="HTML")
# --- ШАГ 5 (ГОЛОСОВАНИЕ): МЕСТО РЕГИСТРАЦИИ ---
@dp.callback_query(ContestForm.vote_choose_place)
async def process_vote_place(callback: types.CallbackQuery, state: FSMContext):
    place = callback.data.replace("vplace_", "")
    if place == "ls":
        await state.set_state(ContestForm.input_vote_org_username)
        await callback.message.edit_text("⌨️ Введите <b>@username</b> организатора:")
    else:
        await state.update_data(vote_reg_place="Комментарии под постом")
        await ask_intensity(callback.message, state)
    await callback.answer()
@dp.message(ContestForm.input_vote_org_username)
async def process_org_username(message: types.Message, state: FSMContext):
    await state.update_data(vote_reg_place=f"ЛС {message.text}")
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
    # 1. Если выбрана подписка - запрашиваем ссылки
    if "sub" in conds:
        await state.set_state(ContestForm.input_sub_links)
        await message.answer("🔗 Введите ссылки на ТГК для подписки:")
    # 2. НОВОЕ: Если выбрана реакция - запрашиваем ID эмодзи
    elif "reac" in conds:
        await state.set_state(ContestForm.waiting_for_reaction) # Используем существующий стейт
        await message.answer("🔍 Введите **ID или Эмодзи** для реакции под постом:", parse_mode="HTML")
    # 3. Если выбран репост - запрашиваем количество
    elif "repost" in conds:
        await state.set_state(ContestForm.input_repost_count)
        await message.answer("🔄 Введите количество чатов для репоста:")
    else:
        await ask_afk_groups(message, state)
async def ask_afk_groups(message, state: FSMContext):
    data = await state.get_data()
    post_id = data['current_post_id']
    async with async_session() as session:
        # Находим ТГК, чтобы увидеть, какие группы в статусе 'ready'
        res = await session.execute(
            select(TargetChannel).join(PotentialPost, PotentialPost.source_tg_id == TargetChannel.tg_id).where(PotentialPost.id == post_id)
        )
        ch = res.scalar_one()
        # Собираем только те группы, где инвайт завершен
        ready_groups = [g for g, status in (ch.sync_status or {}).items() if status == "ready"]
    if not ready_groups:
        await message.answer("⚠️ Нет готовых групп (инвайт еще идет). Используйте только основную.")
        ready_groups = [ch.group_tag] # Фолбэк на основную
    builder = InlineKeyboardBuilder()
    selected = data.get("participating_groups", [])
    for g in ready_groups:
        mark = " ✅" if g in selected else ""
        builder.row(types.InlineKeyboardButton(text=f"Группа {g}{mark}", callback_data=f"afksel_{g}"))
    builder.row(types.InlineKeyboardButton(text="➡️ Далее (Интенсивность)", callback_data="afksel_done"))
    await state.set_state(ContestForm.sharing_to_groups) # Используем это состояние для перехвата кликов
    await message.answer("👥 <b>Выберите группы для участия:</b>", reply_markup=builder.as_markup(), parse_mode="HTML")
# ОБРАБОТЧИК КЛИКОВ ПО ГРУППАМ (Вставьте ниже)
@dp.callback_query(ContestForm.sharing_to_groups, F.data.startswith("afksel_"))
async def process_afk_groups_choice(callback: types.CallbackQuery, state: FSMContext):
    if callback.data == "afksel_done":
        data = await state.get_data()
        if not data.get("participating_groups"):
             await callback.answer("Выберите хотя бы одну группу!", show_alert=True)
             return
        await ask_intensity(callback.message, state) # Теперь переходим к интенсивности
        return
    group_tag = callback.data.replace("afksel_", "")
    data = await state.get_data()
    selected = data.get("participating_groups", [])
    if group_tag in selected: selected.remove(group_tag)
    else: selected.append(group_tag)
    await state.update_data(participating_groups=selected)
    # Обновляем галочки в меню (код обновления клавиатуры аналогичен предыдущим шагам)
    await callback.message.edit_reply_markup(reply_markup=callback.message.reply_markup)
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
async def resolve_and_distribute_links(links_str: str, parent_tg_id: int, main_group: str):
    """Превращает ссылки в ТГК или Чаты и распределяет по базе"""
    links = links_str.replace(',', ' ').split()
    from telethon.tl.types import Channel, Chat
    async with async_session() as session:
        for link in links:
            clean_link = link.strip().replace("https://t.me", "").replace("@", "")
            try:
                # Используем клиент Читателя для проверки ссылки
                entity = await bot.get_chat(link) # Aiogram метод для быстрой проверки
                tg_id = entity.id
                is_group = entity.type in ['group', 'supergroup']
                if not is_group: # Это КАНАЛ
                    # Проверяем, нет ли его уже в базе
                    exists = await session.execute(select(TargetChannel).where(TargetChannel.tg_id == tg_id))
                    if not exists.scalar():
                        new_ch = TargetChannel(
                            tg_id=tg_id,
                            username=link,
                            group_tag=main_group,
                            status="idle",
                            actions_config={main_group: "join"},
                            sync_status={main_group: "pending"}
                        )
                        session.add(new_ch)
                        print(f"🆕 [АВТО-ТГК] Добавлен канал {link} для группы {main_group}")
                else: # Это ЧАТ
                    exists_chat = await session.execute(select(ExtraChat).where(ExtraChat.tg_id == tg_id))
                    if not exists_chat.scalar():
                        new_chat = ExtraChat(
                            tg_id=tg_id,
                            username=link,
                            parent_channel_id=parent_tg_id
                        )
                        session.add(new_chat)
                        print(f"💬 [АВТО-ЧАТ] Чат {link} привязан к ТГК {parent_tg_id}")
            except Exception as e:
                print(f"⚠️ [RESOLVER-ERR] Не удалось обработать ссылку {link}: {e}")
        await session.commit()
# --- ФИНАЛ: СОХРАНЕНИЕ ПАСПОРТА (ПОЛНАЯ ФУНКЦИЯ) ---
@dp.callback_query(ContestForm.confirming, F.data == "passport_confirm")
async def save_passport(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    op = await get_operator(callback.from_user.id)
    if not op:
        await callback.answer("❌ Ошибка: оператор не найден.")
        return
    async with async_session() as session:
        # 1. Достаем оригинальные данные поста из PotentialPost
        # Это нужно, чтобы передать воркеру точные ID канала и сообщения
        post_id_int = int(data['current_post_id'])
        post_raw_query = await session.execute(
            select(PotentialPost).where(PotentialPost.id == post_id_int)
        )
        post_raw = post_raw_query.scalar_one()
                # --- НОВЫЙ КИРПИЧИК: РАСПРЕДЕЛЕНИЕ ССЫЛОК ИЗ УСЛОВИЙ ---
        if data.get("sub_links"):
            # Запускаем фоновую задачу, чтобы не тормозить оператора
            asyncio.create_task(resolve_and_distribute_links(
                data["sub_links"], 
                post_raw.source_tg_id, 
                op.group_tag
            ))

        # 2. Помечаем пост-триггер как отработанный (claimed)
        post_raw.is_claimed = True
        post_raw.claimed_at = datetime.now()
        # 3. Переводим канал в режим активного мониторинга (зеркало)
        await session.execute(
            update(TargetChannel)
            .where(TargetChannel.tg_id == post_raw.source_tg_id)
            .values(status="active_monitor")
        )
        # 4. Формируем расширенный JSON условий для Воркера
        conditions_data = {
            "selected": data.get("selected_conds", []),
            "sub_links": data.get("sub_links", ""),
            "repost_count": data.get("repost_count", "0"),
            "afk_reaction_id": data.get("afk_reaction_id"),
            # ВАЖНО: Эти поля воркер будет искать в start_work.py
            "source_tg_id": post_raw.source_tg_id,
            "source_msg_id": post_raw.source_msg_id,
            "vote_details": {
                "executor": data.get("vote_executor"),     # ID лид-аккаунта
                "reg_data": data.get("vote_reg_data"),  # Данные ника/текста
                "reg_media_id": data.get("vote_reg_media_id"),
                "reg_place": data.get("vote_reg_place")    # Куда писать (ЛС/Комменты)
            } if data['contest_type'] == 'vote' else {}
        }
        # 5. Создаем новую запись в таблице паспортов
                # Находим все готовые группы для этого канала
        ready_groups = [g for g, status in (post_raw.sync_status or {}).items() if status == "ready"]
        # Основная группа всегда считается (если она в конфиге ready)
        if not ready_groups: ready_groups = [op.group_tag]
        # Создаем паспорт с учетом списка участвующих групп (для единой интенсивности)
        new_passport = ContestPassport(
            post_id=post_id_int,
            group_tag=op.group_tag,
            participating_groups=ready_groups, # <--- ДОБАВЛЯЕМ ЭТО
            type=data['contest_type'],
            prize_type=data['prize'],
            conditions=conditions_data,
            intensity_level=int(data['intensity']),
            status="active"
        )
        session.add(new_passport)
        await session.commit()
    await state.clear()
    await callback.message.edit_text(
        "🚀 <b>Паспорт успешно создан!</b>\n"
        "Канал переведен в режим активного мониторинга.\n"
        "Исполнители начали подготовку к задачам.", 
        parse_mode="HTML"
    )
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
    # --- 1. ОПРЕДЕЛЯЕМ ID РЕАКЦИИ ---
    target_id = None
    res_type = ""
    if message.dice:
        target_id = message.dice.emoji
        res_type = "🎰 Анимированный слот/кубик"
    elif message.entities:
        for entity in message.entities:
            if entity.type == "custom_emoji":
                target_id = entity.custom_emoji_id
                res_type = "🌟 Кастомный эмодзи (Premium)"
                break
    if not target_id and message.text:
        target_id = message.text.strip()
        res_type = "😀 Обычный эмодзи"
    if not target_id:
        await message.answer("❌ Не удалось распознать тип. Отправьте эмодзи, кубик или кастомный смайл.")
        return
    # --- 2. ПРОВЕРЯЕМ КОНТЕКСТ (Паспорт или просто Проверка) ---
    data = await state.get_data()
    # Если мы внутри создания паспорта (есть ID поста)
    if 'current_post_id' in data:
        await state.update_data(afk_reaction_id=target_id)
        # Смотрим, нужно ли еще вводить репосты после реакции
        if "repost" in data.get("selected_conds", []):
            await state.set_state(ContestForm.input_repost_count)
            await message.answer(
                f"✅ Реакция <code>{target_id}</code> сохранена для конкурса.\n"
                f"🔄 Теперь введите <b>количество репостов</b>:", 
                parse_mode="HTML"
            )
        else:
            # Если репосты не нужны — идем к выбору групп
            await ask_afk_groups(message, state)
    else:
        # ОБЫЧНЫЙ РЕЖИМ (Кнопка "Узнать ID")
        await message.answer(
            f"<b>{res_type}</b>\n"
            f"ID для рапорта: <code>{target_id}</code>\n\n"
            f"<i>Используйте этот код при оформлении паспорта или рапорта.</i>",
            parse_mode="HTML"
        )
        await state.clear() # Очищаем стейт только в режиме проверки
@dp.message(F.text == "📋 Текущие конкурсы")
async def show_contests_types(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🕹 АФК", callback_data="cur_afk"))
    builder.row(types.InlineKeyboardButton(text="🗳 Голосование", callback_data="cur_vote"))
    await message.answer("Выберите тип активных конкурсов:", reply_markup=builder.as_markup())
# --- ИСПРАВЛЕННЫЙ СПИСОК КАНАЛОВ (Текущие конкурсы) ---
@dp.callback_query(F.data.startswith("cur_"))
async def list_active_channels(callback: types.CallbackQuery, state: FSMContext):
    c_type = callback.data.replace("cur_", "")
    op = await get_operator(callback.from_user.id)
    async with async_session() as session:
        # ИЗМЕНЕНО: Теперь ищем паспорта со статусом 'active' И 'finished'
        # Пока оператор вручную не нажмет кнопку "Остановить", канал будет в списке
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
                # ПРАВКА ТУТ: Показываем всё, что не удалено окончательно
                ContestPassport.status.in_(["active", "finished"]) 
            )
            .group_by(TargetChannel.id)
            .order_by(text("new_count DESC"))
        )
        result = await session.execute(query)
        channels_data = result.all()
    if not channels_data:
        await callback.message.edit_text(f"📭 У группы {op.group_tag} сейчас нет активных или отслеживаемых конкурсов ({c_type}).")
        return
    builder = InlineKeyboardBuilder()
    for ch, new_count in channels_data:
        status_tag = f" (+{new_count})" if new_count > 0 else ""
        btn_text = f"{ch.username or ch.tg_id}{status_tag}"
        builder.row(types.InlineKeyboardButton(text=btn_text, callback_data=f"viewch_{ch.tg_id}_{c_type}"))
    await callback.message.edit_text(f"📡 Мониторинг каналов ({c_type}):", reply_markup=builder.as_markup())
# --- ИСПРАВЛЕННЫЙ ПРОСМОТР ДЕТАЛЕЙ (viewch_) ---
@dp.callback_query(F.data.startswith("viewch_"))
async def view_contest_details(callback: types.CallbackQuery, state: FSMContext):
    # 1. ПРАВИЛЬНЫЙ РАЗБОР: viewch_ID_TYPE
    parts = callback.data.split("_")
    # ПРОВЕРКА: Если в списке меньше 3 элементов, значит данные битые
    if len(parts) < 3:
        await callback.answer("❌ Ошибка структуры данных.")
        return
    try:
        # Индекс 0 - 'viewch', Индекс 1 - ID канала, Индекс 2 - тип (vote/afk)
        tg_id = int(parts[1]) 
        c_type = parts[2]
    except (ValueError, IndexError):
        await callback.answer("❌ Ошибка извлечения ID канала.")
        return
    op = await get_operator(callback.from_user.id)
    if not op: return
    async with async_session() as session:
        # Получаем объект канала
        ch_query = select(TargetChannel).where(TargetChannel.tg_id == tg_id)
        channel = (await session.execute(ch_query)).scalar_one_or_none()
        if not channel:
            await callback.answer("❌ Канал не найден в базе.")
            return
        # 2. Пересылка новых постов (Лента)
        posts_query = select(PotentialPost).where(
            PotentialPost.source_tg_id == tg_id,
            PotentialPost.source_msg_id > channel.last_read_post_id,
            PotentialPost.post_type == "monitoring"
        ).order_by(PotentialPost.source_msg_id.asc())
        new_posts = (await session.execute(posts_query)).scalars().all()
        if new_posts:
            await callback.message.answer(f"⬇️ <b>Новые посты ({len(new_posts)} шт):</b>", parse_mode="HTML")
            max_id = channel.last_read_post_id
            for p in new_posts:
                try:
                    # Используем глобальную переменную MONITOR_STORAGE из начала файла
                    await bot.forward_message(callback.message.chat.id, MONITOR_STORAGE, p.storage_msg_id)
                    if p.source_msg_id > max_id: 
                        max_id = p.source_msg_id
                except Exception as e:
                    print(f"Ошибка пересылки поста {p.id}: {e}")
            # Обновляем курсор прочитанного
            channel.last_read_post_id = max_id
            await session.commit()
        else:
            await callback.answer("🧐 Новых постов в ленте нет.")
        # 3. ПОИСК ПАСПОРТОВ (фильтруем по типу и статусу)
        # Важно: берем и active, и finished (чтобы видеть итоги)
        p_query = select(ContestPassport).join(PotentialPost, ContestPassport.post_id == PotentialPost.id).\
            where(PotentialPost.source_tg_id == tg_id, 
                  ContestPassport.type == c_type,
                  ContestPassport.status.in_(["active", "finished"]))
        passports = (await session.execute(p_query)).scalars().all()
    # 4. Вывод карточек управления
    if not passports:
        await callback.message.answer("⚠️ Все активные задачи в этом канале завершены.")
        return
    for passp in passports:
        status_icon = "🟢 В работе" if passp.status == "active" else "🏁 Завершен (Ожидание)"
        summary = (
            f"📝 <b>Паспорт конкурса #{passp.id}</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"📊 Статус: <b>{status_icon}</b>\n"
            f"🔹 Тип: <code>{passp.type}</code>\n"
            f"🔹 Приз: <code>{passp.prize_type}</code>\n"
            f"🔹 Интенсивность: <code>{passp.intensity_level} ур.</code>"
        )
        builder = InlineKeyboardBuilder()
        if c_type == "afk":
            builder.row(types.InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"edit_{passp.id}"))
            builder.row(types.InlineKeyboardButton(text="👥 Добавить группы", callback_data=f"addgr_{passp.id}"))
            builder.row(types.InlineKeyboardButton(text="📢 Разослать другим", callback_data=f"share_{passp.id}"))
        else: # vote
            builder.row(types.InlineKeyboardButton(text="🗳 Рапорт Голосования", callback_data=f"v_rep_{passp.id}"))
            builder.row(types.InlineKeyboardButton(text="⭐ Отправить звезды", callback_data=f"stars_{passp.id}"))
            builder.row(types.InlineKeyboardButton(text="👥 Добавить группы", callback_data=f"addgr_{passp.id}"))
        builder.row(types.InlineKeyboardButton(text="🛑 Остановить мониторинг", callback_data=f"stop_{passp.id}"))
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
# --- 2. ФИНАЛЬНОЕ СОХРАНЕНИЕ (УБЕДИСЬ, ЧТО ИМЕНА СОВПАДАЮТ) ---
@dp.callback_query(ContestForm.v_rep_confirm, F.data == "final_v_confirm")
async def save_voting_report_final(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    async with async_session() as session:
        new_report = VotingReport(
            passport_id=data['v_passport_id'],
            target_msg_id=data['v_target_msg_id'],
            target_chat_id=data['v_target_chat_id'],
            vote_type=data['v_method'],
            # ПРОВЕРЬ ЭТУ СТРОКУ: берем именно v_option
            option_id=str(data.get('v_option')), 
            target_groups=data['v_selected_groups'],
            accounts_count=data.get('v_rep_count', 0),
            intensity=int(data['v_intensity']),
            created_by=callback.from_user.id,
            status="pending"
        )
        session.add(new_report)
        await session.commit()
    await state.clear()
    await callback.message.edit_text("✅ <b>Рапорт успешно отправлен!</b>", parse_mode="HTML")
    await callback.answer()
# --- ОТМЕНА РАПОРТА ---
@dp.callback_query(ContestForm.v_rep_confirm, F.data == "final_v_cancel")
async def cancel_voting_report_final(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Создание рапорта отменено.")
    await callback.answer()
@dp.callback_query(F.data == "final_v_cancel")
async def cancel_voting_report(callback: types.CallbackQuery, state: FSMContext):
    # Весь внутренний код функции остается прежним!
    await state.clear()
    await callback.message.edit_text("❌ Создание рапорта отменено.")
    await callback.answer()
# --- 1. СТАРТ: ВЫБОР ГРУПП (ТОЛЬКО JOINED) ---
@dp.callback_query(F.data.startswith("v_rep_"))
async def start_voting_report(callback: types.CallbackQuery, state: FSMContext):
    # 1. Извлекаем ID паспорта из callback_data (v_rep_ID)
    passport_id = int(callback.data.split("_")[2])
    async with async_session() as session:
        # 2. Достаем Паспорт и ID канала одним запросом
        res = await session.execute(
            select(ContestPassport, PotentialPost.source_tg_id)
            .join(PotentialPost, ContestPassport.post_id == PotentialPost.id)
            .where(ContestPassport.id == passport_id)
        )
        row = res.first()
        if not row:
            await callback.answer("❌ Паспорт не найден!", show_alert=True)
            return
        passport, tg_id = row
        # 3. Извлекаем данные регистрации из паспорта (для контроля оператора)
        v_details = passport.conditions.get("vote_details", {})
        reg_info = (
            f"⚠️ <b>КОНТРОЛЬ ДАННЫХ ПАСПОРТА:</b>\n"
            f"👤 Регались как: <code>{v_details.get('reg_data', 'Нет данных')}</code>\n"
            f"📍 Место: <code>{v_details.get('reg_place', 'Нет данных')}</code>\n"
            f"📸 Фото: {'Загружено ✅' if v_details.get('reg_media_id') else 'Нет ❌'}\n"
            f"━━━━━━━━━━━━━━\n"
            f"<i>Старший оператор будет сверять эти данные с целью накрутки!</i>"
        )
        # 4. Ищем группы, которые прошли инвайт (как и было)
        query = select(GroupChannelRelation.group_tag).where(
            GroupChannelRelation.channel_id == tg_id,
            GroupChannelRelation.status == 'joined'
        )
        res_gr = await session.execute(query)
        available_groups = [r[0] for r in res_gr.all()]
    if not available_groups:
        await callback.answer("⚠️ Нет групп, прошедших инвайт в этот канал!", show_alert=True)
        return
    # 5. Сохраняем всё в FSM (включая данные из паспорта для финала)
    await state.update_data(
        v_passport_id=passport_id, 
        v_available_groups=available_groups, 
        v_selected_groups=[],
        v_original_reg_data=v_details.get('reg_data') # Сохраняем для проверки в конце
    )
    # 6. Формируем интерфейс
    builder = InlineKeyboardBuilder()
    for g in available_groups:
        builder.row(types.InlineKeyboardButton(text=f"Группа {g}", callback_data=f"vsel_{g}"))
    builder.row(types.InlineKeyboardButton(text="➡️ Далее", callback_data="vsel_done"))
    await state.set_state(ContestForm.v_rep_choose_groups)
    # Сначала шлем инфо-блок с данными из паспорта, чтобы оператор не «забыл», на кого крутить
    await callback.message.answer(reg_info, parse_mode="HTML")
    # Затем шлем выбор групп
    await callback.message.answer(
        "👥 <b>Выберите группы для голосования:</b>", 
        reply_markup=builder.as_markup(), 
        parse_mode="HTML"
    )
    await callback.answer()
# --- 2. ОБРАБОТКА ГАЛОЧЕК И ВЫБОР КОЛИЧЕСТВА ---
@dp.callback_query(ContestForm.v_rep_choose_groups, F.data.startswith("vsel_"))
async def process_v_groups(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("v_selected_groups", [])
    if callback.data == "vsel_done":
        if not selected:
            await callback.answer("Выберите хотя бы одну группу!", show_alert=True)
            return
        if len(selected) == 1:
            await state.set_state(ContestForm.v_rep_count)
            await callback.message.edit_text(f"🔢 <b>Выбрана Группа {selected[0]}</b>\nВведите количество исполнителей:")
        else:
            await state.update_data(v_rep_count=0) # Все
            await state.set_state(ContestForm.v_rep_fwd)
            await callback.message.edit_text("🗳 <b>Группы выбраны.</b>\nПерешлите пост-голосование из канала:")
        return
    group = callback.data.replace("vsel_", "")
    if group in selected: selected.remove(group)
    else: selected.append(group)
    await state.update_data(v_selected_groups=selected)
    builder = InlineKeyboardBuilder()
    for g in data['v_available_groups']:
        mark = " ✅" if g in selected else ""
        builder.row(types.InlineKeyboardButton(text=f"Группа {g}{mark}", callback_data=f"vsel_{g}"))
    builder.row(types.InlineKeyboardButton(text="➡️ Далее", callback_data="vsel_done"))
    await callback.message.edit_reply_markup(reply_markup=builder.as_markup())
# --- 3. ПРИЕМ КОЛИЧЕСТВА (ДЛЯ ОДНОЙ ГРУППЫ) ---
@dp.message(ContestForm.v_rep_count)
async def process_v_count(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Введите число!")
        return
    await state.update_data(v_rep_count=int(message.text))
    await state.set_state(ContestForm.v_rep_fwd)
    await message.answer("🗳 Теперь перешлите пост-голосование из канала:")
# --- 4. ПРИЕМ ПОСТА И СПОСОБ ---
@dp.message(ContestForm.v_rep_fwd)
async def process_v_fwd(message: types.Message, state: FSMContext):
    if not message.forward_from_message_id:
        await message.answer("❌ Нужно именно ПЕРЕСЛАТЬ пост!")
        return
    await state.update_data(v_target_msg_id=message.forward_from_message_id, v_target_chat_id=message.forward_from_chat.id)
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="📊 Опрос", callback_data="v_meth_poll"),
                types.InlineKeyboardButton(text="🔥 Реакция", callback_data="v_meth_reac"))
    await state.set_state(ContestForm.v_rep_method)
    await message.answer("Выберите способ:", reply_markup=builder.as_markup())
# --- 5. ВАРИАНТ (ОПЦИЯ) ---
@dp.callback_query(ContestForm.v_rep_method)
async def process_v_method(callback: types.CallbackQuery, state: FSMContext):
    method = callback.data.replace("v_meth_", "")
    await state.update_data(v_method=method)
    prompt = "Введите номер варианта (1, 2...):" if method == "poll" else "Введите ID/Эмодзи реакции:"
    await state.set_state(ContestForm.v_rep_option)
    await callback.message.edit_text(prompt)
# --- 1. ПРИЕМ ВАРИАНТА ---
@dp.message(ContestForm.v_rep_option)
async def process_v_option(message: types.Message, state: FSMContext):
    # Сохраняем текст сообщения (будь то "1", "🏀" или ID)
    await state.update_data(v_option=message.text) 
    await ask_v_intensity(message, state)
# --- 6. ИНТЕНСИВНОСТЬ И ФИНАЛ ---
async def ask_v_intensity(message, state: FSMContext):
    await state.set_state(ContestForm.v_rep_intensity)
    await message.answer("🚀 Выберите интенсивность:", reply_markup=get_intensity_kb())
@dp.callback_query(ContestForm.v_rep_intensity)
async def process_v_intensity(callback: types.CallbackQuery, state: FSMContext):
    intensity = callback.data.replace("int_", "")
    await state.update_data(v_intensity=intensity)
    data = await state.get_data()
    summary = (
        f"📊 <b>ПРЕДПРОСМОТР РАПОРТА</b>\n"
        f"📍 Пост: <code>{data['v_target_msg_id']}</code>\n"
        f"🛠 Метод: <code>{data['v_method']}</code>\n"
        f"👥 Группы: <code>{', '.join(data['v_selected_groups'])}</code>\n"
        f"🔢 Кол-во: <code>{'Все' if data['v_rep_count'] == 0 else data['v_rep_count']}</code>\n"
        f"🚀 Интенсивность: {intensity} ур."
    )
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="📤 Отправить Старшему", callback_data="final_v_confirm"),
                types.InlineKeyboardButton(text="❌ Отмена", callback_data="final_v_cancel"))
    await state.set_state(ContestForm.v_rep_confirm)
    await callback.message.edit_text(summary, reply_markup=builder.as_markup(), parse_mode="HTML")
@dp.message(F.text == "🛡 Админ-панель")
async def admin_panel(message: types.Message):
    op = await get_operator(message.from_user.id)
    if not op or op.rank < 2: return
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🗳 Рапорты Голосования", callback_data="adm_list_vote"))
    builder.row(types.InlineKeyboardButton(text="⭐ Рапорты на Звезды", callback_data="adm_list_stars"))
    builder.row(types.InlineKeyboardButton(text="🔎 Проверка новых ТГК", callback_data="adm_list_new_tgc"))
    await message.answer(
        "🛠 <b>Панель управления (Rank 2)</b>\nВыберите категорию для проверки:", 
        reply_markup=builder.as_markup(), 
        parse_mode="HTML"
    )
# --- СОХРАНЕНИЕ РАПОРТА (ОТПРАВКА СТАРШЕМУ) ---
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
            target_groups=data['selected_groups'],  # JSON список
            accounts_count=data['v_rep_count'],
            intensity=int(data['v_intensity']),
            created_by=callback.from_user.id,
            status="pending"
        )
        session.add(new_report)
        await session.commit()
    await state.clear()
    await callback.message.edit_text("✅ <b>Рапорт успешно отправлен!</b>\nОн появится в списке ожидания у Старшего Оператора.", parse_mode="HTML")
    await callback.answer()
# --- АДМИНКА: ПРОСМОТР PENDING РАПОРТОВ ---
# Вместо startswith используем прямое сравнение
@dp.callback_query(F.data == "adm_list_vote")
async def admin_view_pending_extended(callback: types.CallbackQuery):
    op = await get_operator(callback.from_user.id)
    if op.rank < 2: return
    async with async_session() as session:
        # Тянем Рапорт + Паспорт + Оригинальный пост конкурса (PotentialPost)
        query = select(VotingReport, ContestPassport, PotentialPost).\
            join(ContestPassport, VotingReport.passport_id == ContestPassport.id).\
            join(PotentialPost, ContestPassport.post_id == PotentialPost.id).\
            where(VotingReport.status == "pending").order_by(VotingReport.id.asc())
        results = (await session.execute(query)).all()
    if not results:
        await callback.message.edit_text("📭 Новых рапортов на голосование нет.")
        return
    for report, passport, post in results:
        # --- ШАГ 1: ПОКАЗЫВАЕМ СТАРШЕМУ ОРИГИНАЛЬНЫЙ КОНКУРС ---
        # Чтобы он видел, что вообще за розыгрыш
        await bot.forward_message(callback.message.chat.id, MONITOR_STORAGE, post.storage_msg_id)
        # --- ШАГ 2: ПОКАЗЫВАЕМ СКРИНШОТ РЕГИСТРАЦИИ (Если был) ---
        # Из данных паспорта достаем медиа, которое оператор загрузил при реге лида
        reg_media = passport.conditions.get("vote_details", {}).get("reg_media_id")
        if reg_media:
            await bot.copy_message(callback.message.chat.id, MONITOR_STORAGE, reg_media, 
                                 caption="📸 <b>Фото/Скрин регистрации из Паспорта</b>", parse_mode="HTML")
        # --- ШАГ 3: КАРТОЧКА СЛИЧЕНИЯ ДАННЫХ ---
        v_details = passport.conditions.get("vote_details", {})
        summary = (
            f"⚖️ <b>СЛИЧЕНИЕ ДАННЫХ РАПОРТА #{report.id}</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"📝 <b>В ПАСПОРТЕ (Регистрация):</b>\n"
            f"└ Аккаунт: <code>{v_details.get('executor')}</code>\n"
            f"└ Данные: <code>{v_details.get('reg_data')}</code>\n"
            f"└ Куда: <code>{v_details.get('reg_place')}</code>\n"
            f"━━━━━━━━━━━━━━\n"
            f"📊 <b>В РАПОРТЕ (Накрутка):</b>\n"
            f"└ Метод: <code>{report.vote_type.upper()}</code>\n"
            f"└ Вариант/ID: <b>{report.option_id}</b>\n"
            f"└ Группы: <code>{', '.join(report.target_groups)}</code>\n"
            f"━━━━━━━━━━━━━━\n"
            f"❓ <i>Совпадают ли данные регистрации в паспорте с целью в голосовании?</i>"
        )
        builder = InlineKeyboardBuilder()
        builder.row(
            types.InlineKeyboardButton(text="✅ Данные верны (Одобрить)", callback_data=f"adm_appr_{report.id}"),
            types.InlineKeyboardButton(text="❌ ПОДМЕНА (Отклонить)", callback_data=f"adm_decl_{report.id}")
        )
        # Пересылаем сам пост-голосование (на который идет накрутка)
        await bot.forward_message(callback.message.chat.id, callback.message.chat.id, report.target_msg_id)
        await callback.message.answer(summary, reply_markup=builder.as_markup(), parse_mode="HTML")
# --- ИСПРАВЛЕННЫЙ ПРИЕМ РЕШЕНИЯ СТАРШЕГО ---
@dp.callback_query(F.data.startswith("adm_appr_")) # Для кнопок Одобрить
@dp.callback_query(F.data.startswith("adm_decl_")) # Для кнопок Отклонить
async def process_report_decision(callback: types.CallbackQuery):
    # Разбираем данные: adm_appr_ID или adm_decl_ID
    parts = callback.data.split("_")
    action = parts[1]     # 'appr' или 'decl'
    report_id = int(parts[2])
    # Четко прописываем статус
    if action == "appr":
        new_status = "approved"
        status_text = "🟢 ОДОБРЕН"
    else:
        new_status = "declined"
        status_text = "🔴 ОТКЛОНЕН"
    async with async_session() as session:
        # Обновляем статус рапорта в базе
        await session.execute(
            update(VotingReport)
            .where(VotingReport.id == report_id)
            .values(status=new_status)
        )
        await session.commit()
    await callback.message.edit_text(
        f"⚖️ Рапорт #{report_id} изменен на: <b>{status_text}</b>\n"
        f"<i>Исполнители получили задачу.</i>", 
        parse_mode="HTML"
    )
    await callback.answer()
@dp.callback_query(F.data.startswith("addgr_"))
async def start_add_extra_group(callback: types.CallbackQuery, state: FSMContext):
    """Шаг 1: Оператор выбирает, какую группу добавить как дополнительную в ТГК"""
    passport_id = int(callback.data.split("_")[1])
    await state.update_data(current_passport_id=passport_id)
    async with async_session() as session:
        # 1. Находим ТГК через паспорт
        res = await session.execute(
            select(TargetChannel).join(ContestPassport, ContestPassport.post_id == PotentialPost.id).where(ContestPassport.id == passport_id)
        )
        channel = res.scalar_one_or_none()
        if not channel: return
        # 2. Собираем список групп, которые уже привязаны (осн + доп)
        existing_groups = [channel.group_tag]
        if channel.extra_groups:
            existing_groups.extend(channel.extra_groups)
        # 3. Находим все уникальные группы из БД воркеров, которых ТУТ ЕЩЕ НЕТ
        res_all = await session.execute(text("SELECT DISTINCT group_tag FROM workers.workers"))
        all_tags = [row[0] for row in res_all.all()]
        available_groups = [g for g in all_tags if g not in existing_groups]
    if not available_groups:
        await callback.answer("✅ Все группы уже привязаны к этому каналу!", show_alert=True)
        return
    builder = InlineKeyboardBuilder()
    for g in available_groups:
        builder.row(types.InlineKeyboardButton(text=f"➕ Добавить Группу {g}", callback_data=f"conf_add_{g}"))
    await callback.message.edit_text(
        f"👥 <b>Добавление ресурсов в ТГК</b>\nВыберите группу, которая должна вступить в канал как дополнительная:", 
        reply_markup=builder.as_markup(), 
        parse_mode="HTML"
    )
@dp.callback_query(F.data.startswith("conf_add_"))
async def propose_extra_group(callback: types.CallbackQuery, state: FSMContext):
    group_to_add = callback.data.replace("conf_add_", "")
    data = await state.get_data()
    passport_id = data['current_passport_id']
    async with async_session() as session:
        # 1. Получаем ID канала через паспорт
        res = await session.execute(
            select(TargetChannel)
            .join(PotentialPost, PotentialPost.source_tg_id == TargetChannel.tg_id)
            .join(ContestPassport, ContestPassport.post_id == PotentialPost.id)
            .where(ContestPassport.id == passport_id)
        )
        channel = res.scalar_one_or_none()
        if not channel:
            await callback.answer("❌ Ошибка: Канал не найден.")
            return
        # 2. Создаем запись об инвайт-процессе (сразу в статусе 'inviting')
        new_rel = GroupChannelRelation(
            group_tag=group_to_add,
            channel_id=channel.tg_id,
            status='inviting',        # Сразу ставим активный статус
            invite_started_at=func.now() # Засекаем время начала (24ч пошли)
        )
        session.add(new_rel)
        # 3. ОБНОВЛЯЕМ РЕСУРСЫ КАНАЛА (Авто-одобрение)
        # Добавляем в список доп. групп
        extras = list(channel.extra_groups) if channel.extra_groups else []
        if group_to_add not in extras:
            extras.append(group_to_add)
        channel.extra_groups = extras
        # Прописываем задачу на вступление ('join') и статус ожидания ('pending')
        configs = dict(channel.actions_config) if channel.actions_config else {}
        syncs = dict(channel.sync_status) if channel.sync_status else {}
        configs[group_to_add] = "join"
        syncs[group_to_add] = "pending"
        channel.actions_config = configs
        channel.sync_status = syncs
        await session.commit()
    await callback.message.edit_text(
        f"🚀 <b>Группа {group_to_add} добавлена автоматически!</b>\n"
        f"Процесс инвайтинга запущен (24 часа).\n"
        f"Воркеры группы получили команду на вступление.", 
        parse_mode="HTML"
    )
    await state.clear()
@dp.callback_query(ContestForm.choosing_group_to_invite, F.data.startswith("do_inv_"))
async def process_inviting(callback: types.CallbackQuery, state: FSMContext):
    group_tag = callback.data.replace("do_inv_", "")
    data = await state.get_data()
    passport_id = data['current_passport_id']
    async with async_session() as session:
        # Получаем ID канала
        res = await session.execute(
    select(PotentialPost.source_tg_id).join(ContestPassport).where(ContestPassport.id == passport_id)
)
        tg_id = res.scalar()       
        # СОЗДАЕМ ЗАПИСЬ (Заявку), которую увидит Админ
        new_rel = GroupChannelRelation(
            group_tag=group_tag,
            channel_id=tg_id,
            status='not_joined' # Админка rank 2 ищет именно этот статус
        )
        session.add(new_rel)
        await session.commit()
    await callback.message.edit_text(
        f"📨 <b>Заявка отправлена!</b>\nСтарший оператор должен подтвердить инвайтинг Группы {group_tag}.\n"
        f"После одобрения начнется процесс вступления (24 часа).", 
        parse_mode="HTML"
    )
    await state.clear()
# --- 1. СТАРТ: УЗНАЕМ КТО ИСПОЛНИТЕЛЬ ИЗ ПАСПОРТА ---
@dp.callback_query(F.data.startswith("stars_"))
async def start_stars_report(callback: types.CallbackQuery, state: FSMContext):
    # Разбираем ID паспорта из кнопки (stars_ID)
    passport_id = int(callback.data.split("_")[1])
    async with async_session() as session:
        # Достаем данные паспорта, чтобы найти Лид-исполнителя (executor)
        res = await session.execute(select(ContestPassport).where(ContestPassport.id == passport_id))
        passport = res.scalar_one_or_none()
        if not passport:
            await callback.answer("❌ Паспорт не найден в базе.", show_alert=True)
            return
        # Ищем в JSON-поле conditions данные об исполнителе
        executor = passport.conditions.get("vote_details", {}).get("executor")
        if not executor:
            await callback.answer("❌ В паспорте этого конкурса не указан исполнитель-участник!", show_alert=True)
            return
    # Сохраняем ID паспорта и ID исполнителя в память бота
    await state.update_data(star_passport_id=passport_id, star_executor=executor)
    await state.set_state(ContestForm.star_target)
    await callback.message.answer(
        f"⭐ <b>Рапорт на Звезды</b>\n"
        f"👤 Платит исполнитель: <code>{executor}</code>\n\n"
        f"Введите <b>@username</b> организатора, кому шлем звезды:", 
        parse_mode="HTML"
    )
    await callback.answer()
# --- 2. ВЫБОР ТИПА ПОДАРКА (КНОПКАМИ) ---
@dp.message(ContestForm.star_target)
async def star_target_proc(message: types.Message, state: FSMContext):
    target = message.text.strip()
    await state.update_data(s_target=target)
    data = await state.get_data()
    executor_id = data['star_executor']
    async with async_session() as session:
        # Получаем актуальный баланс воркера из БД
        res = await session.execute(select(WorkerAccount.stars_balance).where(WorkerAccount.tg_id == int(executor_id)))
        balance = res.scalar() or 0
    await state.set_state(ContestForm.star_amount)
    # Кнопки для быстрой вставки суммы
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="25 ⭐", callback_data="amt_25"),
        types.InlineKeyboardButton(text="50 ⭐", callback_data="amt_50"),
        types.InlineKeyboardButton(text="100 ⭐", callback_data="amt_100")
    )
    await message.answer(
        f"💰 <b>Баланс исполнителя:</b> <code>{balance} ⭐</code>\n\n"
        f"Введите <b>стоимость подарка</b> (25, 50 или 100) или выберите кнопкой:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
@dp.callback_query(ContestForm.star_amount, F.data.startswith("amt_"))
@dp.message(ContestForm.star_amount)
async def star_amount_proc(event, state: FSMContext):
    # Получаем сумму из кнопки или текста
    amount_raw = event.data.replace("amt_", "") if isinstance(event, types.CallbackQuery) else event.text
    if not amount_raw.isdigit() or int(amount_raw) not in [25, 50, 100]:
        msg = event.message if isinstance(event, types.CallbackQuery) else event
        await msg.answer("❌ Ошибка: Введите ровно 25, 50 или 100 звезд.")
        return
    amount = int(amount_raw)
    # Авто-подбор названия подарка для системной логики (по минимальной цене)
    gift_names = {25: "Роза", 50: "Торт", 100: "Кубок"}
    await state.update_data(s_amount=amount, s_gift=gift_names[amount])
    await state.set_state(ContestForm.star_reason)
    message = event.message if isinstance(event, types.CallbackQuery) else event
    await message.answer("📝 <b>Введите причину отправки:</b>\n(Например: подарок админу за победу)")
    if isinstance(event, types.CallbackQuery): await event.answer()
# --- 3. ВЫБОР ПОДАРКА И АВТО-ПЕРЕХОД К ФИНАЛУ ---
@dp.callback_query(ContestForm.star_gift_type)
async def star_gift_proc(callback: types.CallbackQuery, state: FSMContext):
    gift_name = callback.data.replace("sgift_", "")
    await state.update_data(s_gift=gift_name, s_amount=0) 
    # ПЕРЕХОДИМ К ПРИЧИНЕ (Вместо финала)
    await state.set_state(ContestForm.star_reason)
    await callback.message.edit_text("📝 <b>Введите причину отправки:</b>\n(Например: требование админа для финала)", parse_mode="HTML")
    await callback.answer()
@dp.message(ContestForm.star_reason)
async def star_reason_proc(message: types.Message, state: FSMContext):
    await state.update_data(s_reason=message.text)
    await state.set_state(ContestForm.star_proof)
    await message.answer("📸 <b>Отправьте скриншот-доказательство:</b>\n(Перешлите сообщение или загрузите фото)", parse_mode="HTML")
@dp.message(ContestForm.star_proof, F.photo | F.document)
async def star_proof_proc(message: types.Message, state: FSMContext):
    # Пересылаем скриншот в хранилище, чтобы Старший его увидел
    fwd = await message.forward(MONITOR_STORAGE)
    await state.update_data(s_proof_id=fwd.message_id)
    # Теперь вызываем финальную карточку
    await show_star_summary(message, state)
async def show_star_summary(message: types.Message, state: FSMContext):
    data = await state.get_data()
    summary = (
        f"🚨 <b>РАПОРТ НА ЗВЕЗДЫ (ПРОВЕРКА)</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"👤 <b>Отправитель:</b> <code>{data['star_executor']}</code>\n"
        f"🎯 <b>Получатель:</b> <code>{data['s_target']}</code>\n"
        f"🎁 <b>Подарок:</b> {data['s_gift']}\n"
        f"📝 <b>Причина:</b> <i>{data['s_reason']}</i>\n"
        f"📸 <b>Скриншот:</b> Прикреплен ✅\n"
        f"━━━━━━━━━━━━━━\n"
        f"Отправить Старшему оператору на одобрение?"
    )
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="✅ Отправить", callback_data="star_final_confirm"),
        types.InlineKeyboardButton(text="❌ Отмена", callback_data="star_final_cancel")
    )
    await state.set_state(ContestForm.star_confirm)
    # Если это текстовое сообщение (после фото), шлем новое. Если колбэк — редактируем.
    if message.photo or message.document:
        await message.answer(summary, reply_markup=builder.as_markup(), parse_mode="HTML")
    else:
        await message.edit_text(summary, reply_markup=builder.as_markup(), parse_mode="HTML")
# --- 5. ФИНАЛЬНОЕ СОХРАНЕНИЕ В БАЗУ ---
@dp.callback_query(ContestForm.star_confirm, F.data == "star_final_confirm")
async def save_star_report_final(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    async with async_session() as session:
        # Пытаемся превратить executor в ID (число)
        raw_executor = data['star_executor']
        try:
            executor_id = int(raw_executor)
        except:
            executor_id = 0 # Если там никнейм, запишем 0 (нужно будет искать по нику)
        new_report = StarReport(
            passport_id=data['star_passport_id'],
            target_user=data['s_target'],
            method=data['s_gift'], # Сохраняем название подарка (Медведь и т.д.)
            star_count=data['s_amount'],
            executor_id=executor_id,
            reason=data['s_reason'],           # НОВОЕ
            proof_media_id=data['s_proof_id'],  # НОВОЕ
            status="pending"
        )
        session.add(new_report)
        await session.commit()
    await state.clear()
    await callback.message.edit_text("✅ <b>Рапорт отправлен!</b>\nОжидайте одобрения Старшим оператором.")
    await callback.answer()
@dp.callback_query(ContestForm.star_confirm, F.data == "star_final_cancel")
async def cancel_star_report(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Создание рапорта отменено.")
    await callback.answer()
@dp.callback_query(F.data.startswith("share_"))
async def start_sharing_contest(callback: types.CallbackQuery, state: FSMContext):
    passport_id = int(callback.data.split("_")[1])
    op = await get_operator(callback.from_user.id)
    async with async_session() as session:
        # 1. Находим ID канала через паспорт
        res = await session.execute(
            select(PotentialPost.source_tg_id).join(ContestPassport).where(ContestPassport.id == passport_id)
        )
        tg_id = res.scalar()
        # 2. Находим группы, которые УЖЕ ПРОШЛИ инвайтинг (статус 'joined')
        # КРОМЕ текущей группы оператора
        query = select(GroupChannelRelation.group_tag).where(
            GroupChannelRelation.channel_id == tg_id,
            GroupChannelRelation.status == 'joined',
            GroupChannelRelation.group_tag != op.group_tag
        )
        res_gr = await session.execute(query)
        available_groups = [row[0] for row in res_gr.all()]
    if not available_groups:
        await callback.answer("⚠️ Нет других групп, прошедших инвайтинг в этот канал!", show_alert=True)
        return
    await state.update_data(share_passport_id=passport_id, share_selected_groups=[])
    builder = InlineKeyboardBuilder()
    for g in available_groups:
        builder.row(types.InlineKeyboardButton(text=f"Группа {g}", callback_data=f"do_sh_{g}"))
    builder.row(types.InlineKeyboardButton(text="➡️ Разослать выбранным", callback_data="do_sh_confirm"))
    await state.set_state(ContestForm.sharing_to_groups)
    await callback.message.answer("📢 <b>Рассылка другим группам</b>\nВыберите группы, которым отправить этот конкурс:", reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()
@dp.callback_query(ContestForm.sharing_to_groups, F.data.startswith("do_sh_"))
async def process_sharing_choice(callback: types.CallbackQuery, state: FSMContext):
    # Если нажали "Подтвердить"
    if callback.data == "do_sh_confirm":
        data = await state.get_data()
        selected = data.get("share_selected_groups", [])
        if not selected:
            await callback.answer("⚠️ Выберите хотя бы одну группу!", show_alert=True)
            return
        async with async_session() as session:
            # Получаем данные оригинального поста
            res = await session.execute(
                select(PotentialPost).join(ContestPassport).where(ContestPassport.id == data['share_passport_id'])
            )
            original = res.scalar_one()
            # Дублируем пост для выбранных групп
            for group in selected:
                new_share = PotentialPost(
                    group_tag=group,
                    storage_msg_id=original.storage_msg_id,
                    source_tg_id=original.source_tg_id,
                    source_msg_id=original.source_msg_id,
                    keyword_hit=f"📢 ОТ ГРУППЫ {data.get('group_tag', 'A1')}",
                    post_type="share",
                    is_claimed=False,
                    published_at=original.published_at
                )
                session.add(new_share)
            await session.commit()
        await callback.message.edit_text(f"✅ Конкурс успешно разослан группам: {', '.join(selected)}")
        await state.clear()
        await callback.answer()
        return
    # Логика переключения галочек
    group_tag = callback.data.replace("do_sh_", "")
    data = await state.get_data()
    selected = data.get("share_selected_groups", [])
    if group_tag in selected:
        selected.remove(group_tag)
    else:
        selected.append(group_tag)
    await state.update_data(share_selected_groups=selected)
    # Перерисовываем клавиатуру (нужно снова достать доступные группы из БД или хранить в state)
    # Для быстроты просто обновим текущую клавиатуру
    builder = InlineKeyboardBuilder()
    # (Здесь в идеале нужно снова запросить список групп из БД как в первой функции)
    # Чтобы не усложнять, пока просто меняем текст кнопки:
    for row in callback.message.reply_markup.inline_keyboard:
        for btn in row:
            if btn.callback_data == callback.data:
                btn.text = f"Группа {group_tag} ✅" if group_tag in selected else f"Группа {group_tag}"
            builder.row(btn)
    await callback.message.edit_reply_markup(reply_markup=callback.message.reply_markup)
    await callback.answer()
@dp.callback_query(F.data == "adm_list_stars")
async def adm_view_stars(callback: types.CallbackQuery):
    async with async_session() as session:
        query = select(StarReport, ContestPassport).join(ContestPassport).\
            where(StarReport.status == "pending").order_by(StarReport.created_at.asc())
        results = (await session.execute(query)).all()
    if not results:
        await callback.message.edit_text("✨ Нет активных заявок на Звезды.")
        return
    for report, passport in results:
        summary = (
            f"⭐ <b>ЗАЯВКА НА ЗВЕЗДЫ #{report.id}</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"💰 Сумма: <b>{report.star_count} ⭐</b>\n"
            f"🎁 Подарок: <code>{report.method}</code>\n"
            f"👤 Кому: <code>{report.target_user}</code>\n"
            f"📝 Причина: <i>{report.reason}</i>\n"
            f"🤖 Исполнитель: <code>{report.executor_id}</code>\n"
            f"━━━━━━━━━━━━━━"
        )
        builder = InlineKeyboardBuilder()
        builder.row(
            types.InlineKeyboardButton(text="✅ Одобрить", callback_data=f"starappr_ok_{report.id}"),
            types.InlineKeyboardButton(text="❌ Отклонить", callback_data=f"starappr_no_{report.id}")
        )
        if report.proof_media_id:
            await bot.copy_message(callback.message.chat.id, MONITOR_STORAGE, report.proof_media_id)
        await callback.message.answer(summary, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()
@dp.callback_query(F.data.startswith("starappr_"))
async def process_star_decision(callback: types.CallbackQuery):
    _, decision, r_id = callback.data.split("_")
    new_status = "approved" if decision == "ok" else "declined"
    async with async_session() as session:
        await session.execute(update(StarReport).where(StarReport.id == int(r_id)).values(status=new_status))
        await session.commit()
    txt = "🟢 ОДОБРЕНО" if decision == "ok" else "🔴 ОТКЛОНЕНО"
    await callback.message.edit_text(f"⚖️ Рапорт на звезды #{r_id}: <b>{txt}</b>", parse_mode="HTML")
@dp.callback_query(F.data == "adm_list_invite")
async def adm_view_invites(callback: types.CallbackQuery):
    async with async_session() as session:
        # Ищем вступления со статусом 'not_joined' или 'inviting' (которые ждут ручного пуска)
        query = select(GroupChannelRelation, TargetChannel.username).\
            join(TargetChannel, TargetChannel.tg_id == GroupChannelRelation.channel_id).\
            where(GroupChannelRelation.status == 'not_joined').limit(10)
        results = (await session.execute(query)).all()
    if not results:
        await callback.message.edit_text("👥 Нет новых заявок на инвайтинг.")
        return
    for rel, ch_name in results:
        summary = (
            f"👥 <b>ЗАПРОС НА ИНВАЙТ</b>\n"
            f"📦 <b>Группа:</b> {rel.group_tag}\n"
            f"📢 <b>Канал:</b> {ch_name or rel.channel_id}\n"
            f"🕒 <b>Длительность:</b> 24 часа"
        )
        builder = InlineKeyboardBuilder()
        builder.row(
            types.InlineKeyboardButton(text="✅ Начать инвайт", callback_data=f"invappr_ok_{rel.id}"),
            types.InlineKeyboardButton(text="❌ Отмена", callback_data=f"invappr_no_{rel.id}")
        )
        await callback.message.answer(summary, reply_markup=builder.as_markup(), parse_mode="HTML")
@dp.callback_query(F.data.startswith("invappr_"))
async def process_invite_decision(callback: types.CallbackQuery):
    _, decision, rel_id = callback.data.split("_")
    async with async_session() as session:
        if decision == "ok":
            # 1. Получаем саму заявку
            rel = await session.get(GroupChannelRelation, int(rel_id))
            # 2. Обновляем статус вступления (запускаем 24ч цикл)
            rel.status = "inviting"
            rel.invite_started_at = func.now()
            # 3. --- НОВЫЙ КИРПИЧИК: ОБНОВЛЯЕМ КОНФИГ КАНАЛА ---
            ch = await session.get(TargetChannel, rel.channel_id)
            if ch:
                # Добавляем группу в extra_groups (JSONB список)
                extras = list(ch.extra_groups) if ch.extra_groups else []
                if rel.group_tag not in extras:
                    extras.append(rel.group_tag)
                ch.extra_groups = extras
                # Прописываем действие 'join' и статус 'pending' (не готова)
                configs = dict(ch.actions_config) if ch.actions_config else {}
                syncs = dict(ch.sync_status) if ch.sync_status else {}
                configs[rel.group_tag] = "join"
                syncs[rel.group_tag] = "pending" # <--- Это даст сигнал воркерам
                ch.actions_config = configs
                ch.sync_status = syncs
            txt = f"🚀 Инвайтинг Группы {rel.group_tag} запущен. Группа добавлена в ресурсы ТГК."
        else:
            txt = "🔴 Заявка на инвайтинг отклонена."
        await session.commit()
    await callback.message.edit_text(f"⚖️ Статус инвайта: <b>{txt}</b>", parse_mode="HTML")
# --- РАЗДЕЛ ЛС: СПИСОК АККАУНТОВ ГРУППЫ ---
@dp.message(F.text == "📬 ЛС исполнителей")
async def show_worker_accounts(message: types.Message):
    op = await get_operator(message.from_user.id)
    if not op: return
    async with async_session() as session:
        # Считаем непрочитанные сообщения для каждого воркера из этой "тарелки" (group_tag)
        query = text("""
            SELECT w.tg_id, COUNT(m.id) as unread_count 
            FROM workers.workers w
            LEFT JOIN workers.messages m ON w.tg_id = m.worker_tg_id AND m.is_read = False
            WHERE w.group_tag = :tag
            GROUP BY w.tg_id
        """)
        result = await session.execute(query, {"tag": op.group_tag})
        workers_data = result.all()
    if not workers_data:
        await message.answer("📭 В вашей группе пока нет активных исполнителей.")
        return
    builder = InlineKeyboardBuilder()
    for tg_id, count in workers_data:
        status = f" (✉️ {count})" if count > 0 else ""
        builder.row(types.InlineKeyboardButton(
            text=f"🤖 Аккаунт {tg_id}{status}", 
            callback_data=f"ls_acc_{tg_id}"
        ))
    await message.answer(f"📱 <b>Управление ЛС группы {op.group_tag}</b>\nВыберите аккаунт:", 
                         reply_markup=builder.as_markup(), parse_mode="HTML")
# --- РАЗДЕЛ ЛС: СПИСОК ДИАЛОГОВ ВНУТРИ АККАУНТА ---
@dp.callback_query(F.data.startswith("ls_acc_"))
async def show_dialogs(callback: types.CallbackQuery):
    worker_id = int(callback.data.split("_")[2])
    async with async_session() as session:
        # Группируем сообщения по отправителям
                # Группируем сообщения по отправителям, исключая системные ID и своих воркеров
        query = text("""
            SELECT m.sender_id, MAX(m.created_at) as last_date, COUNT(m.id) FILTER (WHERE m.is_read = False) as new_msgs
            FROM workers.messages m
            WHERE m.worker_tg_id = :wid 
            AND m.sender_id != 777000 -- Исключаем Telegram
            AND m.sender_id NOT IN (SELECT tg_id FROM workers.workers) -- Исключаем всех воркеров из БД
            GROUP BY m.sender_id
            ORDER BY last_date DESC
        """)

        result = await session.execute(query, {"wid": worker_id})
        dialogs = result.all()
    if not dialogs:
        await callback.message.edit_text("📭 У этого аккаунта пока нет входящих сообщений.")
        return
    builder = InlineKeyboardBuilder()
    for sender_id, last_date, new_count in dialogs:
        status = f" 🔥 +{new_count}" if new_count > 0 else ""
        builder.row(types.InlineKeyboardButton(
            text=f"👤 Юзер {sender_id}{status}", 
            callback_data=f"ls_view_{worker_id}_{sender_id}"
        ))
    builder.row(types.InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_accounts"))
    await callback.message.edit_text(f"📩 <b>Диалоги аккаунта {worker_id}:</b>", 
                                     reply_markup=builder.as_markup(), parse_mode="HTML")
@dp.callback_query(F.data == "back_to_accounts")
async def back_to_accounts(callback: types.CallbackQuery):
    await show_worker_accounts(callback.message)
    await callback.answer()
# --- РАЗДЕЛ ЛС: ПРОСМОТР ИСТОРИИ ЧАТА ---
# --- 1. ВЫБОР ЮЗЕРА ---
@dp.callback_query(F.data.startswith("ls_view_"))
async def view_chat_history(callback: types.CallbackQuery, state: FSMContext):
    # Разбираем: ls_view_{worker_id}_{sender_id}
    parts = callback.data.split("_")
    worker_id = int(parts[2])
    sender_id = int(parts[3])
    async with async_session() as session:
        from database.models import AccountMessage
        # Берем последние 5 сообщений (для теста, чтобы не спамить)
        query = select(AccountMessage).where(
            AccountMessage.worker_tg_id == worker_id,
            AccountMessage.sender_id == sender_id
        ).order_by(AccountMessage.created_at.desc()).limit(5)
        msgs = (await session.execute(query)).scalars().all()
        # Помечаем сообщения в БД как прочитанные
        await session.execute(
            update(AccountMessage).where(
                AccountMessage.worker_tg_id == worker_id,
                AccountMessage.sender_id == sender_id
            ).values(is_read=True)
        )
        await session.commit()
    if not msgs:
        await callback.answer("История сообщений пуста.")
        return
    await callback.message.answer(f"📜 <b>История чата с {sender_id}</b> (через {worker_id}):", parse_mode="HTML")
    # Выводим каждое сообщение отдельным постом с кнопками
    for m in reversed(msgs):
        time_str = m.created_at.strftime("%H:%M")
        caption = f"🕒 <code>[{time_str}]</code>\n{m.text or ''}"
        builder = InlineKeyboardBuilder()
        # Кнопка ОТВЕТА
        builder.row(types.InlineKeyboardButton(
            text="✍️ Ответить", 
            callback_data=f"ls_rep_{worker_id}_{sender_id}_{m.msg_id}"
        ))
        # Кнопки РЕАКЦИЙ
        reacs = ["👍", "❤️", "🔥", "🤡", "⚡️"]
        reac_btns = [
            types.InlineKeyboardButton(text=r, callback_data=f"reac_{worker_id}_{sender_id}_{m.msg_id}_{r}") 
            for r in reacs
        ]
        builder.row(*reac_btns)
        # Если есть медиа — копируем из хранилища, если нет — текстом
        if m.storage_media_id:
            try:
                await bot.copy_message(
                    chat_id=callback.message.chat.id,
                    from_chat_id=MONITOR_STORAGE,
                    message_id=m.storage_media_id,
                    caption=caption,
                    reply_markup=builder.as_markup(),
                    parse_mode="HTML"
                )
            except Exception:
                await callback.message.answer(f"🖼 [Медиа недоступно]\n{caption}", reply_markup=builder.as_markup())
        else:
            await callback.message.answer(caption, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()
# --- РАЗДЕЛ ЛС: НАЧАЛО ОТВЕТА (ИСПРАВЛЕННЫЙ) ---
@dp.callback_query(F.data.startswith("ls_rep_"))
async def start_ls_reply(callback: types.CallbackQuery, state: FSMContext):
    print(f"DEBUG: Нажата кнопка ответить! Data: {callback.data}") 
    # Разбираем данные: ls_rep_{worker_id}_{sender_id}_{msg_id}
    parts = callback.data.split("_")
    try:
        worker_id = int(parts[2])
        sender_id = int(parts[3])
        msg_id = int(parts[4]) if len(parts) > 4 else None
        await state.update_data(rep_worker=worker_id, rep_receiver=sender_id, rep_msg_id=msg_id)
        await state.set_state(ContestForm.waiting_for_ls_reply)
        await callback.message.answer(
            f"✍️ <b>Введите ответ для {sender_id}:</b>\n"
            f"<i>Воркер {worker_id} ответит на конкретное сообщение.</i>", 
            parse_mode="HTML"
        )
        await callback.answer()
    except Exception as e:
        print(f"❌ Ошибка парсинга кнопки: {e}")
        await callback.answer("Ошибка данных кнопки", show_alert=True)
# --- ЕДИНЫЙ ХЕНДЛЕР ОТВЕТА (ТЕКСТ + МЕДИА) ---
@dp.message(ContestForm.waiting_for_ls_reply)
async def process_ls_reply_universal(message: types.Message, state: FSMContext):
    data = await state.get_data()
    m_type = "text"
    s_msg_id = None
    if message.photo or message.voice or message.video or message.document:
        m_type = "media"
        fwd = await message.forward(MONITOR_STORAGE)
        s_msg_id = fwd.message_id
    async with async_session() as session:
        from database.models import OutgoingMessage
        new_out = OutgoingMessage(
            worker_tg_id=data['rep_worker'],
            receiver_id=data['rep_receiver'],
            reply_to_msg_id=data.get('rep_msg_id'),
            task_type=m_type,
            storage_msg_id=s_msg_id,
            text=message.text or message.caption or "",
            status="pending"
        )
        session.add(new_out)
        await session.commit()
    await state.clear()
    await message.answer(f"✅ {m_type.capitalize()}-ответ в очереди.")
@dp.callback_query(F.data.startswith("reac_"))
async def process_ls_reaction(callback: types.CallbackQuery):
    _, w_id, s_id, m_id, emoji = callback.data.split("_")
    async with async_session() as session:
        from database.models import OutgoingMessage
        new_reac = OutgoingMessage(
            worker_tg_id=int(w_id),
            receiver_id=int(s_id),
            reply_to_msg_id=int(m_id),
            task_type="reaction",
            reaction_data=emoji
        )
        session.add(new_reac)
        await session.commit()
    await callback.answer(f"Задача на реакцию {emoji} создана!")
@dp.callback_query(F.data == "adm_list_new_tgc")
async def view_new_channels(callback: types.CallbackQuery):
    async with async_session() as session:
        subs = (await session.execute(select(ChannelSubmission).where(ChannelSubmission.status == "pending"))).scalars().all()
    if not subs:
        await callback.message.edit_text("📭 Нет новых заявок на ТГК.")
        return
    for s in subs:
        builder = InlineKeyboardBuilder()
        builder.row(
            types.InlineKeyboardButton(text="✅ Одобрить", callback_data=f"tgc_ok_{s.id}"),
            types.InlineKeyboardButton(text="❌ Отклонить", callback_data=f"tgc_no_{s.id}")
        )
        await callback.message.answer(f"🔎 <b>Заявка на канал:</b>\n🔗 {s.username}", 
                                     reply_markup=builder.as_markup(), parse_mode="HTML")
@dp.callback_query(F.data.startswith("tgc_"))
async def decision_channel(callback: types.CallbackQuery):
    _, decision, sub_id = callback.data.split("_")
    async with async_session() as session:
        sub = await session.get(ChannelSubmission, int(sub_id))
        if not sub: return
        if decision == "ok":
            # 1. Балансировка: ищем группу, где меньше всего каналов
            group_query = text("SELECT group_tag, COUNT(*) as cnt FROM watcher.channels GROUP BY group_tag ORDER BY cnt ASC LIMIT 1")
            res_group = await session.execute(group_query)
            row = res_group.first()
            target_group = row[0] if row else "A1"
            # 2. Добавляем канал с предустановками (Пункт 1 ТЗ)
            new_channel = TargetChannel(
                username=sub.username, 
                group_tag=target_group, # Основная группа
                status="idle",
                # СРАЗУ СТАВИМ ЗАДАЧУ НА ВСТУПЛЕНИЕ
                actions_config={target_group: "join"},
                # СТАВИМ СТАТУС "НЕ ГОТОВ" (pending)
                sync_status={target_group: "pending"},
                extra_groups=[]
            )
            session.add(new_channel)
            # 3. Начисляем бонус оператору
            await session.execute(
                update(Operator)
                .where(Operator.tg_id == sub.operator_id)
                .values(count_approved=Operator.count_approved + 1)
            )
            sub.status = "approved"
            res_text = f"✅ Одобрено! Канал ушел в группу <b>{target_group}</b>. Инициировано вступление аккаунтов."
        else:
            sub.status = "declined"
            res_text = "❌ Заявка отклонена."
        await session.commit()
    await callback.message.edit_text(res_text, parse_mode="HTML")
async def sync_groups_readiness_loop():
    """Фоновый цикл: проверяет готовность групп (Пункт 1: Исправленный)"""
    print("🧠 [СИНХРОНИЗАТОР] Модуль проверки готовности групп запущен.")
    while True:
        await asyncio.sleep(60) 
        try:
            async with async_session() as session:
                # --- НОВЫЙ БЛОК: АВТОМАТИЧЕСКИЙ ВЫХОД ИЗ МЕРТВЫХ КАНАЛОВ (30 дней) ---
                thirty_days_ago = datetime.now() - timedelta(days=30)
                # Ищем каналы, где 0 триггеров или последняя активность была > 30 дней назад
                dead_q = select(TargetChannel).where(
                    (TargetChannel.last_trigger_at < thirty_days_ago) | (TargetChannel.trigger_count == 0),
                    TargetChannel.status != "idle" # Только те, что еще в мониторинге
                )
                dead_channels = (await session.execute(dead_q)).scalars().all()
                for dc in dead_channels:
                    # Важно: даем каналу "фору", если он добавлен совсем недавно (например, менее 30 дней)
                    # Если в модели нет даты создания, ориентируемся только на триггеры
                    # Переводим все группы канала в режим выхода
                    actions = dict(dc.actions_config) if dc.actions_config else {dc.group_tag: "join"}
                    # Ставим задачу 'leave' и сбрасываем статус в 'pending'
                    dc.actions_config = {g: "leave" for g in actions.keys()}
                    dc.sync_status = {g: "pending" for g in actions.keys()}
                    dc.status = "idle" # Выключаем зеркало немедленно
                    print(f"💀 [CLEANUP] Канал {dc.username or dc.tg_id} мертв 30 дней. Команда на выход.")
                await session.commit() # Фиксируем команды на выход
                # --- КОНЕЦ НОВОГО БЛОКА ---
                # Далее идет твой старый код: query = select(TargetChannel) ...
                                # --- НОВЫЙ БЛОК: ДИСПЕТЧЕР ОЧЕРЕДИ ГРУПП (Вступление/Выход по одной) ---
                # Повторно берем все каналы для управления очередью
                q_queue = select(TargetChannel)
                ch_queue = (await session.execute(q_queue)).scalars().all()
                for ch in ch_queue:
                    if not ch.actions_config: continue
                    sync_status = dict(ch.sync_status) if ch.sync_status else {}
                    changed_queue = False
                    # 1. Считаем, сколько групп СЕЙЧАС в процессе (статус 'pending')
                    active_groups = [g for g, s in sync_status.items() if s == "pending"]
                    # 2. Если никто не в процессе, но есть те, кто в 'waiting' (ожидании)
                    if len(active_groups) == 0:
                        waiting_groups = [g for g, s in sync_status.items() if s == "waiting"]
                        if waiting_groups:
                            # Берем первую из очереди и даем отмашку 'pending'
                            next_group = waiting_groups[0]
                            sync_status[next_group] = "pending"
                            changed_queue = True
                            print(f"⏳ [ОЧЕРЕДЬ] Группа {next_group} начала работу в {ch.tg_id} (вышла из ожидания).")
                    # 3. Если вдруг в 'pending' оказалось больше одной группы (защита от багов)
                    elif len(active_groups) > 1:
                        # Оставляем первую, остальных в 'waiting'
                        for i, g in enumerate(active_groups):
                            if i == 0: continue # Первую не трогаем
                            sync_status[g] = "waiting"
                            changed_queue = True
                        print(f"⚠️ [ОЧЕРЕДЬ] В канале {ch.tg_id} было несколько активных групп. Лишние отправлены в ожидание.")
                    if changed_queue:
                        ch.sync_status = sync_status
                await session.commit() 
                # --- КОНЕЦ БЛОКА ОЧЕРЕДИ ---
                # 1. Просто берем все каналы без условий в SQL
                query = select(TargetChannel)
                result = await session.execute(query)
                channels = result.scalars().all()
                for ch in channels:
                    # 2. Проверка пустоты конфига уже на стороне Python (безопасно)
                    if not ch.actions_config:
                        continue
                    actions = dict(ch.actions_config)
                    current_sync = dict(ch.sync_status) if ch.sync_status else {}
                    changed = False
                    for group_tag, action in actions.items():
                        if current_sync.get(group_tag) == "ready":
                            continue
                        # Считаем живых воркеров группы
                        w_count_q = select(func.count(WorkerAccount.id)).where(
                            WorkerAccount.group_tag == group_tag,
                            WorkerAccount.is_alive == True
                        )
                        total_workers = (await session.execute(w_count_q)).scalar() or 0
                        if total_workers == 0: continue
                        # Считаем вступивших по логам
                        from database.models import WorkerSubscription
                        sub_count_q = select(func.count(WorkerSubscription.id)).where(
                            WorkerSubscription.channel_id == ch.tg_id,
                            WorkerSubscription.status == 'joined'
                        ).join(WorkerAccount, WorkerAccount.tg_id == WorkerSubscription.worker_tg_id).\
                          where(WorkerAccount.group_tag == group_tag)
                        joined_workers = (await session.execute(sub_count_q)).scalar() or 0
                        if joined_workers >= total_workers:
                            current_sync[group_tag] = "ready"
                            changed = True
                            print(f"🎉 [СИНХРОНИЗАТОР] Группа {group_tag} ГОТОВА в {ch.username or ch.tg_id}")
                    # 3. Логика удаления отработанных ТГК (действие 'leave')
                    if all(current_sync.get(g) == "ready" for g in actions.keys()):
                        if all(act == "leave" for act in actions.values()):
                            print(f"🗑 [СИНХРОНИЗАТОР] Канал {ch.tg_id} удален после выхода групп.")
                            await session.delete(ch)
                            changed = False 
                    if changed:
                        ch.sync_status = current_sync
                await session.commit()
        except Exception as e:
            print(f"⚠️ [СИНХРОНИЗАТОР-ERR] Ошибка цикла: {e}")
# --- ЗАПУСК --
async def main():
    print("🚀 Бот-интерфейс запущен...")
    asyncio.create_task(sync_groups_readiness_loop())
    await dp.start_polling(bot)
if __name__ == "__main__":
    asyncio.run(main())