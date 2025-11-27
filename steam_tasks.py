"""
steam_tasks.py - Фоновые задачи для автоматического обновления цен и уведомлений
"""

import discord
from discord.ext import tasks
from datetime import datetime, time as dtime
import asyncio
from typing import List, Dict
import asyncpg

from steam_price import steam_price
from steam_history import SteamPriceHistory


class SteamBackgroundTasks:
    """Класс для управления фоновыми задачами обновления Steam данных"""
    
    def __init__(
        self, 
        bot: discord.Client, 
        db_pool: asyncpg.Pool,
        history_manager: SteamPriceHistory,
        notification_channel_id: int = None
    ):
        self.bot = bot
        self.db_pool = db_pool
        self.history = history_manager
        self.notification_channel_id = notification_channel_id
        
        # Задачи
        self.price_update_task = None
        self.discount_notify_task = None
        self.cleanup_task = None
    
    def start_all_tasks(self):
        """Запускает все фоновые задачи"""
        print("🔄 Starting Steam background tasks...")
        
        if not self.price_update_task or not self.price_update_task.is_running():
            self.price_update_task = self._create_price_update_task()
            self.price_update_task.start()
            print("✅ Price update task started")
        
        if not self.discount_notify_task or not self.discount_notify_task.is_running():
            self.discount_notify_task = self._create_discount_notify_task()
            self.discount_notify_task.start()
            print("✅ Discount notification task started")
        
        if not self.cleanup_task or not self.cleanup_task.is_running():
            self.cleanup_task = self._create_cleanup_task()
            self.cleanup_task.start()
            print("✅ Cleanup task started")
    
    def stop_all_tasks(self):
        """Останавливает все задачи"""
        if self.price_update_task:
            self.price_update_task.cancel()
        if self.discount_notify_task:
            self.discount_notify_task.cancel()
        if self.cleanup_task:
            self.cleanup_task.cancel()
        print("⏹️ All Steam tasks stopped")
    
    def _create_price_update_task(self):
        """Создает задачу ежедневного обновления цен"""
        
        @tasks.loop(hours=12)
        async def update_prices():
            """
            Обновляет цены для популярных игр в БД каждые 12 часов
            """
            try:
                print(f"[{datetime.utcnow()}] Starting price update task...")
                
                # Получаем список уникальных игр из базы
                async with self.db_pool.acquire() as conn:
                    # Берем игры, которые есть у пользователей
                    rows = await conn.fetch('''
                        SELECT DISTINCT appid, game_name
                        FROM games
                        WHERE appid IS NOT NULL
                        LIMIT 500
                    ''')
                    
                    total = len(rows)
                    print(f"Found {total} unique games to update")
                    
                    updated = 0
                    errors = 0
                    
                    for idx, row in enumerate(rows, 1):
                        appid = row['appid']
                        
                        try:
                            # Получаем цену для US региона
                            price_data = await steam_price.get_price_info(appid, 'us', use_cache=False)
                            
                            # Сохраняем снимок
                            if not price_data.get('error') and not price_data.get('is_free'):
                                success = await self.history.save_price_snapshot(
                                    appid,
                                    'us',
                                    price_data.get('price_final', 0),
                                    price_data.get('price_initial', 0),
                                    price_data.get('discount_percent', 0),
                                    price_data.get('currency', 'USD')
                                )
                                
                                if success:
                                    updated += 1
                            
                            # Прогресс каждые 50 игр
                            if idx % 50 == 0:
                                print(f"Progress: {idx}/{total} games processed ({updated} updated, {errors} errors)")
                            
                            # Задержка между запросами
                            await asyncio.sleep(2)
                            
                        except Exception as e:
                            errors += 1
                            if errors < 10:  # Логируем только первые 10 ошибок
                                print(f"Error updating price for {appid}: {e}")
                    
                    print(f"✅ Price update completed: {updated} games updated, {errors} errors")
                    
            except Exception as e:
                print(f"❌ Error in price update task: {e}")
        
        return update_prices
    
    def _create_discount_notify_task(self):
        """Создает задачу проверки скидок и отправки уведомлений"""
        
        @tasks.loop(hours=6)
        async def check_discounts():
            """
            Проверяет скидки для отслеживаемых игр и отправляет уведомления
            """
            if not self.notification_channel_id:
                return
            
            try:
                channel = self.bot.get_channel(self.notification_channel_id)
                if not channel:
                    return
                
                print(f"[{datetime.utcnow()}] Checking discount notifications...")
                
                # Получаем все отслеживаемые игры
                async with self.db_pool.acquire() as conn:
                    tracked = await conn.fetch('''
                        SELECT DISTINCT ON (appid) 
                            discord_id, appid, game_name, notify_threshold
                        FROM steam_tracked_games
                        ORDER BY appid, created_at DESC
                    ''')
                    
                    print(f"Found {len(tracked)} tracked games")
                    
                    notifications_sent = 0
                    
                    for track in tracked:
                        appid = track['appid']
                        discord_id = track['discord_id']
                        game_name = track['game_name']
                        threshold = track['notify_threshold']
                        
                        try:
                            # Получаем текущую цену
                            price_data = await steam_price.get_price_info(appid, 'us')
                            
                            discount = price_data.get('discount_percent', 0)
                            
                            # Проверяем, нужно ли отправить уведомление
                            if discount >= threshold:
                                # Проверяем, не отправляли ли мы уже уведомление недавно
                                last_notify = await conn.fetchval('''
                                    SELECT MAX(fetched_at) 
                                    FROM steam_price_history
                                    WHERE appid = $1 AND discount_percent >= $2
                                    AND fetched_at >= NOW() - INTERVAL '24 hours'
                                ''', appid, threshold)
                                
                                if not last_notify:
                                    # Отправляем уведомление
                                    user = await self.bot.fetch_user(discord_id)
                                    if user:
                                        embed = discord.Embed(
                                            title=f"🔥 Discount Alert: {game_name}",
                                            description=(
                                                f"**{game_name}** is now **-{discount}% OFF**!\n\n"
                                                f"Price: ~~{price_data.get('formatted_initial')}~~ → **{price_data.get('formatted_final')}**\n\n"
                                                f"[View on Steam](https://store.steampowered.com/app/{appid})"
                                            ),
                                            color=0xff6b6b,
                                            timestamp=datetime.utcnow()
                                        )
                                        
                                        header_url = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"
                                        embed.set_thumbnail(url=header_url)
                                        
                                        embed.set_footer(text=f"You set an alert for {threshold}% discount")
                                        
                                        try:
                                            await user.send(embed=embed)
                                            notifications_sent += 1
                                        except discord.Forbidden:
                                            print(f"Cannot send DM to user {discord_id}")
                            
                            await asyncio.sleep(3)
                            
                        except Exception as e:
                            print(f"Error checking discount for {appid}: {e}")
                    
                    print(f"✅ Discount check completed: {notifications_sent} notifications sent")
                    
            except Exception as e:
                print(f"❌ Error in discount notification task: {e}")
        
        return check_discounts
    
    def _create_cleanup_task(self):
        """Создает задачу очистки старых данных"""
        
        @tasks.loop(time=dtime(3, 0))  # Каждый день в 3:00 UTC
        async def cleanup_old_data():
            """
            Очищает старые записи истории цен (сохраняя важные моменты)
            """
            try:
                print(f"[{datetime.utcnow()}] Starting cleanup task...")
                
                # Очищаем историю старше 2 лет
                success = await self.history.cleanup_old_history(days=730)
                
                if success:
                    print("✅ Cleanup completed successfully")
                else:
                    print("⚠️ Cleanup completed with warnings")
                    
            except Exception as e:
                print(f"❌ Error in cleanup task: {e}")
        
        return cleanup_old_data
    
    async def get_popular_games_to_track(self, limit: int = 100) -> List[int]:
        """
        Получает список популярных игр для автоматического отслеживания
        Основано на количестве пользователей, владеющих игрой
        """
        try:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch('''
                    SELECT appid, COUNT(DISTINCT discord_id) as owner_count
                    FROM games
                    WHERE appid IS NOT NULL
                    GROUP BY appid
                    ORDER BY owner_count DESC
                    LIMIT $1
                ''', limit)
                
                return [row['appid'] for row in rows]
                
        except Exception as e:
            print(f"Error fetching popular games: {e}")
            return []
    
    async def update_specific_games(self, appids: List[int]):
        """
        Обновляет цены для конкретного списка игр
        Полезно для принудительного обновления
        """
        print(f"Updating prices for {len(appids)} specific games...")
        
        updated = 0
        for appid in appids:
            try:
                price_data = await steam_price.get_price_info(appid, 'us', use_cache=False)
                
                if not price_data.get('error') and not price_data.get('is_free'):
                    await self.history.save_price_snapshot(
                        appid,
                        'us',
                        price_data.get('price_final', 0),
                        price_data.get('price_initial', 0),
                        price_data.get('discount_percent', 0),
                        price_data.get('currency', 'USD')
                    )
                    updated += 1
                
                await asyncio.sleep(2)
                
            except Exception as e:
                print(f"Error updating {appid}: {e}")
        
        print(f"✅ Updated {updated}/{len(appids)} games")


def create_background_tasks(
    bot: discord.Client, 
    db_pool: asyncpg.Pool,
    history_manager: SteamPriceHistory,
    notification_channel_id: int = None
) -> SteamBackgroundTasks:
    """Фабричная функция для создания менеджера задач"""
    return SteamBackgroundTasks(bot, db_pool, history_manager, notification_channel_id)