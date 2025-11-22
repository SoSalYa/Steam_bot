import asyncio
import asyncpg
import os

async def test():
    url = os.getenv('DATABASE_URL')
    print(f"Подключаюсь к: {url[:30]}...")  # показываем начало URL для проверки
    
    if not url:
        print("ОШИБКА: DATABASE_URL не установлен!")
        return
    
    try:
        conn = await asyncpg.connect(url)
        result = await conn.fetchval('SELECT 1')
        print(f'Подключение успешно! Результат: {result}')
        await conn.close()
    except Exception as e:
        print(f'Ошибка подключения: {e}')

asyncio.run(test())
