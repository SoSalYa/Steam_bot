"""
config.py - Централизованная конфигурация бота
Все настройки загружаются из .env файла
"""

import os
from typing import Optional
from dotenv import load_dotenv

# Загрузка .env
load_dotenv()


class Config:
    """Конфигурация бота"""
    
    # ============================================
    # Discord
    # ============================================
    DISCORD_TOKEN: str = os.getenv('DISCORD_TOKEN', '')
    BOT_PREFIX: str = os.getenv('BOT_PREFIX', '!')
    
    # ID каналов
    # Channels for notifications
    DISCOUNT_CHANNEL_ID: Optional[int] = int(os.getenv('DISCOUNT_CHANNEL_ID', 0)) or None
    EPIC_CHANNEL_ID: Optional[int] = int(os.getenv('EPIC_CHANNEL_ID', 0)) or None
    FREEBIES_CHANNEL_ID: Optional[int] = int(os.getenv('FREEBIES_CHANNEL_ID', 0)) or None
    LOG_CHANNEL_ID: Optional[int] = int(os.getenv('LOG_CHANNEL_ID', 0)) or None
    
    # ============================================
    # База Данных
    # ============================================
    DATABASE_URL: str = os.getenv('DATABASE_URL', '')
    DB_POOL_MIN: int = int(os.getenv('DB_POOL_MIN', 5))
    DB_POOL_MAX: int = int(os.getenv('DB_POOL_MAX', 20))
    
    # ============================================
    # Steam API
    # ============================================
    STEAM_API_KEY: str = os.getenv('STEAM_API_KEY', '')
    
    # ============================================
    # SteamDB
    # ============================================
    STEAMDB_BASE_URL: str = os.getenv('STEAMDB_BASE_URL', 'https://steamdb.info')
    STEAMDB_API_URL: str = os.getenv('STEAMDB_API_URL', '')  # Если есть неофициальное API
    
    # ============================================
    # SteamCharts/PlayerCharts
    # ============================================
    STEAMCHARTS_URL: str = 'https://steamcharts.com'
    STEAM_PLAYER_API: str = 'https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/'
    
    # ============================================
    # Redis (опционально)
    # ============================================
    REDIS_URL: Optional[str] = os.getenv('REDIS_URL')
    CACHE_TTL_PRICE: int = int(os.getenv('CACHE_TTL_PRICE', 3600))  # 1 час
    CACHE_TTL_PLAYERS: int = int(os.getenv('CACHE_TTL_PLAYERS', 300))  # 5 минут
    
    # ============================================
    # Задачи (Background Tasks)
    # ============================================
    
    # Проверка цен
    PRICE_CHECK_INTERVAL: int = int(os.getenv('PRICE_CHECK_INTERVAL', 600))  # 10 минут
    PRICE_CHECK_CONCURRENCY: int = int(os.getenv('PRICE_CHECK_CONCURRENCY', 5))
    
    # Проверка раздач
    FREEBIES_CHECK_INTERVAL: int = int(os.getenv('FREEBIES_CHECK_INTERVAL', 600))  # 10 минут
    
    # Очистка сообщений
    MESSAGE_CLEANUP_INTERVAL: int = int(os.getenv('MESSAGE_CLEANUP_INTERVAL', 120))  # 2 минуты
    MESSAGE_LIFETIME: int = int(os.getenv('MESSAGE_LIFETIME', 900))  # 15 минут
    
    # ============================================
    # HTTP
    # ============================================
    HTTP_TIMEOUT: int = int(os.getenv('HTTP_TIMEOUT', 30))
    HTTP_MAX_RETRIES: int = int(os.getenv('HTTP_MAX_RETRIES', 3))
    HTTP_USER_AGENT: str = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    
    # ============================================
    # Rate Limiting
    # ============================================
    RATE_LIMIT_REQUESTS: int = int(os.getenv('RATE_LIMIT_REQUESTS', 50))
    RATE_LIMIT_WINDOW: int = int(os.getenv('RATE_LIMIT_WINDOW', 60))
    
    # ============================================
    # Уведомления
    # ============================================
    DEFAULT_NOTIFY_THRESHOLD: int = int(os.getenv('DEFAULT_NOTIFY_THRESHOLD', 50))  # 50% скидка
    NOTIFY_COOLDOWN_HOURS: int = int(os.getenv('NOTIFY_COOLDOWN_HOURS', 24))
    
    # ============================================
    # Lobby Invites
    # ============================================
    LOBBY_REDIRECT_URL: str = os.getenv('LOBBY_REDIRECT_URL', '')  # https://yourdomain.com
    LOBBY_INVITE_LIFETIME: int = int(os.getenv('LOBBY_INVITE_LIFETIME', 3600))  # 1 час
    
    # ============================================
    # Логирование
    # ============================================
    LOG_LEVEL: str = os.getenv('LOG_LEVEL', 'INFO')
    LOG_TO_DB: bool = os.getenv('LOG_TO_DB', 'false').lower() == 'true'
    
    # ============================================
    # Sentry (опционально)
    # ============================================
    SENTRY_DSN: Optional[str] = os.getenv('SENTRY_DSN')
    
    # ============================================
    # Render
    # ============================================
    PORT: int = int(os.getenv('PORT', 10000))
    RENDER_EXTERNAL_URL: str = os.getenv('RENDER_EXTERNAL_URL', '')
    
    # ============================================
    # Feature Flags
    # ============================================
    ENABLE_FREEBIES_CHECK: bool = os.getenv('ENABLE_FREEBIES_CHECK', 'true').lower() == 'true'
    ENABLE_PRICE_TRACKING: bool = os.getenv('ENABLE_PRICE_TRACKING', 'true').lower() == 'true'
    ENABLE_MESSAGE_CLEANUP: bool = os.getenv('ENABLE_MESSAGE_CLEANUP', 'true').lower() == 'true'
    ENABLE_GRAPHS: bool = os.getenv('ENABLE_GRAPHS', 'true').lower() == 'true'
    
    # ============================================
    # Валидация
    # ============================================
    
    @classmethod
    def validate(cls) -> None:
        """Проверка обязательных настроек"""
        errors = []
        
        if not cls.DISCORD_TOKEN:
            errors.append("DISCORD_TOKEN is required")
        
        if not cls.DATABASE_URL:
            errors.append("DATABASE_URL is required")
        
        if not cls.STEAM_API_KEY:
            errors.append("STEAM_API_KEY is required (get from https://steamcommunity.com/dev/apikey)")
        
        if errors:
            raise ValueError(f"Configuration errors:\n" + "\n".join(f"- {e}" for e in errors))
    
    @classmethod
    def get_database_url(cls) -> str:
        """Получить DATABASE_URL с поддержкой разных форматов"""
        url = cls.DATABASE_URL
        
        # Supabase использует postgresql://
        # Некоторые библиотеки требуют postgres://
        if url.startswith('postgres://'):
            url = url.replace('postgres://', 'postgresql://', 1)
        
        return url
    
    @classmethod
    def is_production(cls) -> bool:
        """Проверка production окружения"""
        return os.getenv('ENVIRONMENT', 'development').lower() == 'production'


# Экспорт для удобства
config = Config()


# Валидация при импорте
try:
    config.validate()
except ValueError as e:
    print(f"⚠️ Configuration Error: {e}")
    print("Please check your .env file")
