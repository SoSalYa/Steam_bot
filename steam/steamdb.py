"""
steam/steamdb.py - Парсер SteamDB для получения цен и истории скидок
"""

import aiohttp
from bs4 import BeautifulSoup
from typing import Optional, Dict, List
from datetime import datetime
import re
import logging

logger = logging.getLogger(__name__)


class SteamDBParser:
    """Парсер данных с SteamDB"""
    
    BASE_URL = "https://steamdb.info"
    
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
    
    async def get_price_data(self, app_id: int) -> Dict:
        """
        Получить данные о ценах из SteamDB
        
        Returns:
            {
                'current_price': int (в центах),
                'original_price': int,
                'discount_percent': int,
                'currency': str,
                'lowest_price_ever': int,
                'lowest_price_date': datetime,
                'highest_discount_ever': int,
                'highest_discount_date': datetime,
                'discount_history': List[Dict],
                'success': bool
            }
        """
        
        url = f"{self.BASE_URL}/app/{app_id}/"
        
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logger.warning(f"SteamDB returned {resp.status} for app {app_id}")
                    return {'success': False, 'error': f'HTTP {resp.status}'}
                
                html = await resp.text()
                soup = BeautifulSoup(html, 'lxml')
                
                result = {
                    'success': True,
                    'app_id': app_id,
                    'timestamp': datetime.utcnow()
                }
                
                # Парсим текущую цену
                price_data = await self._parse_current_price(soup)
                result.update(price_data)
                
                # Парсим историю скидок
                discount_history = await self._parse_discount_history(soup)
                result['discount_history'] = discount_history
                
                # Находим лучшие скидки
                if discount_history:
                    best_discount = max(discount_history, key=lambda x: x.get('discount', 0))
                    result['highest_discount_ever'] = best_discount.get('discount', 0)
                    result['highest_discount_date'] = best_discount.get('date')
                    
                    lowest_price_entry = min(discount_history, key=lambda x: x.get('price', float('inf')))
                    result['lowest_price_ever'] = lowest_price_entry.get('price')
                    result['lowest_price_date'] = lowest_price_entry.get('date')
                
                return result
                
        except asyncio.TimeoutError:
            logger.error(f"Timeout fetching SteamDB for app {app_id}")
            return {'success': False, 'error': 'Timeout'}
        except Exception as e:
            logger.error(f"Error parsing SteamDB for app {app_id}: {e}")
            return {'success': False, 'error': str(e)}
    
    async def _parse_current_price(self, soup: BeautifulSoup) -> Dict:
        """Парсинг текущей цены"""
        
        try:
            # Поиск блока с ценой
            price_container = soup.select_one('tr[data-cc="us"] td.price-discount')
            
            if not price_container:
                # Игра может быть бесплатной
                free_marker = soup.find(text=re.compile('Free', re.I))
                if free_marker:
                    return {
                        'is_free': True,
                        'current_price': 0,
                        'original_price': 0,
                        'discount_percent': 0,
                        'currency': 'USD'
                    }
                return {}
            
            # Скидка
            discount_elem = price_container.select_one('.discount')
            discount_percent = 0
            if discount_elem:
                discount_text = discount_elem.text.strip()
                match = re.search(r'-?(\d+)%', discount_text)
                if match:
                    discount_percent = int(match.group(1))
            
            # Текущая цена
            current_price_elem = price_container.select_one('.price-final')
            current_price = 0
            if current_price_elem:
                price_text = current_price_elem.text.strip()
                current_price = self._parse_price_to_cents(price_text)
            
            # Оригинальная цена (если есть скидка)
            original_price = current_price
            if discount_percent > 0:
                original_price_elem = price_container.select_one('.price-original')
                if original_price_elem:
                    original_price = self._parse_price_to_cents(original_price_elem.text.strip())
            
            return {
                'current_price': current_price,
                'original_price': original_price,
                'discount_percent': discount_percent,
                'currency': 'USD',
                'is_free': False
            }
            
        except Exception as e:
            logger.error(f"Error parsing current price: {e}")
            return {}
    
    async def _parse_discount_history(self, soup: BeautifulSoup) -> List[Dict]:
        """Парсинг истории скидок"""
        
        try:
            history = []
            
            # Ищем таблицу с историей
            history_table = soup.select_one('table.table-prices')
            if not history_table:
                return history
            
            rows = history_table.select('tbody tr')
            
            for row in rows:
                try:
                    # Дата
                    date_elem = row.select_one('td[data-time]')
                    if not date_elem:
                        continue
                    
                    timestamp = int(date_elem.get('data-time', 0))
                    date = datetime.fromtimestamp(timestamp) if timestamp else None
                    
                    # Цена
                    price_elem = row.select_one('td.price')
                    if not price_elem:
                        continue
                    
                    price_text = price_elem.text.strip()
                    price = self._parse_price_to_cents(price_text)
                    
                    # Скидка
                    discount_elem = row.select_one('td:nth-of-type(3)')
                    discount = 0
                    if discount_elem:
                        discount_text = discount_elem.text.strip()
                        match = re.search(r'-?(\d+)%', discount_text)
                        if match:
                            discount = int(match.group(1))
                    
                    if price > 0:  # Игнорируем нулевые цены
                        history.append({
                            'date': date,
                            'price': price,
                            'discount': discount
                        })
                
                except Exception as e:
                    logger.debug(f"Error parsing history row: {e}")
                    continue
            
            return history
            
        except Exception as e:
            logger.error(f"Error parsing discount history: {e}")
            return []
    
    def _parse_price_to_cents(self, price_text: str) -> int:
        """Конвертация текста цены в центы"""
        
        try:
            # Удаляем всё кроме цифр и точки/запятой
            cleaned = re.sub(r'[^\d.,]', '', price_text)
            
            # Заменяем запятую на точку
            cleaned = cleaned.replace(',', '.')
            
            if not cleaned:
                return 0
            
            # Конвертируем в float и умножаем на 100
            price_float = float(cleaned)
            return int(price_float * 100)
            
        except Exception as e:
            logger.debug(f"Error parsing price '{price_text}': {e}")
            return 0
    
    async def search_game(self, query: str) -> Optional[int]:
        """
        Поиск игры по названию, возвращает app_id
        """
        
        try:
            search_url = f"{self.BASE_URL}/search/"
            params = {'a': query, 'type': 'app'}
            
            async with self.session.get(search_url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                
                html = await resp.text()
                soup = BeautifulSoup(html, 'lxml')
                
                # Первый результат поиска
                first_result = soup.select_one('a[href*="/app/"]')
                if first_result:
                    href = first_result.get('href', '')
                    match = re.search(r'/app/(\d+)/', href)
                    if match:
                        return int(match.group(1))
                
                return None
                
        except Exception as e:
            logger.error(f"Error searching game '{query}': {e}")
            return None
