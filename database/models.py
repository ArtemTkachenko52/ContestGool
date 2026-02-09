from sqlalchemy import Column, BigInteger, String, Integer, Boolean, DateTime, ForeignKey, JSON, Text
from sqlalchemy.sql import func
from database.base import Base

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
    # --- ДОБАВЬ ЭТИ ДВЕ СТРОКИ НИЖЕ ---
    os_version = Column(String)
    app_version = Column(String)
    # ----------------------------------
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
    group_tag = Column(String, index=True)
    status = Column(String, default="idle") # 'idle' или 'active_monitor'
    last_read_post_id = Column(Integer, default=0) # ID последнего просмотренного PotentialPost


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
    status = Column(String, default="active") # 'active', 'finished'

class VotingReport(Base):
    __tablename__ = 'voting_reports'
    __table_args__ = {"schema": "management"}
    id = Column(Integer, primary_key=True)
    passport_id = Column(Integer, ForeignKey("management.passports.id"))
    target_msg_id = Column(BigInteger) # Пост, где крутим
    # Новое: ID канала, куда пересылали пост (чтобы воркер знал, где искать сообщение)
    target_chat_id = Column(BigInteger) 
    vote_type = Column(String) # 'poll' (опрос) или 'reaction'
    option_id = Column(String) # ID кнопки или смайлика
    # --- ДОБАВЬ ЭТИ ДВЕ СТРОКИ ---
    target_groups = Column(JSON)      # Список групп ['A1', 'A2']
    accounts_count = Column(Integer)  # Кол-во участников (если группа одна)
    # -----------------------------
    intensity = Column(Integer)
    status = Column(String, default="pending") # 'pending', 'approved', 'declined', 'completed'
    created_by = Column(BigInteger) # Кто создал рапорт (ID оператора)


# --- СХЕМА WORKERS (Исполнители) ---
class WorkerAccount(Base, BaseAccount):
    __tablename__ = 'workers'
    __table_args__ = {"schema": "workers"}
    is_alive = Column(Boolean, default=True)
    last_action = Column(DateTime)

class AccountMessage(Base):
    __tablename__ = 'messages'
    __table_args__ = {"schema": "workers"}
    id = Column(Integer, primary_key=True)
    worker_id = Column(Integer, ForeignKey("workers.workers.id"))
    sender_id = Column(BigInteger) # Кто написал (настоящий юзер)
    text = Column(Text)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())

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