-- ============================================
-- Migration 001: Security Fixes & Guild Support
-- Fixes SQL injection, adds guild_id tracking
-- ============================================

-- Add guild_id to tracked games for multi-server support
ALTER TABLE steam_tracked_games 
ADD COLUMN IF NOT EXISTS guild_id BIGINT;

ALTER TABLE steam_tracked_games 
ADD COLUMN IF NOT EXISTS last_notified TIMESTAMP WITH TIME ZONE;

-- Drop old constraint if exists
ALTER TABLE steam_tracked_games 
DROP CONSTRAINT IF EXISTS steam_tracked_games_discord_id_appid_key;

-- Create new unique constraint including guild_id
CREATE UNIQUE INDEX IF NOT EXISTS ux_tracked_games_user_app_guild 
ON steam_tracked_games(discord_id, appid, guild_id);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_tracked_guild_active 
ON steam_tracked_games(guild_id, is_active) 
WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_tracked_appid_guild 
ON steam_tracked_games(appid, guild_id);

CREATE INDEX IF NOT EXISTS idx_tracked_last_notified 
ON steam_tracked_games(last_notified) 
WHERE last_notified IS NOT NULL;

-- Optimize price history queries
CREATE INDEX IF NOT EXISTS idx_price_appid_date_desc 
ON steam_price_history(appid, fetched_at DESC);

CREATE INDEX IF NOT EXISTS idx_price_appid_cc_date 
ON steam_price_history(appid, cc, fetched_at DESC);

-- Optimize summary lookups
CREATE INDEX IF NOT EXISTS idx_price_summary_last_seen 
ON steam_price_summary(last_seen DESC);

CREATE INDEX IF NOT EXISTS idx_price_summary_max_discount_filtered 
ON steam_price_summary(max_discount DESC) 
WHERE max_discount > 0;

-- Add message tracking for persistent views
CREATE TABLE IF NOT EXISTS steam_ui_messages (
    message_id BIGINT PRIMARY KEY,
    channel_id BIGINT NOT NULL,
    guild_id BIGINT,
    appid INTEGER NOT NULL,
    user_id BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    expires_at TIMESTAMP WITH TIME ZONE,
    view_data JSONB -- Store view state if needed
);

CREATE INDEX IF NOT EXISTS idx_ui_messages_expires 
ON steam_ui_messages(expires_at) 
WHERE expires_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ui_messages_appid 
ON steam_ui_messages(appid);

-- Leader election table for distributed tasks
CREATE TABLE IF NOT EXISTS bot_leader_election (
    lock_name TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    acquired_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    last_heartbeat TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_leader_expires 
ON bot_leader_election(expires_at);

-- Rate limiting table (fallback if no Redis)
CREATE TABLE IF NOT EXISTS rate_limits (
    key TEXT PRIMARY KEY,
    count INTEGER DEFAULT 1,
    window_start TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rate_limits_expires 
ON rate_limits(expires_at);

-- Add game cache for faster lookups
CREATE TABLE IF NOT EXISTS steam_game_cache (
    appid INTEGER PRIMARY KEY,
    game_name TEXT NOT NULL,
    short_description TEXT,
    header_image TEXT,
    genres TEXT[],
    developers TEXT[],
    publishers TEXT[],
    release_date DATE,
    is_free BOOLEAN DEFAULT FALSE,
    metacritic_score INTEGER,
    steam_reviews_positive INTEGER DEFAULT 0,
    steam_reviews_negative INTEGER DEFAULT 0,
    last_updated TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_game_cache_name_trgm 
ON steam_game_cache USING gin(game_name gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_game_cache_updated 
ON steam_game_cache(last_updated DESC);

-- Function to clean expired data
CREATE OR REPLACE FUNCTION cleanup_expired_data()
RETURNS void AS $$
BEGIN
    DELETE FROM steam_ui_messages WHERE expires_at < NOW();
    DELETE FROM rate_limits WHERE expires_at < NOW();
    DELETE FROM bot_leader_election WHERE expires_at < NOW();
END;
$$ LANGUAGE plpgsql;

-- Comments
COMMENT ON TABLE steam_ui_messages IS 'Tracks persistent UI messages for view reconstruction on restart';
COMMENT ON TABLE bot_leader_election IS 'Distributed lock for leader election in multi-instance deployments';
COMMENT ON COLUMN steam_tracked_games.guild_id IS 'Server ID for multi-guild tracking isolation';
COMMENT ON COLUMN steam_tracked_games.last_notified IS 'Prevent duplicate notifications within 24h';

-- Grant permissions (adjust role name as needed)
-- GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO your_bot_user;

SELECT 'Migration 001 completed successfully - Security fixes and guild support added' AS status;
