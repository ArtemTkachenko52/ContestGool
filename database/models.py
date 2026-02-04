from sqlalchemy import Column, Integer, String, BigInteger, Boolean, DateTime, ForeignKey, JSON
from sqlalchemy.sql import func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from database.base import Base

class Base(DeclarativeBase):
    pass

# Твоя старая таблица (оставляем!)
class Keyword(Base):
    __tablename__ = "keywords"
    __table_args__ = {"schema": "watcher"}
    id: Mapped[int] = mapped_column(primary_key=True)
    word: Mapped[str] = mapped_column(String(100), unique=True)
    is_active: Mapped[bool] = mapped_column(default=True)

# --- НОВЫЕ ТАБЛИЦЫ ---

# 1. Каналы (Схема watcher)
class TargetChannel(Base):
    __tablename__ = 'channels'
    __table_args__ = {"schema": "watcher"}
    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String)
    group_tag = Column(String, index=True) # "A1", "A2"
    last_msg_id = Column(Integer, default=0)
    fail_count = Column(Integer, default=0)
    status = Column(String, default="idle")

# 2. Операторы (Схема management)
class Operator(Base):
    __tablename__ = 'operators'
    __table_args__ = {"schema": "management"}
    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, unique=True, nullable=False)
    group_tag = Column(String, index=True)
    last_activity = Column(DateTime, onupdate=func.now())

# 3. Аккаунты Читатели (Схема watcher)
class ReaderAccount(Base):
    __tablename__ = 'readers'
    __table_args__ = {"schema": "watcher"}
    id = Column(Integer, primary_key=True)
    phone = Column(String, unique=True, nullable=False)
    password_2fa = Column(String)
    email = Column(String)
    api_id = Column(Integer)
    api_hash = Column(String)
    proxy = Column(String)
    device_model = Column(String)
    os_version = Column(String)
    app_version = Column(String)
    system_lang = Column(String, default="ru-RU")
    group_tag = Column(String)
    session_string = Column(String)

# 4. Аккаунты Исполнители (Схема workers)
class WorkerAccount(Base):
    __tablename__ = 'workers'
    __table_args__ = {"schema": "workers"}
    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, unique=True) # Юзер айди самого аккаунта
    phone = Column(String, unique=True, nullable=False)
    password_2fa = Column(String)
    email = Column(String)
    api_id = Column(Integer)
    api_hash = Column(String)
    proxy = Column(String)
    device_model = Column(String)
    os_version = Column(String)
    app_version = Column(String)
    system_lang = Column(String, default="ru-RU")
    group_tag = Column(String)
    session_string = Column(String)

class PotentialPost(Base):
    __tablename__ = 'potential_posts'
    __table_args__ = {"schema": "watcher"}

    id = Column(Integer, primary_key=True)
    group_tag = Column(String, index=True)      # Группа (A1, A2...)
    
    source_tg_id = Column(BigInteger)           # ID канала
    source_msg_id = Column(BigInteger)          # ID поста в канале
    storage_msg_id = Column(BigInteger)         # ID в твоем хранилище
    
    keyword_hit = Column(String)                # Какое слово сработало
    
    is_claimed = Column(Boolean, default=False) # Получен ли оператором
    published_at = Column(DateTime)             # Дата поста в ТГ
    claimed_at = Column(DateTime)               # Когда оператор нажал кнопку в боте
class DiscoveryChannel(Base):
    __tablename__ = 'discovery_channels'
    __table_args__ = {"schema": "watcher"}

    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, unique=True)
    username = Column(String)
    found_from_group = Column(String)  # Кто нашел (напр. 'A1')
    reason = Column(String)            # 'forward' или 'subscription_condition'
    created_at = Column(DateTime, server_default=func.now())
    is_checked = Column(Boolean, default=False) # Проверил ли оператор

class ContestPassport(Base):
    __tablename__ = 'passports'
    __table_args__ = {"schema": "management"}

    id = Column(Integer, primary_key=True)
    post_id = Column(Integer) # ID из таблицы potential_posts
    group_tag = Column(String, index=True)
    
    type = Column(String)           # 'afk', 'vote', 'ludka'
    prize_type = Column(String)     # 'Деньги', 'NFT' и т.д.
    conditions = Column(JSON)        # Список условий ["sub", "reac"]
    
    deadline = Column(DateTime, nullable=True) # Может быть None
    max_accounts = Column(Integer, default=0)  # 0 значит "Все"
    
    status = Column(String, default="active") # active / finished
    created_at = Column(DateTime, server_default=func.now())