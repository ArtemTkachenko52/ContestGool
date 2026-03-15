from sqlalchemy import Column, BigInteger, String, Integer, Boolean, DateTime, ForeignKey, JSON, Text
from sqlalchemy.sql import func
from database.base import Base
from sqlalchemy.dialects.postgresql import JSONB
# Общий класс для всех типов аккаунтов (Читатели и Исполнители)
class BaseAccount:
    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, unique=True)
    phone = Column(String, unique=True, nullable=False)
    api_id = Column(Integer)
    api_hash = Column(String)
    session_string = Column(String)
    group_tag = Column(String, index=True)
    proxy = Column(String) 
    device_model = Column(String)
    os_version = Column(String)
    app_version = Column(String)
    system_lang = Column(String, default="ru-RU")
# --- СХЕМА WATCHER (Мониторинг) ---
class Keyword(Base):
    __tablename__ = "keywords"
    __table_args__ = {"schema": "watcher"}
    id = Column(Integer, primary_key=True)
    word = Column(String(100), unique=True)
    category = Column(String, default="general") # 'general' или 'fast'
class TargetChannel(Base):
    __tablename__ = 'channels'
    __table_args__ = {"schema": "watcher"}
    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, unique=True)
    username = Column(String)
    # КТО УПРАВЛЯЕТ (Пункт 1, часть 1)
    group_tag = Column(String, index=True) # Основная группа (А1)
    extra_groups = Column(JSONB, server_default='[]')    # Доп. группы ["А2", "В1"]
    # КОНФИГУРАЦИЯ (Пункт 1: действие и статус)
    # Пример: {"A1": "join", "A2": "join", "B1": "leave"}
    actions_config = Column(JSONB, server_default='{}') 
    # Пример: {"A1": "ready", "A2": "pending"}
    sync_status = Column(JSONB, server_default='{}') 
    status = Column(String, default="idle") # 'idle' или 'active_monitor'
    last_read_post_id = Column(Integer, default=0)
    # ID ЧАТА КОММЕНТАРИЕВ (Пункт 2, часть 1)
    comment_chat_id = Column(BigInteger, nullable=True)
    participating_groups = Column(JSONB, server_default='[]')
    trigger_count = Column(Integer, default=0) # Сколько раз сработал за всё время
    last_trigger_at = Column(DateTime, server_default=func.now(), onupdate=func.now()) # Дата последней активности
class ExtraChat(Base):
    """Таблица для временных чатов из условий подписки (Пункт 2 ТЗ)"""
    __tablename__ = 'extra_chats'
    __table_args__ = {"schema": "watcher"}
    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, unique=True)
    username = Column(String)
    parent_channel_id = Column(BigInteger) # К какому ТГК привязан (например, №132)
    created_at = Column(DateTime, server_default=func.now())
class WorkerSubscription(Base):
    """Таблица логов вступлений (Пункт 1: Хранилище данных)"""
    __tablename__ = 'subscriptions'
    __table_args__ = {"schema": "workers"}
    id = Column(Integer, primary_key=True)
    worker_tg_id = Column(BigInteger, ForeignKey("workers.workers.tg_id"))
    channel_id = Column(BigInteger, ForeignKey("watcher.channels.tg_id"))
    # Статус: 'joined' (состоит), 'left' (не состоит), 'in_progress' (в процессе)
    status = Column(String, default="left")
    # Для лимитов (20 тгк в день)
    last_action_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
class ReaderAccount(Base, BaseAccount):
    __tablename__ = 'readers'
    __table_args__ = {"schema": "watcher"}
class PotentialPost(Base):
    __tablename__ = 'potential_posts'
    __table_args__ = {"schema": "watcher"}
    id = Column(Integer, primary_key=True)
    group_tag = Column(String, index=True)
    storage_msg_id = Column(BigInteger)
    source_tg_id = Column(BigInteger)
    source_msg_id = Column(BigInteger)
    keyword_hit = Column(String)
    post_type = Column(String) # 'keyword', 'fast', 'button'
    is_claimed = Column(Boolean, default=False)
    published_at = Column(DateTime)
    claimed_at = Column(DateTime, nullable=True)
# --- СХЕМА MANAGEMENT (Управление) ---
class Operator(Base):
    __tablename__ = 'operators'
    __table_args__ = {"schema": "management"}
    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, unique=True)
    group_tag = Column(String)
    rank = Column(Integer, default=1) # 1 - оператор, 2 - старший
    count_approved = Column(Integer, default=0)
class ContestPassport(Base):
    __tablename__ = 'passports'
    __table_args__ = {"schema": "management"}
    id = Column(Integer, primary_key=True)
    post_id = Column(Integer, ForeignKey("watcher.potential_posts.id"))
    group_tag = Column(String, index=True)
    type = Column(String) # 'afk', 'vote'
    prize_type = Column(String)
    conditions = Column(JSON) # Здесь лежат sub_links, repost_count, vote_details
    intensity_level = Column(Integer, default=1) # 1-4
    # Список групп, участвующих в этом паспорте (для единой интенсивности)
    participating_groups = Column(JSONB, server_default='[]') # ["A1", "A2"]
    status = Column(String, default="active") # 'active', 'finished'
class VotingReport(Base):
    __tablename__ = 'voting_reports'
    __table_args__ = {"schema": "management"}
    id = Column(Integer, primary_key=True)
    passport_id = Column(Integer, ForeignKey("management.passports.id"))
    target_msg_id = Column(BigInteger) 
    target_chat_id = Column(BigInteger) 
    vote_type = Column(String) # 'poll' или 'reaction'
    option_id = Column(String) 
    target_groups = Column(JSON)      # Сюда запишем ['A1', 'B2']
    accounts_count = Column(Integer)  # Сюда кол-во (если группа одна)
    intensity = Column(Integer)
    status = Column(String, default="pending") 
    created_by = Column(BigInteger)
class ChannelSubmission(Base):
    __tablename__ = 'channel_submissions'
    __table_args__ = {"schema": "management"}
    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, unique=True)
    username = Column(String) # Ссылка или юзернейм
    operator_id = Column(BigInteger) # Кто предложил
    status = Column(String, default="pending") # pending, approved, declined
# --- СХЕМА WORKERS (Исполнители) ---
class WorkerAccount(Base, BaseAccount):
    __tablename__ = 'workers'
    __table_args__ = {"schema": "workers"}
    is_alive = Column(Boolean, default=True)
    last_action = Column(DateTime)
    last_sync_subscriptions = Column(DateTime, nullable=True)
    stars_balance = Column(Integer, default=0)
    is_financial_ready = Column(Boolean, default=False)
    last_check_window_end = Column(DateTime, nullable=True)
    last_inventory_check_window_end = Column(DateTime, nullable=True)
class AccountMessage(Base):
    __tablename__ = 'messages'
    __table_args__ = {"schema": "workers"}
    id = Column(Integer, primary_key=True)
    msg_id = Column(Integer)          # ID сообщения в самом Telegram (для Reply)
    worker_tg_id = Column(BigInteger) 
    sender_id = Column(BigInteger)    
    text = Column(Text)
    media_type = Column(String, default="text") # text, photo, voice, video, document
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())
    storage_media_id = Column(BigInteger, nullable=True) # ID сообщения в группе-хранилище
class AuditLog(Base):
    __tablename__ = 'audit_logs'
    __table_args__ = {"schema": "management"}
    id = Column(Integer, primary_key=True)
    group_tag = Column(String, index=True)
    action = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
class StarReport(Base):
    __tablename__ = 'star_reports'
    __table_args__ = {"schema": "management"}
    id = Column(Integer, primary_key=True)
    passport_id = Column(Integer, ForeignKey("management.passports.id"))
    target_user = Column(String)
    method = Column(String)
    star_count = Column(Integer)
    executor_id = Column(BigInteger) # Лид-аккаунт
    reason = Column(Text)              # Текст причины
    proof_media_id = Column(BigInteger) # ID скриншота в MONITOR_STORAGE
    status = Column(String, default="pending")
    created_at = Column(DateTime, server_default=func.now())
class GroupChannelRelation(Base):
    __tablename__ = 'group_channel_relations'
    __table_args__ = {"schema": "management"}
    id = Column(Integer, primary_key=True)
    group_tag = Column(String)
    channel_id = Column(BigInteger)
    # Статусы: 'not_joined', 'inviting', 'joined'
    status = Column(String, default='not_joined') 
    invite_started_at = Column(DateTime, nullable=True)
class ReserveChannel(Base):
    """Таблица для потенциальных каналов (Пункт 5)"""
    __tablename__ = 'reserve'
    __table_args__ = {"schema": "watcher"}
    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, unique=True)
    username = Column(String)
    source_group_tag = Column(String) # Кто нашел
    reason = Column(String) # Ключевое слово или 'button'
    created_at = Column(DateTime, server_default=func.now())
class LuckEvent(Base):
    """Логирование триггеров удачи для тестов (Пункт 2)"""
    __tablename__ = 'luck_events'
    __table_args__ = {"schema": "watcher"}
    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger)
    post_id = Column(Integer)
    emoji = Column(String)
    status = Column(String, default="detected") # detected / working / finished
class MentionTask(Base):
    """Очередь задач на авто-комментарий при упоминании (Пункт 1)"""
    __tablename__ = 'mention_tasks'
    __table_args__ = {"schema": "workers"}
    id = Column(Integer, primary_key=True)
    worker_tg_id = Column(BigInteger)
    channel_id = Column(BigInteger)
    post_id = Column(Integer)
    status = Column(String, default="pending")
    created_at = Column(DateTime, server_default=func.now())
class OutgoingMessage(Base):
    __tablename__ = 'outgoing_messages'
    __table_args__ = {"schema": "workers"}
    id = Column(Integer, primary_key=True)
    worker_tg_id = Column(BigInteger)
    receiver_id = Column(String) # Теперь принимает и ID (как строку), и @username
    reply_to_msg_id = Column(Integer, nullable=True)
    text = Column(Text, nullable=True)
    task_type = Column(String, default="text") # text, reaction, media
    file_id = Column(String, nullable=True)     # Для фото/ГС от оператора
    reaction_data = Column(String, nullable=True) # "👍" или ID кастомного
    status = Column(String, default="pending")
    created_at = Column(DateTime, server_default=func.now())
    storage_msg_id = Column(BigInteger, nullable=True) # ID сообщения в хранилище
class LuckRaid(Base):
    """Активные рейды десанта (Пункт 2 ТЗ)"""
    __tablename__ = 'luck_raids'
    __table_args__ = {"schema": "workers"}
    id = Column(Integer, primary_key=True)
    channel_id = Column(BigInteger)
    post_id = Column(Integer)
    emoji = Column(String)
    status = Column(String, default="active") # active / finished
    created_at = Column(DateTime, server_default=func.now())
class DailyLimitCounter(Base):
    """Контроль лимитов (Пункт 1: 30 акков на ТГК, 20 ТГК на акк)"""
    __tablename__ = 'daily_limits'
    __table_args__ = {"schema": "management"}
    id = Column(Integer, primary_key=True)
    target_date = Column(DateTime, default=func.current_date())
    entity_type = Column(String) # 'worker' или 'channel'
    entity_id = Column(BigInteger) # tg_id воркера или канала
    current_count = Column(Integer, default=0)
class FastTask(Base):
    """Таблица для мгновенных ответов 'Кто первый' (Пункт 9)"""
    __tablename__ = 'fast_tasks'
    __table_args__ = {"schema": "workers"}
    id = Column(Integer, primary_key=True)
    channel_id = Column(BigInteger)
    post_id = Column(Integer)
    group_tag = Column(String)
    status = Column(String, default="pending") # 'pending', 'completed'
    created_at = Column(DateTime, server_default=func.now())
class WinHunt(Base):
    """Модель для отслеживания контакта админа после победы"""
    __tablename__ = 'win_hunts'
    __table_args__ = {"schema": "workers"}
    id = Column(Integer, primary_key=True)
    worker_tg_id = Column(BigInteger) # Кто победил
    channel_id = Column(BigInteger)   # В каком канале
    counter = Column(Integer, default=0) # Сколько постов пропустили (0-3)
    status = Column(String, default="active") # active / finished
class WorkerContact(Base):
    """Таблица установленных связей между воркерами для прогрева"""
    __tablename__ = 'worker_contacts'
    __table_args__ = {"schema": "workers"}
    id = Column(Integer, primary_key=True)
    worker_a = Column(BigInteger, index=True) # Кто инициировал
    worker_b = Column(BigInteger, index=True) # С кем чат
    last_chat_at = Column(DateTime, server_default=func.now())