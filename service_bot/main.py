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
from database.models import Operator, PotentialPost, ContestPassport, VotingReport
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
    """Поиск следующего свободного поста (БЕЗ пометки о получении)"""
    async with async_session() as session:
        query = select(PotentialPost).where(
            PotentialPost.group_tag == group_tag,
            PotentialPost.is_claimed == False
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
@dp.callback_query(ContestForm.confirming, F.data == "passport_confirm")
async def save_passport(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    op = await get_operator(callback.from_user.id)
    
    async with async_session() as session:
        # 1. Помечаем пост как отработанный
        await session.execute(
            update(PotentialPost)
            .where(PotentialPost.id == int(data['current_post_id']))
            .values(is_claimed=True, claimed_at=datetime.now())
        )
        
        # 2. Собираем условия (ссылки, репосты и т.д.) в один JSON
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

        # 3. Создаем запись паспорта
        new_passport = ContestPassport(
            post_id=int(data['current_post_id']),
            group_tag=op.group_tag,
            type=data['contest_type'],
            prize_type=data['prize'],
            conditions=conditions_data, # Теперь тут вся пачка данных
            intensity_level=int(data['intensity']),
            status="active"
        )
        
        session.add(new_passport)
        await session.commit()
    
    await state.clear()
    await callback.message.edit_text("🚀 <b>Паспорт успешно создан!</b>\nДанные записаны в БД.", parse_mode="HTML")
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

# --- ЗАПУСК ---

async def main():
    print("🚀 Бот-интерфейс запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
