"""
steam_history.py - FIXED VERSION with SQL injection protection
All INTERVAL queries now use safe parameterization
"""

import asyncpg
from typing import Dict, Optional, List
from datetime import datetime, timedelta


class SteamPriceHistory:
    """Класс для работы с историей цен в базе данных"""
    
    def __init__(self, db_pool: asyncpg.Pool):
        self.db_pool = db_pool
    
    async def save_price_snapshot(
        self, 
        appid: int, 
        cc: str, 
        price_final: int, 
        price_initial: int, 
        discount_percent: int, 
        currency: str
    ) -> bool:
        """Сохраняет снимок цены в базу данных"""
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO steam_price_history 
                    (appid, fetched_at, cc, price_final, price_initial, discount_percent, currency)
                    VALUES ($1, NOW(), $2, $3, $4, $5, $6)
                ''', appid, cc, price_final, price_initial, discount_percent, currency)
                
                await self._update_summary(appid, discount_percent, conn)
                return True
                
        except Exception as e:
            print(f"Error saving price snapshot for {appid}: {e}")
            return False
    
    async def _update_summary(self, appid: int, discount_percent: int, conn: asyncpg.Connection):
        """Обновляет сводную таблицу с минимальными и последними скидками"""
        
        existing = await conn.fetchrow(
            'SELECT * FROM steam_price_summary WHERE appid = $1',
            appid
        )
        
        now = datetime.utcnow()
        
        if not existing:
            await conn.execute('''
                INSERT INTO steam_price_summary 
                (appid, first_seen, last_seen, min_discount, min_discount_date, last_discount, last_discount_date)
                VALUES ($1, $2, $2, $3, $2, $4, $2)
            ''', appid, now, discount_percent if discount_percent > 0 else None, 
                 discount_percent if discount_percent > 0 else None)
        else:
            update_fields = ['last_seen = $2']
            params = [appid, now]
            param_idx = 3
            
            if discount_percent > 0:
                current_min = existing['min_discount']
                if current_min is None or discount_percent < current_min:
                    update_fields.append(f'min_discount = ${param_idx}')
                    params.append(discount_percent)
                    param_idx += 1
                    update_fields.append(f'min_discount_date = ${param_idx}')
                    params.append(now)
                    param_idx += 1
                
                update_fields.append(f'last_discount = ${param_idx}')
                params.append(discount_percent)
                param_idx += 1
                update_fields.append(f'last_discount_date = ${param_idx}')
                params.append(now)
                param_idx += 1
            
            query = f"UPDATE steam_price_summary SET {', '.join(update_fields)} WHERE appid = $1"
            await conn.execute(query, *params)
    
    async def get_price_history(
        self, 
        appid: int, 
        cc: str = 'us', 
        days: int = 365
    ) -> List[Dict]:
        """
        FIXED: Получает историю цен за указанный период
        Uses safe INTERVAL parameterization
        """
        try:
            async with self.db_pool.acquire() as conn:
                # SAFE: Use parameter concatenation for interval
                rows = await conn.fetch('''
                    SELECT 
                        appid, 
                        fetched_at, 
                        cc, 
                        price_final, 
                        price_initial, 
                        discount_percent, 
                        currency
                    FROM steam_price_history
                    WHERE appid = $1 
                      AND cc = $2 
                      AND fetched_at >= NOW() - ($3 || ' days')::interval
                    ORDER BY fetched_at DESC
                ''', appid, cc, str(days))
                
                return [dict(row) for row in rows]
                
        except Exception as e:
            print(f"Error fetching price history for {appid}: {e}")
            return []
    
    async def get_discount_stats(self, appid: int) -> Dict:
        """Получает статистику по скидкам для игры"""
        try:
            async with self.db_pool.acquire() as conn:
                summary = await conn.fetchrow(
                    'SELECT * FROM steam_price_summary WHERE appid = $1',
                    appid
                )
                
                if summary:
                    count = await conn.fetchval(
                        'SELECT COUNT(*) FROM steam_price_history WHERE appid = $1',
                        appid
                    )
                    
                    return {
                        'min_discount': summary['min_discount'],
                        'min_discount_date': summary['min_discount_date'],
                        'last_discount': summary['last_discount'],
                        'last_discount_date': summary['last_discount_date'],
                        'total_snapshots': count or 0,
                        'first_seen': summary['first_seen'],
                        'last_seen': summary['last_seen']
                    }
                else:
                    return await self._calculate_stats_from_history(appid, conn)
                    
        except Exception as e:
            print(f"Error fetching discount stats for {appid}: {e}")
            return {}
    
    async def _calculate_stats_from_history(self, appid: int, conn: asyncpg.Connection) -> Dict:
        """Вычисляет статистику напрямую из таблицы истории"""
        
        stats = await conn.fetchrow('''
            SELECT 
                MIN(CASE WHEN discount_percent > 0 THEN discount_percent END) as min_discount,
                MAX(CASE WHEN discount_percent > 0 THEN discount_percent END) as max_discount,
                COUNT(*) as total_snapshots,
                MIN(fetched_at) as first_seen,
                MAX(fetched_at) as last_seen
            FROM steam_price_history
            WHERE appid = $1
        ''', appid)
        
        min_discount_row = await conn.fetchrow('''
            SELECT fetched_at, discount_percent
            FROM steam_price_history
            WHERE appid = $1 AND discount_percent > 0
            ORDER BY discount_percent ASC, fetched_at DESC
            LIMIT 1
        ''', appid)
        
        last_discount_row = await conn.fetchrow('''
            SELECT fetched_at, discount_percent
            FROM steam_price_history
            WHERE appid = $1 AND discount_percent > 0
            ORDER BY fetched_at DESC
            LIMIT 1
        ''', appid)
        
        return {
            'min_discount': min_discount_row['discount_percent'] if min_discount_row else None,
            'min_discount_date': min_discount_row['fetched_at'] if min_discount_row else None,
            'last_discount': last_discount_row['discount_percent'] if last_discount_row else None,
            'last_discount_date': last_discount_row['fetched_at'] if last_discount_row else None,
            'total_snapshots': stats['total_snapshots'] or 0,
            'first_seen': stats['first_seen'],
            'last_seen': stats['last_seen']
        }
    
    async def get_best_discount_ever(self, appid: int) -> Optional[Dict]:
        """Находит лучшую (максимальную) скидку за всю историю"""
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow('''
                    SELECT discount_percent, price_final, fetched_at, cc, currency
                    FROM steam_price_history
                    WHERE appid = $1 AND discount_percent > 0
                    ORDER BY discount_percent DESC, fetched_at DESC
                    LIMIT 1
                ''', appid)
                
                if row:
                    return {
                        'discount_percent': row['discount_percent'],
                        'price_final': row['price_final'],
                        'date': row['fetched_at'],
                        'cc': row['cc'],
                        'currency': row['currency']
                    }
                    
        except Exception as e:
            print(f"Error fetching best discount for {appid}: {e}")
        
        return None
    
    async def cleanup_old_history(self, days: int = 730):
        """
        FIXED: Удаляет старые записи истории
        Uses safe INTERVAL parameterization
        """
        try:
            async with self.db_pool.acquire() as conn:
                # SAFE: Use parameter for interval
                result = await conn.execute('''
                    DELETE FROM steam_price_history
                    WHERE fetched_at < NOW() - ($1 || ' days')::interval
                      AND discount_percent = 0
                ''', str(days))
                
                print(f"Cleaned up {result} old price history records")
                return True
                
        except Exception as e:
            print(f"Error cleaning up price history: {e}")
            return False
    
    async def get_tracked_games(self, discord_id: int, guild_id: int) -> List[Dict]:
        """
        UPDATED: Получает список отслеживаемых игр пользователя
        Now includes guild_id for multi-server support
        """
        try:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch('''
                    SELECT * FROM steam_tracked_games
                    WHERE discord_id = $1 AND guild_id = $2 AND is_active = TRUE
                    ORDER BY created_at DESC
                ''', discord_id, guild_id)
                
                return [dict(row) for row in rows]
                
        except Exception as e:
            print(f"Error fetching tracked games: {e}")
            return []
    
    async def add_tracked_game(
        self, 
        discord_id: int, 
        appid: int, 
        game_name: str,
        guild_id: int,
        notify_threshold: int = 50
    ) -> bool:
        """
        UPDATED: Добавляет игру в отслеживание
        Now includes guild_id
        """
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO steam_tracked_games 
                    (discord_id, appid, game_name, guild_id, notify_threshold, is_active)
                    VALUES ($1, $2, $3, $4, $5, TRUE)
                    ON CONFLICT (discord_id, appid, guild_id) 
                    DO UPDATE SET 
                        notify_threshold = $5,
                        is_active = TRUE,
                        game_name = $3
                ''', discord_id, appid, game_name, guild_id, notify_threshold)
                
                return True
                
        except Exception as e:
            print(f"Error adding tracked game: {e}")
            return False
    
    async def remove_tracked_game(self, discord_id: int, appid: int, guild_id: int) -> bool:
        """
        UPDATED: Удаляет игру из отслеживания
        Now uses soft delete (is_active = FALSE)
        """
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute('''
                    UPDATE steam_tracked_games
                    SET is_active = FALSE
                    WHERE discord_id = $1 AND appid = $2 AND guild_id = $3
                ''', discord_id, appid, guild_id)
                
                return True
                
        except Exception as e:
            print(f"Error removing tracked game: {e}")
            return False
    
    async def get_games_needing_notification(self, hours_since_last: int = 24) -> List[Dict]:
        """
        FIXED: Get games that need discount notifications
        Uses safe INTERVAL parameterization
        """
        try:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch('''
                    SELECT 
                        t.discord_id,
                        t.appid,
                        t.game_name,
                        t.guild_id,
                        t.notify_threshold,
                        h.discount_percent,
                        h.price_final,
                        h.price_initial,
                        h.currency
                    FROM steam_tracked_games t
                    INNER JOIN LATERAL (
                        SELECT * FROM steam_price_history
                        WHERE appid = t.appid 
                          AND cc = 'us'
                          AND discount_percent >= t.notify_threshold
                        ORDER BY fetched_at DESC
                        LIMIT 1
                    ) h ON TRUE
                    WHERE t.is_active = TRUE
                      AND (
                          t.last_notified IS NULL 
                          OR t.last_notified < NOW() - ($1 || ' hours')::interval
                      )
                ''', str(hours_since_last))
                
                return [dict(row) for row in rows]
                
        except Exception as e:
            print(f"Error fetching games needing notification: {e}")
            return []
    
    async def mark_notified(self, discord_id: int, appid: int, guild_id: int):
        """Mark that user was notified about this game"""
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute('''
                    UPDATE steam_tracked_games
                    SET last_notified = NOW()
                    WHERE discord_id = $1 AND appid = $2 AND guild_id = $3
                ''', discord_id, appid, guild_id)
        except Exception as e:
            print(f"Error marking notified: {e}")


def create_history_manager(db_pool: asyncpg.Pool) -> SteamPriceHistory:
    """Создает менеджер истории цен с пулом БД"""
    return SteamPriceHistory(db_pool)
