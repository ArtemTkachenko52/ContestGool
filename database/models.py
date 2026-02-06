from sqlalchemy import Column, BigInteger, String, Integer, Boolean, DateTime, ForeignKey, JSON, Text
from sqlalchemy.sql import func
from database.base import Base

class BaseAccount:
    id = Column(Integer, primary_key=True)
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

# --- СХЕМА WATCHER ---
class Keyword(Base):
    __tablename__ = "keywords"
    __table_args__ = {"schema": "watcher"}
    id = Column(Integer, primary_key=True)
    word = Column(String(100), unique=True)
    category = Column(String, default="general")
    is_active = Column(Boolean, default=True)

class TargetChannel(Base):
    __tablename__ = 'channels'
    __table_args__ = {"schema": "watcher"}
    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, unique=True)
    username = Column(String)
    group_tag = Column(String, index=True)
    status = Column(String, default="idle")
    last_msg_id = Column(Integer, default=0)
    fail_count = Column(Integer, default=0)

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
    post_type = Column(String, default="keyword")
    keyword_hit = Column(String)
    is_claimed = Column(Boolean, default=False)
    published_at = Column(DateTime)
    claimed_at = Column(DateTime)

class MonitoringPost(Base):
    __tablename__ = 'monitoring_posts'
    __table_args__ = {"schema": "watcher"}
    id = Column(Integer, primary_key=True)
    channel_id = Column(BigInteger)
    storage_msg_id = Column(BigInteger)
    created_at = Column(DateTime, server_default=func.now())

class DiscoveryChannel(Base):
    __tablename__ = 'discovery_channels'
    __table_args__ = {"schema": "watcher"}
    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, unique=True)
    username = Column(String)
    found_from_group = Column(String)
    reason = Column(String)
    is_checked = Column(Boolean, default=False)

# --- СХЕМА MANAGEMENT ---
class Operator(Base):
    __tablename__ = 'operators'
    __table_args__ = {"schema": "management"}
    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, unique=True)
    group_tag = Column(String)
    rank = Column(Integer, default=1)
    last_activity = Column(DateTime, onupdate=func.now())

class ContestPassport(Base):
    __tablename__ = 'passports'
    __table_args__ = {"schema": "management"}
    id = Column(Integer, primary_key=True)
    post_id = Column(Integer)
    group_tag = Column(String)
    type = Column(String) 
    prize_type = Column(String)     
    prize_details = Column(Text)    # Для варианта "Другое"
    conditions = Column(JSON)       # ['sub', 'comm'...]
    sub_links = Column(JSON)        # НОВОЕ: ссылки на каналы
    repost_data = Column(String)    # НОВОЕ: куда/сколько репостов
    winners_count = Column(Integer) # НОВОЕ: призовые места
    max_accounts = Column(Integer)  # Сколько наших участвует
    deadline = Column(DateTime, nullable=True)
    status = Column(String, default="active")

class VotingTask(Base):
    __tablename__ = 'voting_tasks'
    __table_args__ = {"schema": "management"}
    id = Column(Integer, primary_key=True)
    passport_id = Column(Integer)
    target_msg_id = Column(BigInteger)
    option_id = Column(String)
    intensity = Column(String)
    status = Column(String, default="pending")
    created_by = Column(BigInteger)

# --- СХЕМА WORKERS ---
class WorkerAccount(Base, BaseAccount):
    __tablename__ = 'workers'
    __table_args__ = {"schema": "workers"}
    tg_id = Column(BigInteger, unique=True)
    is_alive = Column(Boolean, default=True)

class IncomingMessage(Base):
    __tablename__ = 'incoming_messages'
    __table_args__ = {"schema": "workers"}
    id = Column(Integer, primary_key=True)
    worker_id = Column(BigInteger)
    sender_id = Column(BigInteger)
    text = Column(Text)
    is_read = Column(Boolean, default=False)
    is_internal = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())

class GroupChannelRelation(Base):
    __tablename__ = 'group_channel_relations'
    __table_args__ = {"schema": "management"}
    id = Column(Integer, primary_key=True)
    group_tag = Column(String)
    channel_id = Column(BigInteger)
    # Статусы: 'joined' (все вступили), 'inviting' (в процессе 24ч), 'not_joined'
    status = Column(String, default='not_joined') 
    invite_started_at = Column(DateTime, nullable=True)