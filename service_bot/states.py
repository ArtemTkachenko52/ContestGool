from aiogram.fsm.state import StatesGroup, State

class ContestForm(StatesGroup):
    choosing_type = State()      # Выбор типа (АФК/Голосование/Лудка)
    choosing_prize = State()     # Выбор приза
    filling_conditions = State() # Указание условий (подписки и т.д.)
    setting_deadline = State()   # Ввод даты завершения
    choosing_accounts = State()  # Выбор количества исполнителей
    confirming = State()         # Финальное подтверждение
