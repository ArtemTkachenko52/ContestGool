-- Active: 1769873943528@@localhost@5432@contest_monitor
-- Active: 1769873943528@@localhost@5432@postgres
INSERT INTO management.operators (tg_id, group_tag, rank) 
VALUES (7738844337, 'A1', 2);
INSERT INTO watcher.readers (
    phone, api_id, api_hash, group_tag, session_string, 
    device_model, os_version, app_version, system_lang
) VALUES (
    '+918088396263', 
    31879162, 
    '6fd9f4de71bdcba733b4feddc11eb3f3', 
    'A1', 
    '1BVtsOLEBu1ivfPiu2GUTHnMjc4m8NmSgjwc0iRcv-rUvfiKHy7GhSAGR0FJFIng0s0xmjvZOBvCzjlRdqusLaxGSu1wjkCpN3qgyAkmWg6GV5VGTaY-tLQ0XqbSkxRCOB01KHrwvD5VFxfMdeeNYgIwcpkkisY_5gWvBsQF7nkul0VPz9y69M1pP4PP7H5SZWqhZ4DrSTFhPrWC5DKNrcFOXptmQdEcWXFt7aQxo_zAmngNEU4fPHXB7AGeTzD3uvLhTc4wX1vA6txE8D8IUdF0mCNgYd6qRIi50b426y4v_3QTXqwIsi9sHxQGKqybUgZx6f0WRFBikfsSS8CNJHM-iLoQHfOI=', 
    'PC 64bit', 
    'Windows 10', 
    '4.11.2',
    'ru-RU'
);
INSERT INTO watcher.channels (tg_id, username, group_tag, status) 
VALUES (-1003743474124, 'contestgo_test', 'A1', 'idle');
INSERT INTO watcher.keywords (word, category, is_active) VALUES 
('конкурс', 'general', true),
('первый', 'fast', true),
('1', 'fast', true),
('розыгрыш', 'general', true);
INSERT INTO workers.workers (tg_id, phone, group_tag, is_alive) 
VALUES (8539434410, '+380933277858', 'A1', true);

-- Полная зачистка всех схем со всеми данными
DROP SCHEMA IF EXISTS watcher CASCADE;
DROP SCHEMA IF EXISTS management CASCADE;
DROP SCHEMA IF EXISTS workers CASCADE;

-- Создание пустых схем обратно
CREATE SCHEMA watcher;
CREATE SCHEMA management;
CREATE SCHEMA workers;

-- Добавляем колонки для Читателей
ALTER TABLE watcher.readers ADD COLUMN IF NOT EXISTS os_version VARCHAR;
ALTER TABLE watcher.readers ADD COLUMN IF NOT EXISTS app_version VARCHAR;

-- Добавляем колонки для Исполнителей
ALTER TABLE workers.workers ADD COLUMN IF NOT EXISTS os_version VARCHAR;
ALTER TABLE workers.workers ADD COLUMN IF NOT EXISTS app_version VARCHAR;

-- Проверка результата
SELECT table_schema, table_name, column_name 
FROM information_schema.columns 
WHERE column_name IN ('os_version', 'app_version');

-- Добавляем колонку для отслеживания прочитанных постов в таблицу каналов
ALTER TABLE watcher.channels ADD COLUMN IF NOT EXISTS last_read_post_id INTEGER DEFAULT 0;
UPDATE watcher.potential_posts SET post_type = 'monitoring' WHERE keyword_hit = 'MONITORING';
ALTER TABLE management.voting_reports ADD COLUMN IF NOT EXISTS target_groups JSON;
ALTER TABLE management.voting_reports ADD COLUMN IF NOT EXISTS accounts_count INTEGER;
ALTER TABLE management.voting_reports ADD COLUMN IF NOT EXISTS target_chat_id BIGINT;
ALTER TABLE management.voting_reports ADD COLUMN IF NOT EXISTS created_by BIGINT;


-- Таблица для рапортов на отправку Звезд
CREATE TABLE IF NOT EXISTS management.star_reports (
    id SERIAL PRIMARY KEY,
    passport_id INTEGER REFERENCES management.passports(id),
    target_user VARCHAR(255),    -- Юз организатора
    method VARCHAR(50),          -- 'gift' или 'paid_msg'
    star_count INTEGER,
    executor_id BIGINT,          -- Тот самый лид-аккаунт из паспорта
    status VARCHAR(50) DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Добавляем колонку в отношения групп и каналов для инвайтинга
ALTER TABLE management.group_channel_relations ADD COLUMN IF NOT EXISTS invite_started_at TIMESTAMP;



-- 1. Создаем таблицу для заявок на новые каналы в схеме management
CREATE TABLE IF NOT EXISTS management.channel_submissions (
    id SERIAL PRIMARY KEY,
    tg_id BIGINT UNIQUE,
    username VARCHAR,
    operator_id BIGINT,
    status VARCHAR DEFAULT 'pending'
);

-- 2. Добавляем колонку счетчика в таблицу операторов, если её там еще нет
-- (DO блок нужен, чтобы не вылетела ошибка, если колонка уже существует)
DO $$ 
BEGIN 
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                   WHERE table_schema='management' 
                   AND table_name='operators' 
                   AND column_name='count_approved') THEN
        ALTER TABLE management.operators ADD COLUMN count_approved INTEGER DEFAULT 0;
    END IF;
END $$;

-- 3. На всякий случай проверяем существование таблицы TargetChannel в watcher
-- Если ты ее уже создавал через init_db, этот шаг просто подтвердит структуру
CREATE TABLE IF NOT EXISTS watcher.channels (
    id SERIAL PRIMARY KEY,
    tg_id BIGINT UNIQUE,
    username VARCHAR,
    group_tag VARCHAR,
    status VARCHAR DEFAULT 'idle',
    last_read_post_id INTEGER DEFAULT 0
);

ALTER TABLE workers.workers ADD COLUMN is_configured BOOLEAN DEFAULT FALSE;
ALTER TABLE workers.workers ADD COLUMN used_asset_id VARCHAR UNIQUE; -- Чтобы не повторять авы/ники
