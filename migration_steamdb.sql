-- ============================================
-- Steam Bot v3.0 - Complete Database Schema
-- PostgreSQL для Supabase
-- ============================================

-- Отслеживаемые игры пользователями
CREATE TABLE IF NOT EXISTS tracked_games (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    guild_id BIGINT NOT NULL,
    app_id INTEGER NOT NULL,
    game_name TEXT NOT NULL,
    notify_percent INTEGER DEFAULT 50,  -- При какой скидке уведомлять
    tracked_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_checked TIMESTAMP WITH TIME ZONE,
    last_notified TIMESTAMP WITH TIME ZONE,
    is_active BOOLEAN DEFAULT TRUE,
    
    -- Уникальность: один пользователь не может дважды отслеживать одну игру на сервере
    UNIQUE(user_id, guild_id, app_id)
);

-- Индексы для производительности
CREATE INDEX IF NOT EXISTS idx_tracked_user_guild ON tracked_games(user_id, guild_id) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_tracked_app_active ON tracked_games(app_id, is_active) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_tracked_last_checked ON tracked_games(last_checked) WHERE is_active = TRUE;

-- История цен (кеш)
CREATE TABLE IF NOT EXISTS price_history (
    id BIGSERIAL PRIMARY KEY,
    app_id INTEGER NOT NULL,
    price_current INTEGER,  -- В центах
    price_original INTEGER,
    discount_percent INTEGER DEFAULT 0,
    currency TEXT DEFAULT 'USD',
    lowest_price_ever INTEGER,
    lowest_price_date TIMESTAMP WITH TIME ZONE,
    highest_discount_ever INTEGER,
    highest_discount_date TIMESTAMP WITH TIME ZONE,
    checked_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    -- Данные с SteamDB
    steamdb_data JSONB,
    
    -- Партиционирование по дате для больших данных
    CONSTRAINT check_positive_prices CHECK (
        price_current >= 0 AND price_original >= 0
    )
);

-- Индексы
CREATE INDEX IF NOT EXISTS idx_price_app_date ON price_history(app_id, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_price_checked ON price_history(checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_price_discount ON price_history(discount_percent DESC) WHERE discount_percent > 0;

-- Раздачи и халява
CREATE TABLE IF NOT EXISTS freebies_posted (
    id BIGSERIAL PRIMARY KEY,
    app_id INTEGER,
    item_type TEXT NOT NULL,  -- 'game', 'dlc', 'item', 'frame', 'bundle'
    title TEXT NOT NULL,
    description TEXT,
    discount_percent INTEGER DEFAULT 0,
    original_price INTEGER,
    store_url TEXT,
    image_url TEXT,
    
    -- Временные рамки
    starts_at TIMESTAMP WITH TIME ZONE,
    ends_at TIMESTAMP WITH TIME ZONE,
    
    -- Публикация
    posted_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    posted_to_channel BIGINT,
    message_id BIGINT,
    
    -- Статус
    is_expired BOOLEAN DEFAULT FALSE,
    
    -- Уникальность (не публиковать дважды)
    UNIQUE(app_id, item_type, starts_at)
);

-- Индексы
CREATE INDEX IF NOT EXISTS idx_freebies_active ON freebies_posted(is_expired, ends_at) WHERE is_expired = FALSE;
CREATE INDEX IF NOT EXISTS idx_freebies_posted ON freebies_posted(posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_freebies_channel ON freebies_posted(posted_to_channel);

-- Интерактивные сообщения (для автоудаления)
CREATE TABLE IF NOT EXISTS interaction_messages (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL UNIQUE,
    command_type TEXT NOT NULL,  -- 'steam_db', 'common_games', etc.
    
    -- Данные для восстановления view
    app_id INTEGER,
    view_data JSONB,
    
    -- Время жизни
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    deleted_at TIMESTAMP WITH TIME ZONE,
    
    is_deleted BOOLEAN DEFAULT FALSE
);

-- Индексы
CREATE INDEX IF NOT EXISTS idx_messages_expires ON interaction_messages(expires_at) WHERE is_deleted = FALSE;
CREATE INDEX IF NOT EXISTS idx_messages_user ON interaction_messages(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_cleanup ON interaction_messages(is_deleted, expires_at);

-- Статистика онлайна (кеш)
CREATE TABLE IF NOT EXISTS player_stats (
    id BIGSERIAL PRIMARY KEY,
    app_id INTEGER NOT NULL,
    
    -- Текущий онлайн
    current_players INTEGER DEFAULT 0,
    
    -- 24 часа
    avg_24h INTEGER DEFAULT 0,
    peak_24h INTEGER DEFAULT 0,
    
    -- Месяц
    peak_month INTEGER DEFAULT 0,
    
    -- Всё время
    peak_alltime INTEGER DEFAULT 0,
    peak_alltime_date TIMESTAMP WITH TIME ZONE,
    
    -- Метаданные
    checked_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    source TEXT,  -- 'steamcharts', 'steamdb', 'steam_api'
    
    -- Один записей на игру
    UNIQUE(app_id)
);

-- Индексы
CREATE INDEX IF NOT EXISTS idx_player_stats_checked ON player_stats(checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_player_stats_current ON player_stats(current_players DESC);

-- Настройки пользователей
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id BIGINT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    
    -- Уведомления
    notify_freebies BOOLEAN DEFAULT TRUE,
    notify_tracked_games BOOLEAN DEFAULT TRUE,
    notify_threshold INTEGER DEFAULT 50,  -- По умолчанию при скидке 50%+
    
    -- DM настройки
    prefer_dm BOOLEAN DEFAULT FALSE,
    
    -- Язык
    language TEXT DEFAULT 'ru',
    
    -- Метаданные
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Индексы
CREATE INDEX IF NOT EXISTS idx_user_prefs_guild ON user_preferences(guild_id);

-- Lobby приглашения (для steam://joinlobby/)
CREATE TABLE IF NOT EXISTS lobby_invites (
    id BIGSERIAL PRIMARY KEY,
    invite_code TEXT NOT NULL UNIQUE,  -- Короткий код
    
    -- Steam данные
    app_id INTEGER NOT NULL,
    lobby_id TEXT NOT NULL,
    steam_id TEXT NOT NULL,
    
    -- Discord данные
    creator_id BIGINT NOT NULL,
    guild_id BIGINT NOT NULL,
    message_id BIGINT,
    
    -- Время жизни
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    
    is_active BOOLEAN DEFAULT TRUE
);

-- Индексы
CREATE INDEX IF NOT EXISTS idx_lobby_code ON lobby_invites(invite_code) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_lobby_expires ON lobby_invites(expires_at) WHERE is_active = TRUE;

-- Кеш игр (метаданные)
CREATE TABLE IF NOT EXISTS game_cache (
    app_id INTEGER PRIMARY KEY,
    game_name TEXT NOT NULL,
    description TEXT,
    header_image TEXT,
    developers TEXT[],
    publishers TEXT[],
    genres TEXT[],
    release_date DATE,
    is_free BOOLEAN DEFAULT FALSE,
    
    -- Метрики
    total_reviews INTEGER DEFAULT 0,
    positive_reviews INTEGER DEFAULT 0,
    
    -- Кеш
    last_updated TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    -- Full-text search
    search_vector tsvector GENERATED ALWAYS AS (
        to_tsvector('russian', COALESCE(game_name, ''))
    ) STORED
);

-- Индексы
CREATE INDEX IF NOT EXISTS idx_game_search ON game_cache USING GIN(search_vector);
CREATE INDEX IF NOT EXISTS idx_game_updated ON game_cache(last_updated DESC);

-- Логи (для отладки и мониторинга)
CREATE TABLE IF NOT EXISTS bot_logs (
    id BIGSERIAL PRIMARY KEY,
    log_level TEXT NOT NULL,  -- 'INFO', 'WARNING', 'ERROR', 'CRITICAL'
    module TEXT NOT NULL,
    message TEXT NOT NULL,
    user_id BIGINT,
    guild_id BIGINT,
    error_traceback TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Индексы
CREATE INDEX IF NOT EXISTS idx_logs_level_time ON bot_logs(log_level, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_logs_module ON bot_logs(module, created_at DESC);

-- Функция автообновления updated_at
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Триггер для user_preferences
CREATE TRIGGER update_user_preferences_updated_at 
    BEFORE UPDATE ON user_preferences
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Функция очистки старых логов (вызывать раз в неделю)
CREATE OR REPLACE FUNCTION cleanup_old_logs(days_to_keep INTEGER DEFAULT 30)
RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    DELETE FROM bot_logs 
    WHERE created_at < NOW() - (days_to_keep || ' days')::INTERVAL;
    
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;

-- Функция очистки устаревших раздач
CREATE OR REPLACE FUNCTION expire_old_freebies()
RETURNS INTEGER AS $$
DECLARE
    expired_count INTEGER;
BEGIN
    UPDATE freebies_posted 
    SET is_expired = TRUE
    WHERE is_expired = FALSE 
      AND ends_at < NOW();
    
    GET DIAGNOSTICS expired_count = ROW_COUNT;
    RETURN expired_count;
END;
$$ LANGUAGE plpgsql;

-- Представление: активные отслеживания
CREATE OR REPLACE VIEW v_active_tracking AS
SELECT 
    tg.user_id,
    tg.guild_id,
    tg.app_id,
    tg.game_name,
    tg.notify_percent,
    tg.tracked_at,
    ph.price_current,
    ph.discount_percent,
    ph.lowest_price_ever
FROM tracked_games tg
LEFT JOIN LATERAL (
    SELECT * FROM price_history 
    WHERE app_id = tg.app_id 
    ORDER BY checked_at DESC 
    LIMIT 1
) ph ON TRUE
WHERE tg.is_active = TRUE;

-- Представление: статистика по раздачам
CREATE OR REPLACE VIEW v_freebies_stats AS
SELECT 
    item_type,
    COUNT(*) as total_posted,
    COUNT(*) FILTER (WHERE is_expired = FALSE) as active,
    COUNT(*) FILTER (WHERE is_expired = TRUE) as expired,
    MAX(posted_at) as last_posted
FROM freebies_posted
GROUP BY item_type;

-- Комментарии к таблицам
COMMENT ON TABLE tracked_games IS 'Игры, отслеживаемые пользователями для уведомлений о скидках';
COMMENT ON TABLE price_history IS 'История цен игр Steam (кеш + аналитика)';
COMMENT ON TABLE freebies_posted IS 'Опубликованные раздачи (100% скидки, DLC, фреймы)';
COMMENT ON TABLE interaction_messages IS 'Интерактивные Discord сообщения для автоудаления';
COMMENT ON TABLE player_stats IS 'Статистика онлайна игроков (кеш)';
COMMENT ON TABLE lobby_invites IS 'Короткие коды для steam://joinlobby/ редиректов';
COMMENT ON TABLE game_cache IS 'Кеш метаданных игр из Steam Store';

-- Начальные данные (опционально)
-- INSERT INTO game_cache (app_id, game_name, is_free) VALUES (730, 'Counter-Strike 2', TRUE) ON CONFLICT DO NOTHING;

-- Завершение
SELECT 'Schema created successfully! Tables: ' || COUNT(*) 
FROM information_schema.tables 
WHERE table_schema = 'public' 
  AND table_name IN (
    'tracked_games', 'price_history', 'freebies_posted', 
    'interaction_messages', 'player_stats', 'user_preferences',
    'lobby_invites', 'game_cache', 'bot_logs'
  );
