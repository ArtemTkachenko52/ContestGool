from aiogram.fsm.state import StatesGroup, State

class ContestForm(StatesGroup):
    # --- МОНИТОРИНГ И РЕАКЦИИ ---
    waiting_for_reaction = State()  # ТА САМАЯ СТРОКА ДЛЯ ОПРЕДЕЛИТЕЛЯ ID

    # --- ПАСПОРТ (ОСНОВНОЙ) ---
    choosing_type = State()
    choosing_prize = State()
    input_prize_custom = State()
    filling_conditions = State()
    input_sub_links = State()
    input_repost_count = State()
    
    # --- ГОЛОСОВАНИЕ (РЕГИСТРАЦИЯ ЛИДА) ---
    input_vote_executor = State()
    input_vote_data = State()
    input_vote_place = State()
    
    # --- ОБЩИЕ ПАРАМЕТРЫ ---
    setting_intensity = State()
    confirming = State()
    editing_field = State()

    # --- ИНВАЙТИНГ ---
    choosing_group_to_invite = State()
    
    # --- РАПОРТ НА ЗВЕЗДЫ ---
    star_target = State()
    star_method = State()
    star_amount = State()
    star_confirm = State()

    # --- РАПОРТ НА ГОЛОСОВАНИЕ (НАКРУТКА) ---
    v_rep_fwd = State()
    v_rep_method = State()
    v_rep_option = State()
    v_rep_choose_groups = State()
    v_rep_count = State()
    v_rep_intensity = State()
    v_rep_confirm = State()

    sharing_to_groups = State() # Выбор групп для пересылки конкурса
