import os
import re
import discord
from discord.ext import commands, tasks
from discord import app_commands, ui, Embed, SelectOption
from typing import List
import asyncpg
import aiohttp
import asyncio
from datetime import datetime, timedelta, time as dtime
from bs4 import BeautifulSoup
import psutil
from flask import Flask, jsonify
from threading import Thread

# === Config ===
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
STEAM_API_KEY = os.getenv('STEAM_API_KEY')
DATABASE_URL = os.getenv('DATABASE_URL')
EPIC_API_URL = 'https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions'
DISCOUNT_CHANNEL_ID = int(os.getenv('DISCOUNT_CHANNEL_ID', '0'))
EPIC_CHANNEL_ID = int(os.getenv('EPIC_CHANNEL_ID', '0'))
LOG_CHANNEL_ID = int(os.getenv('LOG_CHANNEL_ID', '0'))
PORT = int(os.getenv('PORT', '10000'))
BIND_TTL_HOURS = int(os.getenv('BIND_TTL_HOURS', '24'))
CACHE_TTL = timedelta(hours=2)

# === Intents ===
INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.presences = True
INTENTS.message_content = True
INTENTS.reactions = True

# === Bot Setup ===
bot = commands.Bot(command_prefix='/', intents=INTENTS)
db_pool: asyncpg.Pool = None

# === Cache ===
steam_cache = {}
ORIGINAL_NICKS = {}
PAGINATION_VIEWS = {}

# === Flask Keep-Alive ===
app = Flask(__name__)

@app.route('/')
def index():
    return jsonify(status='ok')

def run_flask():
    app.run(host='0.0.0.0', port=PORT)

# === Helpers ===
STEAM_URL_REGEX = re.compile(r'^(?:https?://)?steamcommunity\.com/(?:id|profiles)/([\w\-]+)/?$')

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    print("Database pool created")

async def resolve_steamid(identifier: str) -> str | None:
    if identifier.isdigit():
        return identifier
    async with aiohttp.ClientSession() as session:
        async with session.get(
            'https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/',
            params={'key': STEAM_API_KEY, 'vanityurl': identifier}
        ) as resp:
            if resp.ok:
                data = await resp.json()
                return data.get('response', {}).get('steamid')
    return None

async def fetch_owned_games(steamid: str) -> dict:
    now = datetime.utcnow()
    if steamid in steam_cache and now - steam_cache[steamid][0] < CACHE_TTL:
        return steam_cache[steamid][1]
    
    async with aiohttp.ClientSession() as session:
        async with session.get(
            'https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/',
            params={
                'key': STEAM_API_KEY,
                'steamid': steamid,
                'include_appinfo': True,
                'include_played_free_games': True
            }
        ) as resp:
            if resp.ok:
                data = await resp.json()
                games = data.get('response', {}).get('games', [])
                result = {g['appid']: (g['name'], g['playtime_forever'] // 60) for g in games}
                steam_cache[steamid] = (now, result)
                return result
    return {}

def parse_steam_url(url: str) -> str | None:
    m = STEAM_URL_REGEX.match(url)
    return m.group(1) if m else None

# === Database Functions ===
async def get_profile(discord_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            'SELECT * FROM profiles WHERE discord_id = $1', discord_id
        )

async def save_profile(discord_id: int, steam_url: str):
    async with db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO profiles (discord_id, steam_url, last_bound)
            VALUES ($1, $2, NOW())
            ON CONFLICT (discord_id) DO UPDATE SET steam_url = $2, last_bound = NOW()
        ''', discord_id, steam_url)

async def delete_profile(discord_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute('DELETE FROM profiles WHERE discord_id = $1', discord_id)

async def save_games(discord_id: int, games: dict):
    async with db_pool.acquire() as conn:
        await conn.execute('DELETE FROM games WHERE discord_id = $1', discord_id)
        if games:
            await conn.executemany('''
                INSERT INTO games (discord_id, appid, game_name, playtime)
                VALUES ($1, $2, $3, $4)
            ''', [(discord_id, appid, name, hrs) for appid, (name, hrs) in games.items()])

async def get_all_games() -> dict:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch('SELECT discord_id, appid, game_name, playtime FROM games')
        data = {}
        for row in rows:
            uid = row['discord_id']
            data.setdefault(uid, {})[row['appid']] = {'name': row['game_name'], 'hrs': row['playtime']}
        return data

async def get_games_by_name(game_name: str):
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            'SELECT discord_id, playtime FROM games WHERE LOWER(game_name) = LOWER($1)',
            game_name
        )

# === Confirm View ===
class ConfirmView(ui.View):
    def __init__(self, user_id: int, steam_url: str, profile_name: str):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.steam_url = steam_url
        self.profile_name = profile_name

    @ui.button(label='Да', style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message('Это не ваш запрос.', ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        
        await save_profile(self.user_id, self.steam_url)
        
        ident = parse_steam_url(self.steam_url)
        steamid = await resolve_steamid(ident) if ident else None
        games = await fetch_owned_games(steamid) if steamid else {}
        await save_games(self.user_id, games)

        role = discord.utils.get(interaction.guild.roles, name='подвязан стим')
        member = interaction.guild.get_member(self.user_id)
        if role and member:
            try:
                await member.add_roles(role)
            except discord.Forbidden:
                pass

        await interaction.followup.send(f'✅ Профиль `{self.profile_name}` привязан! Загружено {len(games)} игр.', ephemeral=True)
        self.stop()

    @ui.button(label='Нет', style=discord.ButtonStyle.red)
    async def reject(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message('Это не ваш запрос.', ephemeral=True)
        await interaction.response.send_message('❌ Привязка отменена.', ephemeral=True)
        self.stop()

# === Games View ===
class GamesView(ui.View):
    def __init__(self, ctx_user: discord.Member, initial_users: List[discord.Member]):
        super().__init__(timeout=120)
        self.ctx_user = ctx_user
        self.users = initial_users[:6]
        self.pages: List[Embed] = []
        self.page_idx = 0
        self.message = None

    async def _build_pages(self):
        data = await get_all_games()
        sets = [set(data.get(u.id, {})) for u in self.users]
        common = set.intersection(*sets) if sets else set()
        
        sorted_list = sorted(common, key=lambda a: data[self.ctx_user.id][a]['name'].lower())
        
        self.pages.clear()
        per_page = 10
        total = len(sorted_list)
        
        for i in range(0, max(total, 1), per_page):
            chunk = sorted_list[i:i+per_page]
            if chunk:
                desc = "\n".join(
                    f"**{data[self.ctx_user.id][appid]['name']}** — " +
                    " — ".join(f"{u.display_name}: {data[u.id].get(appid, {}).get('hrs', 0)}ч" for u in self.users)
                    for appid in chunk
                )
            else:
                desc = "Нет общих игр."
            
            emb = Embed(title=f"Общие игры ({total})", description=desc)
            emb.add_field(name="Участники", value=", ".join(u.display_name for u in self.users), inline=False)
            emb.set_footer(text=f"Стр. {len(self.pages)+1}/{max((total-1)//per_page+1, 1)}")
            self.pages.append(emb)

    async def render(self, interaction: discord.Interaction):
        await self._build_pages()
        
        if not self.pages:
            return await interaction.response.send_message("Нет общих игр.", ephemeral=True)

        await interaction.response.send_message(embed=self.pages[0], view=self)
        self.message = await interaction.original_response()
        
        if len(self.pages) > 1:
            await self.message.add_reaction("⬅️")
            await self.message.add_reaction("➡️")
        
        PAGINATION_VIEWS[self.message.id] = self

# === Events ===
@bot.event
async def on_ready():
    await init_db()
    print(f'Logged in as {bot.user}')
    
    for guild in bot.guilds:
        bot.tree.clear_commands(guild=guild)
        await bot.tree.sync(guild=guild)
    
    await bot.tree.sync()
    print("Commands synced")
    
    if not daily_link_check.is_running():
        daily_link_check.start()
    if not discount_game_check.is_running():
        discount_game_check.start()
    if not epic_free_check.is_running():
        epic_free_check.start()

@bot.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return
    view = PAGINATION_VIEWS.get(reaction.message.id)
    if not view:
        return
    
    if reaction.emoji == "➡️" and view.page_idx < len(view.pages) - 1:
        view.page_idx += 1
    elif reaction.emoji == "⬅️" and view.page_idx > 0:
        view.page_idx -= 1
    else:
        return
    
    await reaction.message.edit(embed=view.pages[view.page_idx])
    await reaction.message.remove_reaction(reaction.emoji, user)

# === Slash Commands ===
@bot.tree.command(name='привязать_steam')
@app_commands.describe(steam_url='Ссылка на профиль Steam')
async def link_steam(interaction: discord.Interaction, steam_url: str):
    await interaction.response.defer(ephemeral=True)

    profile = await get_profile(interaction.user.id)
    
    if profile and profile['steam_url'] == steam_url:
        return await interaction.followup.send('ℹ️ Вы уже привязали этот профиль.', ephemeral=True)

    if profile and profile['last_bound']:
        if datetime.utcnow() - profile['last_bound'].replace(tzinfo=None) < timedelta(hours=BIND_TTL_HOURS):
            return await interaction.followup.send(f'⏳ Попробуйте снова через {BIND_TTL_HOURS}ч.', ephemeral=True)

    if not STEAM_URL_REGEX.match(steam_url):
        return await interaction.followup.send('❌ Некорректная ссылка.', ephemeral=True)

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(steam_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return await interaction.followup.send('❌ Профиль недоступен.', ephemeral=True)
                html = await r.text()
        except:
            return await interaction.followup.send('❌ Профиль недоступен.', ephemeral=True)

    name_m = re.search(r'<title>(.*?) on Steam</title>', html)
    profile_name = name_m.group(1) if name_m else 'Unknown'
    
    view = ConfirmView(interaction.user.id, steam_url, profile_name)
    await interaction.followup.send(
        embed=Embed(description=f'Подтверждаете привязку профиля **{profile_name}**?'),
        view=view,
        ephemeral=True
    )

@bot.tree.command(name='отвязать_steam')
async def unlink_steam(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    profile = await get_profile(interaction.user.id)
    if not profile:
        return await interaction.followup.send('ℹ️ Профиль не найден.', ephemeral=True)

    await delete_profile(interaction.user.id)
    await interaction.followup.send('✅ Профиль отвязан.', ephemeral=True)

@bot.tree.command(name='найти_тиммейтов')
@app_commands.describe(игра='Название игры')
async def find_teammates(interaction: discord.Interaction, игра: str):
    await interaction.response.defer(ephemeral=True)
    
    rows = await get_games_by_name(игра)
    if not rows:
        return await interaction.followup.send('Никто не играет в эту игру.', ephemeral=True)
    
    mentions = []
    for row in sorted(rows, key=lambda x: x['playtime'], reverse=True):
        member = interaction.guild.get_member(row['discord_id'])
        if member:
            mentions.append(f"{member.mention} ({row['playtime']}ч)")
    
    await interaction.followup.send(', '.join(mentions) if mentions else 'Никто не найден.', ephemeral=True)

@bot.tree.command(name='общие_игры', description='Показать общие игры')
async def common_games(interaction: discord.Interaction, user: discord.Member):
    view = GamesView(interaction.user, [interaction.user, user])
    await view.render(interaction)

# === Tasks ===
@tasks.loop(time=dtime(0, 10))
async def daily_link_check():
    async with db_pool.acquire() as conn:
        profiles = await conn.fetch('SELECT discord_id, steam_url FROM profiles')
    
    for p in profiles:
        ident = parse_steam_url(p['steam_url'])
        if not ident:
            continue
        steamid = await resolve_steamid(ident)
        if steamid:
            games = await fetch_owned_games(steamid)
            await save_games(p['discord_id'], games)
        await asyncio.sleep(1)

@tasks.loop(hours=12)
async def discount_game_check():
    ch = bot.get_channel(DISCOUNT_CHANNEL_ID)
    if not ch:
        return
    
    async with aiohttp.ClientSession() as session:
        async with session.get('https://store.steampowered.com/search/?specials=1&discount=100') as resp:
            if not resp.ok:
                return
            html = await resp.text()
    
    soup = BeautifulSoup(html, 'html.parser')
    
    async with db_pool.acquire() as conn:
        existing = {r['game_link'] for r in await conn.fetch('SELECT game_link FROM sent_sales')}
        
        for item in soup.select('.search_result_row')[:5]:
            pct_elem = item.select_one('.search_discount > span')
            if not pct_elem or pct_elem.text.strip() != '-100%':
                continue
            
            title = item.select_one('.title').text.strip()
            link = item['href'].split('?')[0]
            
            if link in existing:
                continue
            
            await conn.execute(
                'INSERT INTO sent_sales (game_link, discount_end) VALUES ($1, NOW() + interval \'7 days\')',
                link
            )
            await ch.send(f'🔥 100% скидка: [{title}]({link})')

@tasks.loop(hours=24)
async def epic_free_check():
    ch = bot.get_channel(EPIC_CHANNEL_ID)
    if not ch:
        return
    
    async with aiohttp.ClientSession() as session:
        async with session.get(EPIC_API_URL) as resp:
            if not resp.ok:
                return
            data = await resp.json()
    
    offers = data.get('data', {}).get('Catalog', {}).get('searchStore', {}).get('elements', [])
    
    async with db_pool.acquire() as conn:
        existing = {r['game_title'] for r in await conn.fetch('SELECT game_title FROM sent_epic')}
        
        for game in offers:
            title = game.get('title')
            if not title or title in existing:
                continue
            
            promos = game.get('promotions') or {}
            for block in promos.get('promotionalOffers', []):
                for o in block.get('promotionalOffers', []):
                    if o.get('discountSetting', {}).get('discountPercentage') == 0:
                        await conn.execute(
                            'INSERT INTO sent_epic (game_title, offer_end) VALUES ($1, $2) ON CONFLICT DO NOTHING',
                            title, datetime.utcnow() + timedelta(days=7)
                        )
                        slug = game.get('productSlug') or game.get('catalogNs', {}).get('mappings', [{}])[0].get('pageSlug')
                        url = f"https://www.epicgames.com/store/ru/p/{slug}" if slug else ""
                        await ch.send(f'🎁 Бесплатно: [{title}]({url})')

# === Start ===
if __name__ == '__main__':
    Thread(target=run_flask, daemon=True).start()
    bot.run(DISCORD_TOKEN)
