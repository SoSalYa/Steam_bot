import asyncio
import asyncpg
import os

async def test():
    conn = await asyncpg.connect(os.getenv('DATABASE_URL'))
    result = await conn.fetchval('SELECT 1')
    print(f'Подключение успешно! Результат: {result}')
    await conn.close()

asyncio.run(test())
