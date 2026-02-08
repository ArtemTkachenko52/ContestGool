from aiogram.fsm.state import StatesGroup, State

class ContestForm(StatesGroup):
    choosing_type = State()         # Выбор: АФК / Голосование
    choosing_prize = State()        # Выбор приза (Деньги, Звезды и т.д.)
    input_prize_custom = State()    # Ввод своего приза (если выбрано "Другое")
    
    # Блок для типа "АФК"
    filling_conditions = State()    # Выбор чекбоксов (Подписка, Реакция...)
    input_sub_links = State()       # Ввод ссылок (если выбрана подписка)
    input_repost_count = State()    # Ввод кол-ва репостов (если выбран репост)
    
    # Блок для типа "Голосование"
    input_vote_executor = State()   # Аккаунт-участник
    input_vote_data = State()       # Данные для регистрации (ник/фото)
    input_vote_place = State()      # Место регистрации (ЛС/комменты)
    
    setting_intensity = State()     # Уровень интенсивности (1-4)
    confirming = State()            # Финальное подтверждение
    waiting_for_reaction = State() 
    viewing_current_contests = State() # Просмотр списка ТГК
    viewing_specific_contest = State() # Просмотр конкретного конкурса и постов
