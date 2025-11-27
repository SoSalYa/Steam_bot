-- ============================================
-- Steam DB Features Migration
-- Добавляет таблицы для отслеживания истории цен
-- ============================================

-- Таблица истории цен (ежедневные снимки)
CREATE TABLE IF NOT EXISTS steam_price_history (
    id BIGSERIAL PRIMARY KEY,
    appid INTEGER NOT NULL,
    fetched_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    cc TEXT NOT NULL DEFAULT 'us', -- регион (us, eu, ru и т.д.)
    price_final INTEGER, -- цена в центах
    price_initial INTEGER, -- начальная цена до скидки
    discount_percent INTEGER DEFAULT 0, -- процент скидки
    currency TEXT DEFAULT 'USD', -- валюта
    
    -- Индексы для быстрого поиска
    CONSTRAINT check_positive_prices CHECK (
        price_final >= 0 AND 
        price_initial >= 0 AND 
        discount_percent >= 0 AND 
        discount_percent <= 100
    )
);

-- Индексы для оптимизации запросов
CREATE INDEX IF NOT EXISTS idx_price_history_appid ON steam_price_history(appid);
CREATE INDEX IF NOT EXISTS idx_price_history_fetched_at ON steam_price_history(fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_price_history_appid_cc ON steam_price_history(appid, cc);
CREATE INDEX IF NOT EXISTS idx_price_history_discount ON steam_price_history(appid, discount_percent DESC) 
    WHERE discount_percent > 0;

-- Сводная таблица с агрегированными данными о скидках
CREATE TABLE IF NOT EXISTS steam_price_summary (
    appid INTEGER PRIMARY KEY,
    first_seen TIMESTAMP WITH TIME ZONE DEFAULT NOW(), -- когда впервые отслежена игра
    last_seen TIMESTAMP WITH TIME ZONE DEFAULT NOW(), -- последнее обновление
    
    -- Минимальная скидка за всю историю (самая выгодная)
    min_discount INTEGER,
    min_discount_date TIMESTAMP WITH TIME ZONE,
    
    -- Максимальная скидка (для справки)
    max_discount INTEGER DEFAULT 0,
    max_discount_date TIMESTAMP WITH TIME ZONE,
    
    -- Последняя зафиксированная скидка
    last_discount INTEGER,
    last_discount_date TIMESTAMP WITH TIME ZONE,
    
    -- Статистика
    total_checks INTEGER DEFAULT 0, -- сколько раз проверялась цена
    times_on_sale INTEGER DEFAULT 0, -- сколько раз была на распродаже
    
    CONSTRAINT check_valid_discounts CHECK (
        (min_discount IS NULL OR (min_discount >= 0 AND min_discount <= 100)) AND
        (max_discount >= 0 AND max_discount <= 100) AND
        (last_discount IS NULL OR (last_discount >= 0 AND last_discount <= 100))
    )
);

-- Индексы для summary
CREATE INDEX IF NOT EXISTS idx_price_summary_last_seen ON steam_price_summary(last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_price_summary_max_discount ON steam_price_summary(max_discount DESC) 
    WHERE max_discount > 0;

-- Таблица отслеживаемых игр пользователями
CREATE TABLE IF NOT EXISTS steam_tracked_games (
    id BIGSERIAL PRIMARY KEY,
    discord_id BIGINT NOT NULL, -- ID пользователя Discord
    appid INTEGER NOT NULL, -- ID игры в Steam
    game_name TEXT NOT NULL, -- название игры
    notify_threshold INTEGER DEFAULT 50, -- при какой скидке уведомлять (в процентах)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_notified TIMESTAMP WITH TIME ZONE, -- когда последний раз отправляли уведомление
    is_active BOOLEAN DEFAULT TRUE, -- активно ли отслеживание
    
    -- Один пользователь не может добавить одну игру дважды
    UNIQUE(discord_id, appid),
    
    CONSTRAINT check_threshold CHECK (notify_threshold >= 0 AND notify_threshold <= 100)
);

-- Индексы для tracked games
CREATE INDEX IF NOT EXISTS idx_tracked_games_discord_id ON steam_tracked_games(discord_id);
CREATE INDEX IF NOT EXISTS idx_tracked_games_appid ON steam_tracked_games(appid);
CREATE INDEX IF NOT EXISTS idx_tracked_games_active ON steam_tracked_games(is_active) 
    WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_tracked_games_threshold ON steam_tracked_games(appid, notify_threshold);

-- Таблица с кешем информации об играх (для уменьшения запросов к Steam API)
CREATE TABLE IF NOT EXISTS steam_game_cache (
    appid INTEGER PRIMARY KEY,
    game_name TEXT NOT NULL,
    short_description TEXT,
    header_image TEXT, -- URL заголовочной картинки
    genres TEXT[], -- массив жанров
    developers TEXT[], -- разработчики
    publishers TEXT[], -- издатели
    release_date DATE, -- дата выхода
    is_free BOOLEAN DEFAULT FALSE,
    last_updated TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    -- Метаданные
    metacritic_score INTEGER,
    steam_reviews_positive INTEGER DEFAULT 0,
    steam_reviews_negative INTEGER DEFAULT 0
);

-- Индексы для game cache
CREATE INDEX IF NOT EXISTS idx_game_cache_name ON steam_game_cache 
    USING gin(to_tsvector('english', game_name));
CREATE INDEX IF NOT EXISTS idx_game_cache_updated ON steam_game_cache(last_updated DESC);

-- Функция для автоматического обновления timestamp
CREATE OR REPLACE FUNCTION update_modified_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.last_seen = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Триггер для автообновления last_seen в summary
DROP TRIGGER IF EXISTS update_price_summary_modtime ON steam_price_summary;
CREATE TRIGGER update_price_summary_modtime
    BEFORE UPDATE ON steam_price_summary
    FOR EACH ROW
    EXECUTE FUNCTION update_modified_column();

-- Представление для удобного просмотра лучших скидок
CREATE OR REPLACE VIEW v_best_current_discounts AS
SELECT 
    h.appid,
    g.game_name,
    h.discount_percent,
    h.price_final,
    h.price_initial,
    h.currency,
    h.fetched_at,
    s.max_discount as historical_max_discount
FROM steam_price_history h
LEFT JOIN games g ON h.appid = g.appid
LEFT JOIN steam_price_summary s ON h.appid = s.appid
WHERE h.discount_percent > 0
    AND h.fetched_at >= NOW() - INTERVAL '24 hours'
    AND h.cc = 'us'
ORDER BY h.discount_percent DESC, h.fetched_at DESC;

-- Представление для статистики по играм
CREATE OR REPLACE VIEW v_game_price_stats AS
SELECT 
    s.appid,
    g.game_name,
    s.min_discount,
    s.max_discount,
    s.last_discount,
    s.times_on_sale,
    s.total_checks,
    s.first_seen,
    s.last_seen,
    CASE 
        WHEN s.total_checks > 0 THEN 
            ROUND((s.times_on_sale::NUMERIC / s.total_checks::NUMERIC) * 100, 2)
        ELSE 0 
    END as sale_frequency_percent
FROM steam_price_summary s
LEFT JOIN games g ON s.appid = g.appid
ORDER BY s.max_discount DESC NULLS LAST;

-- Комментарии к таблицам
COMMENT ON TABLE steam_price_history IS 'История изменений цен игр Steam (ежедневные снимки)';
COMMENT ON TABLE steam_price_summary IS 'Сводная статистика по ценам и скидкам для каждой игры';
COMMENT ON TABLE steam_tracked_games IS 'Игры, отслеживаемые пользователями для уведомлений о скидках';
COMMENT ON TABLE steam_game_cache IS 'Кеш метаданных игр из Steam Store API';

COMMENT ON COLUMN steam_price_history.price_final IS 'Финальная цена в центах (100 = $1.00)';
COMMENT ON COLUMN steam_price_history.price_initial IS 'Цена до скидки в центах';
COMMENT ON COLUMN steam_price_history.discount_percent IS 'Процент скидки (0-100)';

COMMENT ON COLUMN steam_price_summary.min_discount IS 'Минимальная скидка (самая выгодная цена)';
COMMENT ON COLUMN steam_price_summary.max_discount IS 'Максимальная скидка (самая большая % скидка)';
COMMENT ON COLUMN steam_price_summary.times_on_sale IS 'Количество раз когда игра была на распродаже';

-- Начальные данные
-- Можно добавить популярные игры для отслеживания
-- INSERT INTO steam_game_cache (appid, game_name) VALUES 
-- (730, 'Counter-Strike 2'),
-- (570, 'Dota 2'),
-- (440, 'Team Fortress 2')
-- ON CONFLICT (appid) DO NOTHING;

-- Завершение
SELECT 'Steam DB migration completed successfully!' as status;