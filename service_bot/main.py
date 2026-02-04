import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from decouple import config
from sqlalchemy import select, update
from datetime import datetime

# Импорты из твоего проекта
from database.config import async_session
from database.models import Operator, PotentialPost
from service_bot.states import ContestForm

# Настройки
BOT_TOKEN = config('BOT_TOKEN')
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

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

async def get_next_post(group_tag: str):
    """Поиск следующего свободного поста"""
    async with async_session() as session:
        query = select(PotentialPost).where(
            PotentialPost.group_tag == group_tag,
            PotentialPost.is_claimed == False
        ).order_by(PotentialPost.id.asc()).limit(1)
        
        result = await session.execute(query)
        post = result.scalars().first()
        
        if post:
            post.is_claimed = True
            post.claimed_at = datetime.now()
            await session.commit()
            return post
        return None

# --- ОБРАБОТЧИКИ КОМАНД ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    op = await get_operator(message.from_user.id)
    
    if not op:
        await message.answer("❌ Доступ запрещен. Вас нет в списке операторов.")
        return

    kb = [
        [types.KeyboardButton(text="📥 Получить новый пост")],
        [types.KeyboardButton(text="📊 Статистика группы")]
    ]
    keyboard = types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    
    await message.answer(
        f"👋 Привет, оператор группы {op.group_tag}!\n"
        "Используйте кнопки ниже для работы.",
        reply_markup=keyboard
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

@dp.callback_query(F.data.startswith("setup_"))
async def start_setup(callback: types.CallbackQuery, state: FSMContext):
    post_id = callback.data.split("_")[1]
    
    await state.update_data(current_post_id=post_id)
    await state.set_state(ContestForm.choosing_type)
    
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🕹 АФК участие", callback_data="type_afk"))
    builder.row(types.InlineKeyboardButton(text="🗳 Голосование", callback_data="type_vote"))
    builder.row(types.InlineKeyboardButton(text="🎰 Лудка", callback_data="type_ludka"))
    
    await callback.message.answer(
        "📝 <b>Шаг 1: Тип конкурса</b>\nВыберите механику участия:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(ContestForm.choosing_type)
async def process_type(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(contest_type=callback.data.replace("type_", ""))
    await state.set_state(ContestForm.choosing_prize)
    
    builder = InlineKeyboardBuilder()
    prizes = ["Деньги 💵", "Звезды ⭐", "NFT 🖼", "TG Premium 💎", "Ценности 🎮", "Другое 🎁"]
    for p in prizes:
        builder.add(types.InlineKeyboardButton(text=p, callback_data=f"prize_{p}"))
    builder.adjust(2)
    
    await callback.message.edit_text(
        "📝 <b>Шаг 2: Приз</b>\nЧто разыгрывается?", 
        reply_markup=builder.as_markup(), 
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(ContestForm.choosing_prize)
async def process_prize(callback: types.CallbackQuery, state: FSMContext):
    prize = callback.data.replace("prize_", "")
    await state.update_data(prize=prize, selected_conds=[])
    await state.set_state(ContestForm.filling_conditions)
    
    await callback.message.edit_text(
        "📝 <b>Шаг 3: Условия участия</b>\nВыберите необходимые действия:",
        reply_markup=get_conditions_kb([]),
        parse_mode="HTML"
    )
    await callback.answer()
@dp.callback_query(ContestForm.filling_conditions)
async def process_conditions(callback: types.CallbackQuery, state: FSMContext):
    if callback.data == "cond_done":
        await state.set_state(ContestForm.setting_deadline)
        
        # Создаем кнопку для пропуска ввода даты
        builder = InlineKeyboardBuilder()
        builder.add(types.InlineKeyboardButton(text="🗓 Без точной даты", callback_data="deadline_none"))
        
        await callback.message.edit_text(
            "📝 <b>Шаг 4: Дедлайн</b>\nВведите дату завершения в формате:\n<code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n\n"
            "Или нажмите кнопку ниже, если дата неизвестна:", 
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    # Логика галочек
    code = callback.data.replace("cond_", "")
    data = await state.get_data()
    selected = data.get("selected_conds", [])
    if code in selected:
        selected.remove(code)
    else:
        selected.append(code)
    await state.update_data(selected_conds=selected)
    await callback.message.edit_reply_markup(reply_markup=get_conditions_kb(selected))
    await callback.answer()

# Обработка кнопки "Без даты"
@dp.callback_query(F.data == "deadline_none", ContestForm.setting_deadline)
async def process_deadline_none(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(deadline=None)
    await state.set_state(ContestForm.choosing_accounts)
    
    builder = InlineKeyboardBuilder()
    nums = ["5", "10", "20", "50", "Все"]
    for n in nums:
        builder.add(types.InlineKeyboardButton(text=n, callback_data=f"accs_{n}"))
    builder.adjust(3)

    await callback.message.edit_text(
        "✅ Дата: <b>Не установлена</b>\n\n"
        "📝 <b>Шаг 5: Охват</b>\nСколько аккаунтов должно участвовать?",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()

# Обработка текстового ввода даты
@dp.message(ContestForm.setting_deadline)
async def process_deadline(message: types.Message, state: FSMContext):
    try:
        deadline_dt = datetime.strptime(message.text, "%d.%m.%Y %H:%M")
        if deadline_dt < datetime.now():
            await message.answer("❌ Дата не может быть в прошлом! Попробуйте еще раз:")
            return

        await state.update_data(deadline=deadline_dt)
        await state.set_state(ContestForm.choosing_accounts)
        
        builder = InlineKeyboardBuilder()
        nums = ["5", "10", "20", "50", "Все"]
        for n in nums:
            builder.add(types.InlineKeyboardButton(text=n, callback_data=f"accs_{n}"))
        builder.adjust(3)

        await message.answer(
            f"✅ Дата принята: {deadline_dt.strftime('%d.%m.%Y %H:%M')}\n\n"
            "📝 <b>Шаг 5: Охват</b>\nСколько аккаунтов должно участвовать?",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
    except ValueError:
        await message.answer(
            "⚠️ Неверный формат! Напишите дату строго по шаблону:\n"
            "<code>ДД.ММ.ГГГГ ЧЧ:ММ</code>",
            parse_mode="HTML"
        )

@dp.callback_query(ContestForm.choosing_accounts)
async def process_accounts(callback: types.CallbackQuery, state: FSMContext):
    count = callback.data.replace("accs_", "")
    await state.update_data(account_count=count)
    
    # Собираем все данные из памяти для итогового вывода
    data = await state.get_data()
    
    # Формируем красивое резюме
    deadline_str = data['deadline'].strftime('%d.%m.%Y %H:%M') if data['deadline'] else "Не установлена"
    conds_str = ", ".join(data['selected_conds']) if data['selected_conds'] else "Без условий"
    
    summary = (
        "🏁 <b>Проверка паспорта конкурса</b>\n\n"
        f"🔹 Тип: <code>{data['contest_type']}</code>\n"
        f"🔹 Приз: <code>{data['prize']}</code>\n"
        f"🔹 Условия: <code>{conds_str}</code>\n"
        f"🔹 Дедлайн: <code>{deadline_str}</code>\n"
        f"🔹 Охват: <code>{data['account_count']} аккаунтов</code>\n\n"
        "Все верно? После подтверждения паспорт будет сохранен в БД."
    )
    
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="✅ Подтвердить и запустить", callback_data="passport_confirm"))
    builder.row(types.InlineKeyboardButton(text="🔄 Сбросить", callback_data="passport_cancel"))
    
    await state.set_state(ContestForm.confirming)
    await callback.message.edit_text(summary, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()

from database.models import ContestPassport

@dp.callback_query(ContestForm.confirming, F.data == "passport_confirm")
async def save_passport(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    op = await get_operator(callback.from_user.id)
    
    async with async_session() as session:
        # Создаем запись паспорта
        new_passport = ContestPassport(
            post_id=int(data['current_post_id']),
            group_tag=op.group_tag,
            type=data['contest_type'],
            prize_type=data['prize'],
            conditions=data['selected_conds'], # JSON формат
            deadline=data['deadline'],
            max_accounts=0 if data['account_count'] == "Все" else int(data['account_count']),
            status="active"
        )
        session.add(new_passport)
        await session.commit()
    
    await state.clear() # Очищаем состояние
    await callback.message.edit_text("🚀 <b>Паспорт успешно создан!</b>\nДанные переданы в систему исполнения.", parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "passport_cancel")
async def cancel_passport(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Оформление отменено. Данные удалены.")
    await callback.answer()

@dp.callback_query(F.data.startswith("trash_"))
async def process_trash(callback: types.CallbackQuery):
    post_id = int(callback.data.split("_")[1])
    
    async with async_session() as session:
        # Помечаем пост как отработанный, но паспорт для него не создаем
        await session.execute(
            update(PotentialPost).where(PotentialPost.id == post_id).values(is_claimed=True)
        )
        await session.commit()
    
    await callback.message.edit_text("🗑 Пост отправлен в мусор и удален из очереди.")
    await callback.answer()

# --- ЗАПУСК ---

async def main():
    print("🚀 Бот-интерфейс запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
