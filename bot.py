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
VERIFIED_ROLE = "steam verified"

# === Локализация ===
TEXTS = {
    'en': {
        'not_verified': '❌ You need to link your Steam first! Use `/link_steam`',
        'already_linked': 'ℹ️ You already linked this profile.',
        'cooldown': '⏳ Try again in {hours}h.',
        'invalid_url': '❌ Invalid Steam profile URL.',
        'profile_unavailable': '❌ Profile is unavailable.',
        'confirm_link': 'Do you want to link profile **{name}** as **{discord_name}**?',
        'link_success': '✅ Profile `{name}` linked! Loaded {count} games.',
        'link_cancelled': '❌ Linking cancelled.',
        'not_your_request': 'This is not your request.',
        'profile_not_found': 'ℹ️ Profile not found.',
        'unlink_success': '✅ Profile unlinked.',
        'no_players': 'Nobody plays this game.',
        'no_common_games': 'No games found that all players own.',
        'common_games_title': 'Steam Library - Common Games ({count})',
        'participants': 'Players',
        'page': 'Page {current}/{total}',
        'yes': 'Yes',
        'no': 'No',
        'lang_set': '✅ Language set to English',
        'choose_lang': 'Choose server language:',
        'cmd_link_steam': 'link_steam',
        'cmd_link_desc': 'Link your Steam profile',
        'cmd_link_param': 'Steam profile URL',
        'cmd_unlink_steam': 'unlink_steam',
        'cmd_unlink_desc': 'Unlink Steam',
        'cmd_find_teammates': 'find_teammates',
        'cmd_find_desc': 'Find players',
        'cmd_find_param': 'Game name',
        'cmd_common_games': 'common_games',
        'cmd_common_desc': 'Show common games',
        'cmd_common_param': 'User to compare',
        'hours_visible': '✅ Visible',
        'hours_hidden': '👁️ Hidden',
        'sort_alphabetical': '🔤 Alphabetical',
        'sort_total_hours': '📊 By Total Playtime',
        'sort_your_hours': "⭐ By {user}'s Playtime",
    },
    'ru': {
        'not_verified': '❌ Сначала привяжите Steam! Используйте `/привязать_steam`',
        'already_linked': 'ℹ️ Вы уже привязали этот профиль.',
        'cooldown': '⏳ Попробуйте снова через {hours}ч.',
        'invalid_url': '❌ Некорректная ссылка на профиль Steam.',
        'profile_unavailable': '❌ Профиль недоступен.',
        'confirm_link': 'Подтверждаете привязку профиля **{name}** как **{discord_name}**?',
        'link_success': '✅ Профиль `{name}` привязан! Загружено {count} игр.',
        'link_cancelled': '❌ Привязка отменена.',
        'not_your_request': 'Это не ваш запрос.',
        'profile_not_found': 'ℹ️ Профиль не найден.',
        'unlink_success': '✅ Профиль отвязан.',
        'no_players': 'Никто не играет в эту игру.',
        'no_common_games': 'Нет игр, которые есть у всех игроков.',
        'common_games_title': 'Библиотека Steam - Общие игры ({count})',
        'participants': 'Игроки',
        'page': 'Стр. {current}/{total}',
        'yes': 'Да',
        'no': 'Нет',
        'lang_set': '✅ Язык установлен: Русский',
        'choose_lang': 'Выберите язык сервера:',
        'cmd_link_steam': 'привязать_steam',
        'cmd_link_desc': 'Привязать профиль Steam',
        'cmd_link_param': 'Ссылка на профиль Steam',
        'cmd_unlink_steam': 'отвязать_steam',
        'cmd_unlink_desc': 'Отвязать Steam',
        'cmd_find_teammates': 'найти_тиммейтов',
        'cmd_find_desc': 'Найти игроков',
        'cmd_find_param': 'Название игры',
        'cmd_common_games': 'общие_игры',
        'cmd_common_desc': 'Показать общие игры',
        'cmd_common_param': 'Пользователь для сравнения',
        'hours_visible': '✅ Видимо',
        'hours_hidden': '👁️ Скрыто',
        'sort_alphabetical': '🔤 По алфавиту',
        'sort_total_hours': '📊 По общему времени',
        'sort_your_hours': "⭐ По времени {user}",
    },
    'ua': {
        'not_verified': "❌ Спочатку прив'яжіть Steam! Використовуйте `/привязати_steam`",
        'already_linked': "ℹ️ Ви вже прив'язали цей профіль.",
        'cooldown': '⏳ Спробуйте знову через {hours}год.',
        'invalid_url': '❌ Некоректне посилання на профіль Steam.',
        'profile_unavailable': '❌ Профіль недоступний.',
        'confirm_link': "Підтверджуєте прив'язку профілю **{name}** як **{discord_name}**?",
        'link_success': "✅ Профіль `{name}` прив'язано! Завантажено {count} ігор.",
        'link_cancelled': "❌ Прив'язку скасовано.",
        'not_your_request': 'Це не ваш запит.',
        'profile_not_found': 'ℹ️ Профіль не знайдено.',
        'unlink_success': "✅ Профіль відв'язано.",
        'no_players': 'Ніхто не грає в цю гру.',
        'no_common_games': 'Немає ігор, які є у всіх гравців.',
        'common_games_title': 'Бібліотека Steam - Спільні ігри ({count})',
        'participants': 'Гравці',
        'page': 'Стор. {current}/{total}',
        'yes': 'Так',
        'no': 'Ні',
        'lang_set': '✅ Мову встановлено: Українська',
        'choose_lang': 'Оберіть мову сервера:',
        'cmd_link_steam': 'привязати_steam',
        'cmd_link_desc': "Прив'язати профіль Steam",
        'cmd_link_param': 'Посилання на профіль Steam',
        'cmd_unlink_steam': 'відвязати_steam',
        'cmd_unlink_desc': "Відв'язати Steam",
        'cmd_find_teammates': 'знайти_тіммейтів',
        'cmd_find_desc': 'Знайти гравців',
        'cmd_find_param': 'Назва гри',
        'cmd_common_games': 'спільні_ігри',
        'cmd_common_desc': 'Показати спільні ігри',
        'cmd_common_param': 'Користувач для порівняння',
        'hours_visible': '✅ Видимо',
        'hours_hidden': '👁️ Приховано',
        'sort_alphabetical': '🔤 За алфавітом',
        'sort_total_hours': '📊 За загальним часом',
        'sort_your_hours': "⭐ За часом {user}",
    }
}

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
PAGINATION_VIEWS = {}
server_langs = {}  # guild_id -> lang

# === Flask Keep-Alive ===
app = Flask(__name__)

@app.route('/')
def index():
    return jsonify(status='ok')

def run_flask():
    app.run(host='0.0.0.0', port=PORT)

# === Helpers ===
STEAM_URL_REGEX = re.compile(r'^(?:https?://)?steamcommunity\.com/(?:id|profiles)/([\w\-]+)/?$')

def t(guild_id: int, key: str, **kwargs) -> str:
    lang = server_langs.get(guild_id, 'en')
    text = TEXTS.get(lang, TEXTS['en']).get(key, key)
    return text.format(**kwargs) if kwargs else text

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    
    async with db_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS server_settings (
                guild_id BIGINT PRIMARY KEY,
                language TEXT DEFAULT 'en'
            )
        ''')
        rows = await conn.fetch('SELECT guild_id, language FROM server_settings')
        for row in rows:
            server_langs[row['guild_id']] = row['language']
    
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
                'include_appinfo': 'true',
                'include_played_free_games': 'true'
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

async def has_verified_role(member: discord.Member) -> bool:
    return any(r.name.lower() == VERIFIED_ROLE.lower() for r in member.roles)

async def ensure_verified_role(guild: discord.Guild) -> discord.Role:
    """Создаёт роль 'steam verified' если её нет"""
    role = discord.utils.get(guild.roles, name=VERIFIED_ROLE)
    if not role:
        try:
            role = await guild.create_role(
                name=VERIFIED_ROLE,
                color=discord.Color.blue(),
                reason="Auto-created by Steam Bot"
            )
            print(f"Created role '{VERIFIED_ROLE}' in guild {guild.name}")
        except discord.Forbidden:
            print(f"Missing permissions to create role in {guild.name}")
    return role

# === Database Functions ===
async def get_profile(discord_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow('SELECT * FROM profiles WHERE discord_id = $1', discord_id)

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

async def set_server_lang(guild_id: int, lang: str):
    server_langs[guild_id] = lang
    async with db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO server_settings (guild_id, language)
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET language = $2
        ''', guild_id, lang)

# === Language Select View ===
class LanguageView(ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=600)  # 10 минут для выбора языка
        self.guild_id = guild_id

    async def on_timeout(self):
        """Вызывается когда истекает timeout"""
        for item in self.children:
            item.disabled = True
        
        try:
            if hasattr(self, 'message') and self.message:
                embed = discord.Embed(
                    title="⏰ Timeout",
                    description="Language selection expired. Use `/set_language` to change it later.",
                    color=0x95a5a6
                )
                await self.message.edit(embed=embed, view=self)
        except:
            pass

    @ui.button(label='🇬🇧 English', style=discord.ButtonStyle.secondary)
    async def english(self, interaction: discord.Interaction, button: ui.Button):
        await set_server_lang(self.guild_id, 'en')
        await interaction.response.send_message(TEXTS['en']['lang_set'], ephemeral=True)
        self.stop()

    @ui.button(label='🇷🇺 Русский', style=discord.ButtonStyle.secondary)
    async def russian(self, interaction: discord.Interaction, button: ui.Button):
        await set_server_lang(self.guild_id, 'ru')
        await interaction.response.send_message(TEXTS['ru']['lang_set'], ephemeral=True)
        self.stop()

    @ui.button(label='🇺🇦 Українська', style=discord.ButtonStyle.secondary)
    async def ukrainian(self, interaction: discord.Interaction, button: ui.Button):
        await set_server_lang(self.guild_id, 'ua')
        await interaction.response.send_message(TEXTS['ua']['lang_set'], ephemeral=True)
        self.stop()

# === Confirm View ===
class ConfirmView(ui.View):
    def __init__(self, user_id: int, steam_url: str, profile_name: str, discord_name: str, guild_id: int):
        super().__init__(timeout=300)  # 5 минут для подтверждения
        self.user_id = user_id
        self.steam_url = steam_url
        self.profile_name = profile_name
        self.discord_name = discord_name
        self.guild_id = guild_id
        
        self.children[0].label = t(guild_id, 'yes')
        self.children[1].label = t(guild_id, 'no')

    async def on_timeout(self):
        """Вызывается когда истекает timeout"""
        # Отключаем все кнопки
        for item in self.children:
            item.disabled = True
        
        # Пытаемся обновить сообщение
        try:
            if hasattr(self, 'message') and self.message:
                embed = discord.Embed(
                    title="⏰ Timeout",
                    description="Confirmation expired. Please use `/link_steam` again.",
                    color=0x95a5a6
                )
                await self.message.edit(embed=embed, view=self)
        except:
            pass

    @ui.button(label='Yes', style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(t(self.guild_id, 'not_your_request'), ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        
        await save_profile(self.user_id, self.steam_url)
        
        ident = parse_steam_url(self.steam_url)
        steamid = await resolve_steamid(ident) if ident else None
        games = await fetch_owned_games(steamid) if steamid else {}
        await save_games(self.user_id, games)

        role = await ensure_verified_role(interaction.guild)
        member = interaction.guild.get_member(self.user_id)
        if role and member:
            try:
                await member.add_roles(role)
            except discord.Forbidden:
                pass

        # Красивый embed для успешной привязки
        success_embed = Embed(
            title="✅ Profile Linked Successfully!",
            description=(
                f"**Steam Profile:** `{self.profile_name}`\n"
                f"**Discord:** `{self.discord_name}`\n\n"
                f"🎮 **Games synced:** `{len(games)}`\n"
                f"🎖️ **Role assigned:** `{role.name if role else 'N/A'}`"
            ),
            color=0x00ff00
        )
        success_embed.add_field(
            name="📊 Next Steps",
            value=(
                "• Use `/common_games` to find games with friends\n"
                "• Use `/find_teammates` to find players for a game\n"
                "• Your games will sync automatically every 24h"
            ),
            inline=False
        )
        success_embed.set_footer(text="Steam Bot • Profile linked")
        success_embed.timestamp = datetime.utcnow()
        
        await interaction.followup.send(embed=success_embed, ephemeral=True)
        self.stop()

    @ui.button(label='No', style=discord.ButtonStyle.red)
    async def reject(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(t(self.guild_id, 'not_your_request'), ephemeral=True)
        await interaction.response.send_message(t(self.guild_id, 'link_cancelled'), ephemeral=True)
        self.stop()

# === Games View ===
class GamesView(ui.View):
    def __init__(self, ctx_user: discord.Member, initial_users: List[discord.Member], guild_id: int):
        super().__init__(timeout=900)  # 15 минут = 900 секунд
        self.ctx_user = ctx_user
        self.users = initial_users[:6]
        self.pages: List[Embed] = []
        self.page_idx = 0
        self.message = None
        self.guild_id = guild_id
        self.show_hours = False  # По умолчанию часы скрыты
        self.sort_mode = 'name'  # 'name', 'total_hours', 'your_hours'
        
        # Добавляем кнопки управления
        self.update_buttons()

    def _get_game_icon_url(self, appid: int) -> str:
        """Получает URL маленькой иконки игры как в библиотеке Steam"""
        return f"https://cdn.cloudflare.steamstatic.com/steamcommunity/public/images/apps/{appid}/{appid}_32x32.jpg"
    
    def _get_game_store_url(self, appid: int) -> str:
        """Получает URL страницы игры в Steam Store"""
        return f"https://store.steampowered.com/app/{appid}"

    def update_buttons(self):
        """Обновляет кнопки в зависимости от состояния"""
        self.clear_items()
        
        # Кнопка "назад"
        prev_btn = ui.Button(
            label="◀️",
            style=discord.ButtonStyle.secondary,
            disabled=(self.page_idx == 0 or len(self.pages) <= 1),
            custom_id="prev"
        )
        prev_btn.callback = self.prev_page_callback
        self.add_item(prev_btn)
        
        # Кнопка переключения отображения часов
        hours_btn = ui.Button(
            label="⏱️ Hours" if not self.show_hours else "⏱️ Hide",
            style=discord.ButtonStyle.primary if self.show_hours else discord.ButtonStyle.secondary,
            custom_id="toggle_hours"
        )
        hours_btn.callback = self.toggle_hours_callback
        self.add_item(hours_btn)
        
        # Кнопка сортировки
        sort_label = {
            'name': '🔤 A-Z',
            'total_hours': '📊 Total',
            'your_hours': '⭐ Yours'
        }
        sort_btn = ui.Button(
            label=sort_label[self.sort_mode],
            style=discord.ButtonStyle.secondary,
            custom_id="sort"
        )
        sort_btn.callback = self.cycle_sort_callback
        self.add_item(sort_btn)
        
        # Кнопка "вперед"
        next_btn = ui.Button(
            label="▶️",
            style=discord.ButtonStyle.secondary,
            disabled=(self.page_idx >= len(self.pages) - 1 or len(self.pages) <= 1),
            custom_id="next"
        )
        next_btn.callback = self.next_page_callback
        self.add_item(next_btn)

    async def prev_page_callback(self, interaction: discord.Interaction):
        """Переход на предыдущую страницу"""
        if interaction.user.id != self.ctx_user.id:
            return await interaction.response.send_message("This is not your request.", ephemeral=True)
        
        if self.page_idx > 0:
            self.page_idx -= 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.pages[self.page_idx], view=self)

    async def next_page_callback(self, interaction: discord.Interaction):
        """Переход на следующую страницу"""
        if interaction.user.id != self.ctx_user.id:
            return await interaction.response.send_message("This is not your request.", ephemeral=True)
        
        if self.page_idx < len(self.pages) - 1:
            self.page_idx += 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.pages[self.page_idx], view=self)

    async def toggle_hours_callback(self, interaction: discord.Interaction):
        """Переключает отображение часов"""
        if interaction.user.id != self.ctx_user.id:
            return await interaction.response.send_message("This is not your request.", ephemeral=True)
        
        self.show_hours = not self.show_hours
        self.update_buttons()
        await self._build_pages()
        await interaction.response.edit_message(embed=self.pages[self.page_idx], view=self)

    async def cycle_sort_callback(self, interaction: discord.Interaction):
        """Циклически меняет режим сортировки"""
        if interaction.user.id != self.ctx_user.id:
            return await interaction.response.send_message("This is not your request.", ephemeral=True)
        
        sort_cycle = ['name', 'total_hours', 'your_hours']
        current_idx = sort_cycle.index(self.sort_mode)
        self.sort_mode = sort_cycle[(current_idx + 1) % len(sort_cycle)]
        
        self.page_idx = 0  # Сбрасываем на первую страницу
        self.update_buttons()
        await self._build_pages()
        await interaction.response.edit_message(embed=self.pages[self.page_idx], view=self)

    async def on_timeout(self):
        """Вызывается когда истекает timeout (15 минут)"""
        try:
            if self.message:
                # Удаляем сообщение полностью
                await self.message.delete()
                
                # Удаляем из кэша
                if self.message.id in PAGINATION_VIEWS:
                    del PAGINATION_VIEWS[self.message.id]
                    
                print(f"Deleted expired games view message {self.message.id}")
        except Exception as e:
            print(f"Error deleting expired message: {e}")

    async def _build_pages(self):
        data = await get_all_games()
        sets = [set(data.get(u.id, {})) for u in self.users]
        common = set.intersection(*sets) if sets else set()
        
        # Сортировка в зависимости от режима
        if self.sort_mode == 'name':
            sorted_list = sorted(common, key=lambda a: data[self.ctx_user.id][a]['name'].lower())
        elif self.sort_mode == 'total_hours':
            sorted_list = sorted(
                common,
                key=lambda a: sum(data[u.id].get(a, {}).get('hrs', 0) for u in self.users),
                reverse=True
            )
        else:  # your_hours
            sorted_list = sorted(
                common,
                key=lambda a: data[self.ctx_user.id].get(a, {}).get('hrs', 0),
                reverse=True
            )
        
        self.pages.clear()
        per_page = 10
        total = len(sorted_list)
        
        for i in range(0, max(total, 1), per_page):
            chunk = sorted_list[i:i+per_page]
            
            if chunk:
                # Формируем description как список игр с иконками
                game_lines = []
                for appid in chunk:
                    game_name = data[self.ctx_user.id][appid]['name']
                    game_url = self._get_game_store_url(appid)
                    
                    # Кликабельное название игры
                    game_link = f"[{game_name}]({game_url})"
                    
                    if self.show_hours:
                        # Показываем часы для всех игроков
                        hours_info = []
                        for u in self.users:
                            hrs = data[u.id].get(appid, {}).get('hrs', 0)
                            hours_info.append(f"**{u.display_name}**: {hrs}h")
                        
                        game_lines.append(f"🎮 {game_link}\n    └ {' • '.join(hours_info)}")
                    else:
                        # Просто название игры
                        game_lines.append(f"🎮 {game_link}")
                
                description = "\n".join(game_lines)
                
                emb = Embed(
                    title=f"📚 {t(self.guild_id, 'common_games_title', count=total)}",
                    description=description,
                    color=0x171a21  # Темный цвет Steam
                )
                
                # Информация об участниках
                participants_text = " • ".join(f"**{u.display_name}**" for u in self.users)
                emb.add_field(
                    name=f"👥 {t(self.guild_id, 'participants')}",
                    value=participants_text,
                    inline=False
                )
                
                # Информация о сортировке
                if self.sort_mode == 'name':
                    sort_text = t(self.guild_id, 'sort_alphabetical')
                elif self.sort_mode == 'total_hours':
                    sort_text = t(self.guild_id, 'sort_total_hours')
                else:
                    sort_text = t(self.guild_id, 'sort_your_hours', user=self.ctx_user.display_name)
                
                emb.add_field(
                    name="📋 Sorting",
                    value=sort_text,
                    inline=True
                )
                
                # Статус отображения часов
                hours_status = t(self.guild_id, 'hours_visible') if self.show_hours else t(self.guild_id, 'hours_hidden')
                emb.add_field(
                    name="⏱️ Playtime",
                    value=hours_status,
                    inline=True
                )
                
                page_num = len(self.pages) + 1
                total_pages = max((total - 1) // per_page + 1, 1)
                
                emb.set_footer(
                    text=f"{t(self.guild_id, 'page', current=page_num, total=total_pages)} • Expires in 15min",
                )
                emb.timestamp = datetime.utcnow()
                
            else:
                # Страница "нет общих игр"
                emb = Embed(
                    title=f"📚 {t(self.guild_id, 'common_games_title', count=0)}",
                    description=f"😔 {t(self.guild_id, 'no_common_games')}\n\n*Try linking more games or playing together!*",
                    color=0x5c7e8b
                )
            
            self.pages.append(emb)

    async def render(self, interaction: discord.Interaction):
        await self._build_pages()
        
        if not self.pages:
            return await interaction.response.send_message(t(self.guild_id, 'no_common_games'), ephemeral=True)

        self.update_buttons()  # Обновляем состояние кнопок перед показом
        await interaction.response.send_message(embed=self.pages[0], view=self)
        self.message = await interaction.original_response()
        
        PAGINATION_VIEWS[self.message.id] = self

# === Events ===
@bot.event
async def on_ready():
    await init_db()
    print(f'Logged in as {bot.user}')
    
    # Регистрируем команды для каждого языка на каждом сервере
    for guild in bot.guilds:
        bot.tree.clear_commands(guild=guild)
        lang = server_langs.get(guild.id, 'en')
        await register_commands_for_guild(guild, lang)
    
    await bot.tree.sync()
    print("Commands synced")
    
    if not daily_link_check.is_running():
        daily_link_check.start()
    if not discount_game_check.is_running():
        discount_game_check.start()
    if not epic_free_check.is_running():
        epic_free_check.start()
    if not cleanup_old_views.is_running():
        cleanup_old_views.start()

@bot.event
async def on_guild_join(guild: discord.Guild):
    """Отправляем владельцу выбор языка при добавлении бота"""
    try:
        embed = Embed(
            title="🎮 Steam Bot",
            description="Thanks for adding me! Please choose the server language:\n\n"
                        "Спасибо за добавление! Выберите язык сервера:\n\n"
                        "Дякуємо за додавання! Оберіть мову сервера:",
            color=0x1a9fff
        )
        view = LanguageView(guild.id)
        msg = await guild.owner.send(embed=embed, view=view)
        view.message = msg  # Сохраняем ссылку для timeout
    except discord.Forbidden:
        pass

# === Dynamic Command Registration ===
async def register_commands_for_guild(guild: discord.Guild, lang: str):
    """Регистрирует команды на выбранном языке для гильдии"""
    
    # link_steam
    @app_commands.command(name=t(guild.id, 'cmd_link_steam'), description=t(guild.id, 'cmd_link_desc'))
    @app_commands.describe(steam_url=t(guild.id, 'cmd_link_param'))
    async def link_steam_cmd(interaction: discord.Interaction, steam_url: str):
        await link_steam_handler(interaction, steam_url)
    
    # unlink_steam
    @app_commands.command(name=t(guild.id, 'cmd_unlink_steam'), description=t(guild.id, 'cmd_unlink_desc'))
    async def unlink_steam_cmd(interaction: discord.Interaction):
        await unlink_steam_handler(interaction)
    
    # find_teammates
    @app_commands.command(name=t(guild.id, 'cmd_find_teammates'), description=t(guild.id, 'cmd_find_desc'))
    @app_commands.describe(game=t(guild.id, 'cmd_find_param'))
    async def find_teammates_cmd(interaction: discord.Interaction, game: str):
        await find_teammates_handler(interaction, game)
    
    # common_games
    @app_commands.command(name=t(guild.id, 'cmd_common_games'), description=t(guild.id, 'cmd_common_desc'))
    @app_commands.describe(user=t(guild.id, 'cmd_common_param'))
    async def common_games_cmd(interaction: discord.Interaction, user: discord.Member):
        await common_games_handler(interaction, user)
    
    bot.tree.add_command(link_steam_cmd, guild=guild)
    bot.tree.add_command(unlink_steam_cmd, guild=guild)
    bot.tree.add_command(find_teammates_cmd, guild=guild)
    bot.tree.add_command(common_games_cmd, guild=guild)
    
    await bot.tree.sync(guild=guild)

# === Command Handlers ===
async def link_steam_handler(interaction: discord.Interaction, steam_url: str):
    await interaction.response.defer(ephemeral=True)
    gid = interaction.guild_id

    profile = await get_profile(interaction.user.id)
    
    if profile and profile['steam_url'] == steam_url:
        return await interaction.followup.send(t(gid, 'already_linked'), ephemeral=True)

    if profile and profile['last_bound']:
        if datetime.utcnow() - profile['last_bound'].replace(tzinfo=None) < timedelta(hours=BIND_TTL_HOURS):
            return await interaction.followup.send(t(gid, 'cooldown', hours=BIND_TTL_HOURS), ephemeral=True)

    if not STEAM_URL_REGEX.match(steam_url):
        return await interaction.followup.send(t(gid, 'invalid_url'), ephemeral=True)

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(steam_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return await interaction.followup.send(t(gid, 'profile_unavailable'), ephemeral=True)
                html = await r.text()
        except:
            return await interaction.followup.send(t(gid, 'profile_unavailable'), ephemeral=True)

    # Пробуем разные паттерны для извлечения имени
    name_m = re.search(r'<title>Steam Community :: (.*?)</title>', html)
    if not name_m:
        name_m = re.search(r'<span class="actual_persona_name">(.*?)</span>', html)
    if not name_m:
        name_m = re.search(r'"personaname":"(.*?)"', html)
    if not name_m:
        # Ищем в meta тегах
        name_m = re.search(r'<meta property="og:title" content="(.*?)"', html)
    
    profile_name = name_m.group(1) if name_m else interaction.user.display_name
    # Декодируем HTML entities
    profile_name = profile_name.replace('&quot;', '"').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    discord_name = interaction.user.display_name
    
    # Пытаемся получить аватар Steam
    avatar_m = re.search(r'<link rel="image_src" href="(.*?)"', html)
    avatar_url = avatar_m.group(1) if avatar_m else None
    
    # Пытаемся получить steamid для дополнительной информации
    ident = parse_steam_url(steam_url)
    steamid = await resolve_steamid(ident) if ident else None
    
    # Предпросмотр количества игр
    game_count = 0
    if steamid:
        preview_games = await fetch_owned_games(steamid)
        game_count = len(preview_games)
    
    embed = Embed(
        title="🔗 Link Steam Profile",
        description=(
            f"**Steam Profile:** `{profile_name}`\n"
            f"**Discord User:** `{discord_name}`\n\n"
            f"🎮 **Games found:** `{game_count}`\n\n"
            f"*Confirm to link this profile to your Discord account*"
        ),
        color=0x1b2838
    )
    
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    
    embed.add_field(
        name="🔒 Privacy",
        value="Your profile must be **public** to sync games",
        inline=False
    )
    
    embed.set_footer(text=f"Profile: {steam_url[:50]}...")
    embed.timestamp = datetime.utcnow()
    view = ConfirmView(interaction.user.id, steam_url, profile_name, discord_name, gid)
    msg = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    view.message = msg  # Сохраняем ссылку на сообщение для timeout

async def unlink_steam_handler(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    gid = interaction.guild_id
    
    profile = await get_profile(interaction.user.id)
    if not profile:
        embed = Embed(
            title="ℹ️ No Profile Found",
            description="You don't have a Steam profile linked.\n\nUse `/link_steam` to link your profile!",
            color=0x95a5a6
        )
        return await interaction.followup.send(embed=embed, ephemeral=True)

    steam_url = profile['steam_url']
    await delete_profile(interaction.user.id)
    
    role = discord.utils.get(interaction.guild.roles, name=VERIFIED_ROLE)
    if role:
        try:
            await interaction.user.remove_roles(role)
        except:
            pass
    
    # Красивый embed для отвязки
    unlink_embed = Embed(
        title="✅ Profile Unlinked",
        description=(
            f"Your Steam profile has been successfully unlinked.\n\n"
            f"**Previous profile:** `{steam_url[:50]}...`\n"
            f"🎮 **Games removed:** All synced games\n"
            f"🎖️ **Role removed:** `{VERIFIED_ROLE}`"
        ),
        color=0xe74c3c
    )
    unlink_embed.add_field(
        name="💡 Want to link again?",
        value="You can re-link your profile anytime using `/link_steam`",
        inline=False
    )
    unlink_embed.set_footer(text="Steam Bot • Profile unlinked")
    unlink_embed.timestamp = datetime.utcnow()
    
    await interaction.followup.send(embed=unlink_embed, ephemeral=True)

async def find_teammates_handler(interaction: discord.Interaction, game: str):
    gid = interaction.guild_id
    
    if not await has_verified_role(interaction.user):
        return await interaction.response.send_message(t(gid, 'not_verified'), ephemeral=True)
    
    await interaction.response.defer(ephemeral=True)
    
    rows = await get_games_by_name(game)
    if not rows:
        return await interaction.followup.send(t(gid, 'no_players'), ephemeral=True)
    
    # Получаем appid игры для картинки и ссылки
    async with db_pool.acquire() as conn:
        game_info = await conn.fetchrow(
            'SELECT appid FROM games WHERE LOWER(game_name) = LOWER($1) LIMIT 1',
            game
        )
    
    appid = game_info['appid'] if game_info else None
    
    # Формируем список игроков с эмодзи в зависимости от времени игры
    player_list = []
    for idx, row in enumerate(sorted(rows, key=lambda x: x['playtime'], reverse=True), 1):
        member = interaction.guild.get_member(row['discord_id'])
        if member:
            hrs = row['playtime']
            # Ранги по времени игры
            if hrs > 500:
                rank = "🏆"
            elif hrs > 200:
                rank = "💎"
            elif hrs > 100:
                rank = "⭐"
            elif hrs > 50:
                rank = "✨"
            elif hrs > 10:
                rank = "🎯"
            else:
                rank = "🆕"
            
            player_list.append(f"`#{idx}` {rank} {member.mention} **`{hrs}h`**")
    
    # Формируем заголовок с кликабельной ссылкой
    if appid:
        game_url = f"https://store.steampowered.com/app/{appid}"
        title = f"🔍 [**{game}**]({game_url})"
    else:
        title = f"🔍 **{game}**"
    
    embed = Embed(
        title="Find Teammates",
        description=f"{title}\n\n*Found {len(player_list)} player(s)*\n\n" + "\n".join(player_list[:15]),
        color=0x171a21
    )
    
    # Легенда рангов
    embed.add_field(
        name="🏅 Ranks",
        value="🏆 500h+ • 💎 200h+ • ⭐ 100h+ • ✨ 50h+ • 🎯 10h+ • 🆕 <10h",
        inline=False
    )
    
    embed.set_footer(text=f"Requested by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
    embed.timestamp = datetime.utcnow()
    
    if len(player_list) > 15:
        embed.add_field(
            name="ℹ️ Note",
            value=f"Showing top 15 of {len(player_list)} players",
            inline=False
        )
    
    await interaction.followup.send(embed=embed, ephemeral=True)

async def common_games_handler(interaction: discord.Interaction, user: discord.Member):
    gid = interaction.guild_id
    
    if not await has_verified_role(interaction.user):
        return await interaction.response.send_message(t(gid, 'not_verified'), ephemeral=True)
    
    view = GamesView(interaction.user, [interaction.user, user], gid)
    await view.render(interaction)

# === Global Slash Commands ===
@bot.tree.command(name='set_language', description='Set server language (Admin only)')
@app_commands.describe(language='Language / Язык')
@app_commands.choices(language=[
    app_commands.Choice(name='🇬🇧 English', value='en'),
    app_commands.Choice(name='🇷🇺 Русский', value='ru'),
    app_commands.Choice(name='🇺🇦 Українська', value='ua'),
])
@app_commands.default_permissions(administrator=True)
async def set_language(interaction: discord.Interaction, language: str):
    await set_server_lang(interaction.guild_id, language)
    await interaction.response.send_message(TEXTS[language]['lang_set'], ephemeral=True)
    
    # Перерегистрируем команды с новым языком
    bot.tree.clear_commands(guild=interaction.guild)
    await register_commands_for_guild(interaction.guild, language)
    await interaction.followup.send("✅ Commands updated to new language!", ephemeral=True)

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
    
    # URL для игр со скидкой 100%
    url = 'https://store.steampowered.com/search/?maxprice=free&specials=1'
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url) as resp:
            if not resp.ok:
                print(f"Failed to fetch Steam sales: {resp.status}")
                return
            html = await resp.text()
    
    soup = BeautifulSoup(html, 'html.parser')
    
    async with db_pool.acquire() as conn:
        existing = {r['game_link'] for r in await conn.fetch('SELECT game_link FROM sent_sales')}
        
        # Ищем игры в результатах поиска
        for item in soup.select('a.search_result_row')[:15]:
            # Получаем название игры
            title_elem = item.select_one('.title')
            if not title_elem:
                continue
                
            title = title_elem.text.strip()
            link = item.get('href', '').split('?')[0]
            
            if not link or link in existing:
                continue
            
            # Проверяем что игра действительно со скидкой 100%
            discount_pct = item.select_one('.discount_pct')
            original_price = item.select_one('.discount_original_price')
            final_price = item.select_one('.discount_final_price')
            
            # Должна быть скидка и финальная цена "Free"
            if not discount_pct or not final_price:
                continue
                
            discount_text = discount_pct.text.strip()
            final_price_text = final_price.text.strip()
            
            # Проверяем что это -100% и Free
            if '-100%' in discount_text and ('Free' in final_price_text or 'Бесплатно' in final_price_text):
                print(f"Found 100% discount game: {title}")
                
                await conn.execute(
                    'INSERT INTO sent_sales (game_link, discount_end) VALUES ($1, NOW() + interval \'7 days\') ON CONFLICT DO NOTHING',
                    link
                )
                
                embed = Embed(
                    title="🔥 100% OFF - FREE TO KEEP!",
                    description=f"**[{title}]({link})**\n\n💰 Was: {original_price.text.strip() if original_price else 'Paid'}\n✨ Now: **FREE**\n\n⏰ Limited time offer!",
                    color=0xff6b6b
                )
                embed.set_footer(text="Steam 100% Discount")
                
                # Пробуем получить изображение игры
                img = item.select_one('img')
                if img and img.get('src'):
                    embed.set_thumbnail(url=img['src'])
                
                try:
                    await ch.send(embed=embed)
                    await asyncio.sleep(2)  # Задержка между отправками
                except Exception as e:
                    print(f"Error sending discount message: {e}")

@tasks.loop(hours=1)
async def cleanup_old_views():
    """Очищает устаревшие views из кэша"""
    current_time = datetime.utcnow()
    to_remove = []
    
    for msg_id, view in PAGINATION_VIEWS.items():
        # Проверяем, не истек ли таймаут view
        if hasattr(view, 'message') and view.message:
            try:
                # Если view все еще активен, пропускаем
                if not view.is_finished():
                    continue
                to_remove.append(msg_id)
            except:
                to_remove.append(msg_id)
    
    for msg_id in to_remove:
        PAGINATION_VIEWS.pop(msg_id, None)
    
    if to_remove:
        print(f"Cleaned up {len(to_remove)} old pagination views")
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
                        url = f"https://www.epicgames.com/store/p/{slug}" if slug else ""
                        
                        embed = Embed(
                            title="🎁 FREE GAME",
                            description=f"**[{title}]({url})**\n\nFree on Epic Games Store!",
                            color=0x00d4aa
                        )
                        embed.set_footer(text="Epic Games")
                        await ch.send(embed=embed)

# === Start ===
if __name__ == '__main__':
    Thread(target=run_flask, daemon=True).start()
    bot.run(DISCORD_TOKEN)
