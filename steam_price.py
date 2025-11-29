"""
Steam Price Module
Получение информации о ценах через Steam Store API
"""

import aiohttp
from typing import Dict, List, Optional
from datetime import datetime

class SteamPriceAPI:
    """Класс для работы с ценами Steam Store"""
    
    STORE_API_URL = "https://store.steampowered.com/api/appdetails"
    
    # Основные регионы для мониторинга
    REGIONS = {
        'us': {'name': 'United States', 'currency': 'USD', 'flag': '🇺🇸'},
        'eu': {'name': 'Europe', 'currency': 'EUR', 'flag': '🇪🇺'},
        'ru': {'name': 'Russia', 'currency': 'RUB', 'flag': '🇷🇺'},
        'ar': {'name': 'Argentina', 'currency': 'ARS', 'flag': '🇦🇷'},
        'tr': {'name': 'Turkey', 'currency': 'TRY', 'flag': '🇹🇷'},
        'br': {'name': 'Brazil', 'currency': 'BRL', 'flag': '🇧🇷'},
        'uk': {'name': 'United Kingdom', 'currency': 'GBP', 'flag': '🇬🇧'},
    }
    
    def __init__(self):
        self._cache = {}
        self._cache_ttl = 3600  # 1 час для цен
    
    async def get_price_info(self, appid: int, cc: str = 'us') -> Dict:
        """
        Получает информацию о цене для указанного региона
        
        Args:
            appid: Steam App ID
            cc: Country code (us, eu, ru, ar, tr, etc.)
        
        Returns:
            {
                'appid': int,
                'name': str,
                'success': bool,
                'is_free': bool,
                'price_final': int,  # в центах/копейках
                'price_initial': int,
                'discount_percent': int,
                'currency': str,
                'region': str,
                'error': str (если есть)
            }
        """
        cache_key = f"price_{appid}_{cc}"
        if cache_key in self._cache:
            cached_time, cached_data = self._cache[cache_key]
            if (datetime.utcnow() - cached_time).seconds < self._cache_ttl:
                return cached_data
        
        result = {
            'appid': appid,
            'name': None,
            'success': False,
            'is_free': False,
            'price_final': None,
            'price_initial': None,
            'discount_percent': 0,
            'currency': None,
            'region': cc.upper(),
            'error': None
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                params = {
                    'appids': appid,
                    'cc': cc,
                    'filters': 'price_overview'
                }
                
                async with session.get(
                    self.STORE_API_URL,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        result['error'] = f"HTTP {resp.status}"
                        return result
                    
                    data = await resp.json()
                    
                    app_data = data.get(str(appid))
                    if not app_data or not app_data.get('success'):
                        result['error'] = 'Game not found or unavailable'
                        return result
                    
                    game_data = app_data.get('data', {})
                    result['name'] = game_data.get('name', 'Unknown')
                    
                    # Проверяем, бесплатная ли игра
                    if game_data.get('is_free', False):
                        result['is_free'] = True
                        result['success'] = True
                        result['price_final'] = 0
                        result['price_initial'] = 0
                        self._cache[cache_key] = (datetime.utcnow(), result)
                        return result
                    
                    # Получаем информацию о цене
                    price_overview = game_data.get('price_overview')
                    if not price_overview:
                        result['error'] = 'No price data available'
                        return result
                    
                    result['currency'] = price_overview.get('currency', 'USD')
                    result['price_final'] = price_overview.get('final', 0)
                    result['price_initial'] = price_overview.get('initial', 0)
                    result['discount_percent'] = price_overview.get('discount_percent', 0)
                    result['success'] = True
                    
                    # Кешируем
                    self._cache[cache_key] = (datetime.utcnow(), result)
                    
        except Exception as e:
            result['error'] = str(e)
            print(f"Error getting price for {appid} ({cc}): {e}")
        
        return result
    
    async def get_regional_prices(self, appid: int, regions: List[str] = None) -> List[Dict]:
        """
        Получает цены для нескольких регионов
        
        Args:
            appid: Steam App ID
            regions: Список кодов регионов (если None - все основные)
        
        Returns:
            Список словарей с ценами для каждого региона
        """
        if regions is None:
            regions = list(self.REGIONS.keys())
        
        results = []
        for region in regions:
            price_info = await self.get_price_info(appid, region)
            if price_info['success']:
                results.append(price_info)
        
        return results
    
    async def get_best_regional_price(self, appid: int) -> Optional[Dict]:
        """
        Находит самую низкую региональную цену
        
        Returns:
            Информация о регионе с минимальной ценой
        """
        prices = await self.get_regional_prices(appid)
        
        if not prices:
            return None
        
        # Фильтруем бесплатные и сортируем по цене
        paid_prices = [p for p in prices if not p['is_free'] and p['price_final'] > 0]
        
        if not paid_prices:
            return None
        
        # Конвертируем всё в USD для сравнения (упрощенная конвертация)
        # В реальности нужен актуальный курс
        conversion_rates = {
            'USD': 1.0,
            'EUR': 1.1,
            'RUB': 0.011,
            'ARS': 0.001,
            'TRY': 0.037,
            'BRL': 0.2,
            'GBP': 1.27,
        }
        
        def to_usd(price_data):
            rate = conversion_rates.get(price_data['currency'], 1.0)
            return price_data['price_final'] * rate
        
        return min(paid_prices, key=to_usd)
    
    def format_price(self, price_cents: int, currency: str) -> str:
        """Форматирует цену в читаемый вид"""
        if price_cents == 0:
            return "Free"
        
        # Большинство валют используют 2 десятичных знака
        price_units = price_cents / 100
        
        symbols = {
            'USD': '$',
            'EUR': '€',
            'RUB': '₽',
            'ARS': 'ARS$',
            'TRY': '₺',
            'BRL': 'R$',
            'GBP': '£',
        }
        
        symbol = symbols.get(currency, currency + ' ')
        
        # Для рублей и других целочисленных валют
        if currency in ['RUB']:
            return f"{int(price_units)} {symbol}"
        
        return f"{symbol}{price_units:.2f}"
    
    def get_discount_emoji(self, discount_percent: int) -> str:
        """Возвращает эмодзи в зависимости от размера скидки"""
        if discount_percent >= 90:
            return "🔥🔥🔥"
        elif discount_percent >= 75:
            return "🔥🔥"
        elif discount_percent >= 50:
            return "🔥"
        elif discount_percent >= 25:
            return "💰"
        elif discount_percent > 0:
            return "💸"
        return ""


def calculate_savings(price_initial: int, price_final: int, currency: str) -> str:
    """Вычисляет экономию от скидки"""
    if price_initial <= price_final:
        return ""
    
    api = SteamPriceAPI()
    savings = price_initial - price_final
    return f"Save {api.format_price(savings, currency)}"


def compare_regional_prices(prices: List[Dict]) -> Dict:
    """
    Сравнивает региональные цены и находит статистику
    
    Returns:
        {
            'cheapest': Dict,
            'most_expensive': Dict,
            'price_difference_percent': float
        }
    """
    if not prices or len(prices) < 2:
        return {}
    
    paid_prices = [p for p in prices if not p['is_free'] and p['price_final'] > 0]
    
    if not paid_prices:
        return {}
    
    cheapest = min(paid_prices, key=lambda x: x['price_final'])
    most_expensive = max(paid_prices, key=lambda x: x['price_final'])
    
    if cheapest['price_final'] > 0:
        diff_percent = ((most_expensive['price_final'] - cheapest['price_final']) 
                       / cheapest['price_final'] * 100)
    else:
        diff_percent = 0
    
    return {
        'cheapest': cheapest,
        'most_expensive': most_expensive,
        'price_difference_percent': round(diff_percent, 1)
    }