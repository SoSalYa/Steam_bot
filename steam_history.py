"""
steam_history.py - Модуль для работы с историей цен и скидок
Записывает ежедневные снимки цен и вычисляет статистику
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
        """
        Сохраняет снимок цены в базу данных
        
        Args:
            appid: ID приложения
            cc: Код региона
            price_final: Финальная цена в центах
            price_initial: Начальная цена в центах
            discount_percent: Процент скидки
            currency: Код валюты
        
        Returns:
            True если успешно
        """
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO steam_price_history 
                    (appid, fetched_at, cc, price_final, price_initial, discount_percent, currency)
                    VALUES ($1, NOW(), $2, $3, $4, $5, $6)
                ''', appid, cc, price_final, price_initial, discount_percent, currency)
                
                # Обновляем сводную таблицу
                await self._update_summary(appid, discount_percent, conn)
                
                return True
                
        except Exception as e:
            print(f"Error saving price snapshot for {appid}: {e}")
            return False
    
    async def _update_summary(self, appid: int, discount_percent: int, conn: asyncpg.Connection):
        """Обновляет сводную таблицу с минимальными и последними скидками"""
        
        # Проверяем, существует ли запись
        existing = await conn.fetchrow(
            'SELECT * FROM steam_price_summary WHERE appid = $1',
            appid
        )
        
        now = datetime.utcnow()
        
        if not existing:
            # Создаем новую запись
            await conn.execute('''
                INSERT INTO steam_price_summary 
                (appid, first_seen, last_seen, min_discount, min_discount_date, last_discount, last_discount_date)
                VALUES ($1, $2, $2, $3, $2, $4, $2)
            ''', appid, now, discount_percent if discount_percent > 0 else None, 
                 discount_percent if discount_percent > 0 else None)
        else:
            # Обновляем существующую запись
            update_fields = ['last_seen = $2']
            params = [appid, now]
            param_idx = 3
            
            # Обновляем минимальную скидку
            if discount_percent > 0:
                current_min = existing['min_discount']
                if current_min is None or discount_percent < current_min:
                    update_fields.append(f'min_discount = ${param_idx}')
                    params.append(discount_percent)
                    param_idx += 1
                    update_fields.append(f'min_discount_date = ${param_idx}')
                    params.append(now)
                    param_idx += 1
                
                # Обновляем последнюю скидку
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
        Получает историю цен за указанный период
        
        Returns:
            Список словарей с данными о ценах
        """
        try:
            async with self.db_pool.acquire() as conn:
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
                    WHERE appid = $1 AND cc = $2 AND fetched_at >= NOW() - INTERVAL '{days} days'
                    ORDER BY fetched_at DESC
                ''', appid, cc)
                
                return [dict(row) for row in rows]
                
        except Exception as e:
            print(f"Error fetching price history for {appid}: {e}")
            return []
    
    async def get_discount_stats(self, appid: int) -> Dict:
        """
        Получает статистику по скидкам для игры
        
        Returns:
            {
                'min_discount': int,
                'min_discount_date': datetime,
                'last_discount': int,
                'last_discount_date': datetime,
                'total_snapshots': int,
                'first_seen': datetime,
                'last_seen': datetime
            }
        """
        try:
            async with self.db_pool.acquire() as conn:
                # Получаем из сводной таблицы
                summary = await conn.fetchrow(
                    'SELECT * FROM steam_price_summary WHERE appid = $1',
                    appid
                )
                
                if summary:
                    # Получаем общее количество снимков
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
                    # Нет данных в сводной таблице, вычисляем из истории
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
        
        # Находим даты для минимальной и последней скидки
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
        """
        Находит лучшую (максимальную) скидку за всю историю
        
        Returns:
            {
                'discount_percent': int,
                'price_final': int,
                'date': datetime,
                'cc': str
            }
        """
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
        Удаляет старые записи истории (старше N дней)
        Сохраняет только значимые точки (скидки, изменения цен)
        """
        try:
            async with self.db_pool.acquire() as conn:
                # Удаляем старые записи без скидок
                deleted = await conn.execute('''
                    DELETE FROM steam_price_history
                    WHERE fetched_at < NOW() - INTERVAL '{days} days'
                    AND discount_percent = 0
                ''')
                
                print(f"Cleaned up {deleted} old price history records")
                return True
                
        except Exception as e:
            print(f"Error cleaning up price history: {e}")
            return False
    
    async def get_tracked_games(self, discord_id: int) -> List[Dict]:
        """Получает список отслеживаемых игр пользователя"""
        try:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch('''
                    SELECT * FROM steam_tracked_games
                    WHERE discord_id = $1
                    ORDER BY created_at DESC
                ''', discord_id)
                
                return [dict(row) for row in rows]
                
        except Exception as e:
            print(f"Error fetching tracked games: {e}")
            return []
    
    async def add_tracked_game(
        self, 
        discord_id: int, 
        appid: int, 
        game_name: str, 
        notify_threshold: int = 50
    ) -> bool:
        """Добавляет игру в отслеживание для пользователя"""
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO steam_tracked_games (discord_id, appid, game_name, notify_threshold)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (discord_id, appid) DO UPDATE 
                    SET notify_threshold = $4
                ''', discord_id, appid, game_name, notify_threshold)
                
                return True
                
        except Exception as e:
            print(f"Error adding tracked game: {e}")
            return False
    
    async def remove_tracked_game(self, discord_id: int, appid: int) -> bool:
        """Удаляет игру из отслеживания"""
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute('''
                    DELETE FROM steam_tracked_games
                    WHERE discord_id = $1 AND appid = $2
                ''', discord_id, appid)
                
                return True
                
        except Exception as e:
            print(f"Error removing tracked game: {e}")
            return False


# Функция для создания экземпляра с пулом БД
def create_history_manager(db_pool: asyncpg.Pool) -> SteamPriceHistory:
    """Создает менеджер истории цен с пулом БД"""
    return SteamPriceHistory(db_pool)