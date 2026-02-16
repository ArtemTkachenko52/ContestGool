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
    
    # --- ПАСПОРТ ГОЛОСОВАНИЕ (ОБНОВЛЕННОЕ) ---
    vote_choose_executor = State() # Выбор исполнителя из кнопок
    input_vote_reg_data = State()   # Ник, текст или медиа
    vote_choose_place = State()    # Выбор: ЛС или Комменты
    input_vote_org_username = State() # Если выбрали ЛС - ввод юзернейма

    
    # --- ОБЩИЕ ПАРАМЕТРЫ ---
    setting_intensity = State()
    confirming = State()
    editing_field = State()

    # --- ИНВАЙТИНГ ---
    choosing_group_to_invite = State()
    
    # --- РАПОРТ НА ЗВЕЗДЫ ---
    star_target = State()      # Кому (username)
    star_gift_type = State()   # Выбор: Медведь, Роза и т.д.
    star_amount = State()      # Сколько звезд
    star_confirm = State()     # Финальное подтверждение


    # --- РАПОРТ НА ГОЛОСОВАНИЕ (НАКРУТКА) ---
    v_rep_fwd = State()
    v_rep_method = State()
    v_rep_option = State()
    v_rep_choose_groups = State()
    v_rep_count = State()
    v_rep_intensity = State()
    v_rep_confirm = State()

    sharing_to_groups = State() # Выбор групп для пересылки конкурса
    waiting_for_ls_reply = State()