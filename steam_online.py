"""
steam_online.py - Модуль для получения статистики онлайна игроков
Парсит данные из SteamCharts и Steam API
"""

import aiohttp
import asyncio
from bs4 import BeautifulSoup
from typing import Dict, Optional
import re
from datetime import datetime, timedelta

class SteamOnlineStats:
    """Класс для работы со статистикой игроков Steam"""
    
    STEAMCHARTS_URL = "https://steamcharts.com/app/{appid}"
    STEAM_API_URL = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
    
    def __init__(self):
        self.cache = {}
        self.cache_ttl = timedelta(minutes=5)
    
    def _parse_number(self, text: str) -> int:
        """Парсит число из текста, убирая запятые и пробелы"""
        if not text:
            return 0
        cleaned = re.sub(r'[,\s]', '', text.strip())
        try:
            return int(cleaned)
        except ValueError:
            return 0
    
    async def get_current_players_api(self, appid: int) -> Optional[int]:
        """Получает текущее количество игроков через Steam API"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.STEAM_API_URL,
                    params={'appid': appid},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result = data.get('response', {}).get('player_count')
                        return result if result is not None else 0
        except Exception as e:
            print(f"Error fetching current players from API: {e}")
        return None
    
    async def parse_steamcharts(self, appid: int) -> Dict:
        """
        Парсит SteamCharts для получения статистики
        Возвращает: {current, peak_24h, all_time_peak, all_time_peak_date}
        """
        url = self.STEAMCHARTS_URL.format(appid=appid)
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return {'error': f'HTTP {resp.status}'}
                    
                    html = await resp.text()
                    soup = BeautifulSoup(html, 'html.parser')
                    
                    # Парсим статистику из таблицы app-stat
                    stats = {}
                    
                    # Текущий онлайн
                    current_elem = soup.select_one('.app-stat:nth-of-type(1) span.num')
                    if current_elem:
                        stats['current'] = self._parse_number(current_elem.text)
                    
                    # Пик за 24 часа
                    peak_24h_elem = soup.select_one('.app-stat:nth-of-type(2) span.num')
                    if peak_24h_elem:
                        stats['peak_24h'] = self._parse_number(peak_24h_elem.text)
                    
                    # Рекорд за все время
                    all_time_elem = soup.select_one('.app-stat:nth-of-type(3) span.num')
                    if all_time_elem:
                        stats['all_time_peak'] = self._parse_number(all_time_elem.text)
                    
                    # Дата рекорда
                    date_elem = soup.select_one('.app-stat:nth-of-type(3) .time-date')
                    if date_elem:
                        stats['all_time_peak_date'] = date_elem.text.strip()
                    
                    # Если не нашли через селекторы, пробуем альтернативный метод
                    if not stats:
                        stat_divs = soup.select('div.app-stat')
                        for div in stat_divs:
                            num = div.select_one('span.num')
                            if num:
                                value = self._parse_number(num.text)
                                if 'Playing now' in div.text or 'players right now' in div.text.lower():
                                    stats['current'] = value
                                elif '24-hour peak' in div.text or '24h peak' in div.text.lower():
                                    stats['peak_24h'] = value
                                elif 'all-time peak' in div.text.lower():
                                    stats['all_time_peak'] = value
                                    date = div.select_one('.time-date')
                                    if date:
                                        stats['all_time_peak_date'] = date.text.strip()
                    
                    return stats if stats else {'error': 'No data found'}
                    
        except asyncio.TimeoutError:
            return {'error': 'Timeout'}
        except Exception as e:
            print(f"Error parsing SteamCharts for {appid}: {e}")
            return {'error': str(e)}
    
    async def get_online_stats(self, appid: int, use_cache: bool = True) -> Dict:
        """
        Главная функция получения статистики онлайна
        Комбинирует данные из API и SteamCharts
        
        Returns:
            {
                'current': int,
                'peak_24h': int,
                'all_time_peak': int,
                'all_time_peak_date': str,
                'source': str,
                'cached': bool
            }
        """
        # Проверка кеша
        if use_cache and appid in self.cache:
            cached_data, cached_time = self.cache[appid]
            if datetime.utcnow() - cached_time < self.cache_ttl:
                cached_data['cached'] = True
                return cached_data
        
        result = {
            'current': 0,
            'peak_24h': 0,
            'all_time_peak': 0,
            'all_time_peak_date': 'Unknown',
            'source': 'unknown',
            'cached': False
        }
        
        # Пробуем получить из SteamCharts (наиболее полные данные)
        charts_data = await self.parse_steamcharts(appid)
        
        if 'error' not in charts_data and charts_data:
            result.update(charts_data)
            result['source'] = 'steamcharts'
            
            # Если в SteamCharts нет текущего онлайна, дополняем из API
            if result['current'] == 0:
                api_current = await self.get_current_players_api(appid)
                if api_current is not None:
                    result['current'] = api_current
                    result['source'] = 'steamcharts+api'
        else:
            # Если SteamCharts недоступен, используем только API
            api_current = await self.get_current_players_api(appid)
            if api_current is not None:
                result['current'] = api_current
                result['source'] = 'steam_api'
            else:
                result['error'] = 'No data available'
        
        # Кешируем результат
        self.cache[appid] = (result.copy(), datetime.utcnow())
        
        return result
    
    def format_number(self, num: int) -> str:
        """Форматирует число с разделителями тысяч"""
        return f"{num:,}".replace(',', ' ')
    
    async def search_game_appid(self, game_name: str) -> Optional[int]:
        """
        Ищет appid игры по названию через Steam Store API
        """
        url = "https://store.steampowered.com/api/storesearch/"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    params={'term': game_name, 'l': 'english', 'cc': 'US'},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        items = data.get('items', [])
                        if items:
                            return items[0].get('id')
        except Exception as e:
            print(f"Error searching for game: {e}")
        
        return None


# Глобальный экземпляр для использования в боте
steam_online = SteamOnlineStats()


async def get_online_stats(appid: int) -> Dict:
    """Wrapper функция для простого использования"""
    return await steam_online.get_online_stats(appid)