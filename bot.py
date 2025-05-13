import os
import re
import discord
from discord.ext import commands, tasks
from discord.ui import View, button, select
from discord import app_commands, ui, Embed, Member, SelectOption, Reaction, ButtonStyle
from typing import List
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import requests, asyncio, time, functools
from datetime import datetime, timedelta, time
import base64
import json
from bs4 import BeautifulSoup
import psutil
from flask import Flask, jsonify
from threading import Thread

# Для пагинации
PAGINATION_VIEWS: dict[int, "GamesView"] = {}

# === Config ===
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
STEAM_API_KEY = os.getenv('STEAM_API_KEY')
EPIC_API_URL = 'https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions'
CREDS_B64 = os.getenv('GOOGLE_CREDS_JSON_B64')
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')
BOT_TITLE = os.getenv('BOT_TITLE', 'SteamBotData')
DISCOUNT_CHANNEL_ID = int(os.getenv('DISCOUNT_CHANNEL_ID', '0'))
EPIC_CHANNEL_ID = int(os.getenv('EPIC_CHANNEL_ID', '0'))
LOG_CHANNEL_ID = int(os.getenv('LOG_CHANNEL_ID', '0'))
PREFIX = '/'
PORT = int(os.getenv('PORT', '5000'))
SKIP_BIND_TTL = os.getenv('SKIP_BIND_TTL', 'false').lower() in ['1','true','yes']
BIND_TTL_HOURS = int(os.getenv('BIND_TTL_HOURS', '24'))
CACHE_TTL = timedelta(minutes=30)

# === Intents ===
INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.presences = True
INTENTS.message_content = True

# === Bot Setup ===
bot = commands.Bot(command_prefix=PREFIX, intents=INTENTS)

# === Flask Keep-Alive ===
app = Flask(__name__)
@app.route('/')
def index():
    return jsonify(status='ok')

def run_flask():
    app.run(host='0.0.0.0', port=PORT)

# === Google Sheets Setup ===
SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
REQUIRED_SHEETS = ['Profiles', 'Games', 'SentSales', 'SentEpic']
HEADERS = {
    'Profiles': ['discord_id', 'steam_url', 'last_bound'],
    'Games':    ['discord_id', 'game_name', 'playtime'],
    'SentSales':['game_link', 'discount_end'],
    'SentEpic':['game_title', 'offer_end']
}

def init_gspread_client():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(base64.b64decode(CREDS_B64)), SCOPES
    )
    client = gspread.authorize(creds)
    sh = client.open_by_key(SPREADSHEET_ID) if SPREADSHEET_ID else client.create(BOT_TITLE)
    for title in REQUIRED_SHEETS:
        if title not in [ws.title for ws in sh.worksheets()]:
            sh.add_worksheet(title, rows=1000, cols=20)
    for title, hdr in HEADERS.items():
        ws = sh.worksheet(title)
        if not ws.get_all_values():
            ws.append_row(hdr)
    return sh

# === Helpers ===
STEAM_URL_REGEX = re.compile(r'^(?:https?://)?steamcommunity\.com/(?:id|profiles)/([\w\-]+)/?$')
steam_cache = {}
ORIGINAL_NICKS = {}

def safe_respond(interaction, **kwargs):
    try:
        if not interaction.response.is_done():
            return interaction.response.send_message(**kwargs)
        return interaction.followup.send(**kwargs)
    except discord.NotFound:
        pass

def resolve_steamid(identifier):
    if identifier.isdigit():
        return identifier
    resp = requests.get(
        'https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/',
        params={'key': STEAM_API_KEY, 'vanityurl': identifier}
    )
    return resp.json().get('response', {}).get('steamid') if resp.ok else None

def fetch_owned_games(steamid):
    now = datetime.utcnow()
    if steamid in steam_cache and now - steam_cache[steamid][0] < CACHE_TTL:
        return steam_cache[steamid][1]
    resp = requests.get(
        'https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/',
        params={
            'key': STEAM_API_KEY,
            'steamid': steamid,
            'include_appinfo': True,
            'include_played_free_games': True
        }
    )
    games = resp.json().get('response', {}).get('games', []) if resp.ok else []
    data = {g['name']: g['playtime_forever']//60 for g in games}
    steam_cache[steamid] = (now, data)
    return data

def get_profile_row(ws, discord_id):
    vals = ws.get_all_values()
    for idx, row in enumerate(vals[1:], start=2):
        if row and row[0] == str(discord_id):
            return idx, row
    return None, None

def parse_steam_url(url: str) -> str | None:
    m = STEAM_URL_REGEX.match(url)
    if not m:
        return None
    return resolve_steamid(m.group(1))

class ConfirmView(ui.View):
    def __init__(self, user_id: int, steam_url: str, profile_name: str, sheet):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.steam_url = steam_url
        self.profile_name = profile_name
        self.sheet = sheet

    @ui.button(label='Да', style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message('Это не ваш запрос.', ephemeral=True)

        # --- записываем профиль в Google Sheets ---
        p_ws = self.sheet.worksheet('Profiles')
        idx, row = get_profile_row(p_ws, self.user_id)
        now_iso = datetime.utcnow().isoformat()
        if idx:
            p_ws.update(range_name=f'B{idx}:C{idx}', values=[[self.steam_url, now_iso]])
        else:
            p_ws.append_row([str(self.user_id), self.steam_url, now_iso])

        # --- обновляем Games одним батчем ---
        steamid = parse_steam_url(self.steam_url)
        games = fetch_owned_games(steamid) if steamid else {}
        g_ws = self.sheet.worksheet('Games')
        old = [r for r in g_ws.get_all_values()[1:] if r[0] != str(self.user_id)]
        batch = [HEADERS['Games']] + old + [[str(self.user_id), name, str(hrs)] for name, hrs in games.items()]
        g_ws.clear()
        g_ws.append_rows(batch, value_input_option='USER_ENTERED')

        # --- даём роль и отвечаем ---
        role = discord.utils.get(interaction.guild.roles, name='подвязан стим')
        member = interaction.guild.get_member(self.user_id)
        if role and member:
            try:
                await member.add_roles(role)
            except discord.Forbidden:
                pass

        await interaction.response.send_message(f'✅ Профиль `{self.profile_name}` привязан!', ephemeral=True)
        self.stop()

    @ui.button(label='Нет', style=discord.ButtonStyle.red)
    async def reject(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message('Это не ваш запрос.', ephemeral=True)
        await interaction.response.send_message('❌ Привязка отменена.', ephemeral=True)
        self.stop()






# Константа для времени жизни кэша Google Sheets (в секундах)
CACHE_TTL = 300

# Кэш для тегов игр (Steam)
@functools.lru_cache(maxsize=None)
def get_game_tags(app_id: int):
    """Получаем теги (genres и categories) из Steam API и возвращаем как множество строк."""
    url = f"https://store.steampowered.com/api/appdetails?appids={app_id}"
    try:
        resp = requests.get(url, timeout=5)
        data = resp.json()
        app_data = data.get(str(app_id), {}).get('data', {})
        tags = []
        # Собираем названия жанров
        for genre in app_data.get('genres', []):
            name = genre.get('description')
            if name:
                tags.append(name)
        # И категории
        for cat in app_data.get('categories', []):
            name = cat.get('description')
            if name:
                tags.append(name)
        return set(tags)
    except Exception:
        return set()

# Кэш для данных Google Sheets
_sheets_cache = {"timestamp": 0, "data": None}

def get_sheet_data():
    """Получаем данные из Google Sheets или возвращаем кэшированные (с проверкой CACHE_TTL)."""
    now = time.time()
    if _sheets_cache["data"] is None or (now - _sheets_cache["timestamp"]) > CACHE_TTL:
        # Здесь должен быть код получения данных с помощью gspread или другого API
        data = []  # TODO: заменить на реальный запрос к Google Sheets
        _sheets_cache["data"] = data
        _sheets_cache["timestamp"] = now
    return _sheets_cache["data"]

class GamesView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.participants = []        # список участников (напр. Steam ID или имя)
        self.selected_tags = {"Co-op"}  # по умолчанию фильтр "Co-op"
        self.sort_ascending = True    # направление сортировки
        self.games = []               # текущий список игр после фильтрации
        self.pages = []
        self.current_page = 0

    async def update_pages(self, interaction: discord.Interaction):
        """Пересчёт списка игр, фильтрация, сортировка и генерация страниц."""
        # Получаем свежие данные (например, общие игры участников)
        sheet_data = get_sheet_data()
        # TODO: здесь должна быть логика объединения/фильтрации данных участников по sheet_data
        # Предположим, что из sheet_data мы получаем список (названий или id) игр, общих для участников.
        games_list = []  # TODO: заменить на реальный список игр
        # Фильтруем по выбранным тегам Steam
        filtered = []
        for game in games_list:
            app_id = game.get("appid")  # предполагаем, что в game хранится 'appid'
            tags = get_game_tags(app_id)
            if self.selected_tags.issubset(tags):
                filtered.append(game)
        # Сортируем по имени игры (или любому другому критерию)
        filtered.sort(key=lambda g: g.get("name", ""), reverse=not self.sort_ascending)
        self.games = filtered
        # Разбиваем на страницы (по 10 игр на страницу)
        per_page = 10
        self.pages = [self.games[i:i+per_page] for i in range(0, len(self.games), per_page)]
        self.current_page = 0
        # Отправляем или редактируем сообщение с новой страницей
        await self.render_page(interaction)

    async def render_page(self, interaction: discord.Interaction):
        """Формирует embed для текущей страницы и обновляет сообщение."""
        if not self.pages:
            content = "Нет подходящих игр."
        else:
            page_games = self.pages[self.current_page]
            content = "\n".join(f"- {g.get('name')}" for g in page_games)
        embed = discord.Embed(title="Результаты сравнения игр", description=content)
        embed.set_footer(text=f"Страница {self.current_page+1}/{len(self.pages)}")
        # Если это первая отправка, используем send; иначе edit
        if interaction.response.is_done():
            await interaction.followup.edit_message(interaction.message.id, embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)
        # Обновляем реакции для пагинации
        await self.update_reactions(interaction)

    async def update_reactions(self, interaction: discord.Interaction):
        """Добавляет/удаляет реакции стрелок для навигации."""
        message = interaction.message if interaction.message else await interaction.original_response()
        # Сначала очищаем все реакции
        try:
            await message.clear_reactions()
        except Exception:
            pass
        # Добавляем стрелки по необходимости
        if self.pages and self.current_page > 0:
            await message.add_reaction("⬅️")
        if self.pages and self.current_page < len(self.pages) - 1:
            await message.add_reaction("➡️")

    @button(emoji="➕", style=ButtonStyle.primary)
    async def add_participant(self, button: discord.ui.Button, interaction: discord.Interaction):
        # Логика добавления участника (например, через mention или ID)
        if len(self.participants) >= 6:
            await interaction.response.send_message("Нельзя добавить больше 6 участников.", ephemeral=True)
            return
        # TODO: запросить пользователя для добавления (например, модальное окно или selection)
        # placeholder: просто добавим фиктивного участника
        new_user = "Игрок" + str(len(self.participants)+1)
        self.participants.append(new_user)
        await interaction.response.send_message(f"Участник {new_user} добавлен.", ephemeral=True)
        await self.update_pages(interaction)

    @button(emoji="➖", style=ButtonStyle.danger)
    async def remove_participant(self, button: discord.ui.Button, interaction: discord.Interaction):
        # Логика удаления участника (например, выбор из списка)
        if not self.participants:
            await interaction.response.send_message("Нет участников для удаления.", ephemeral=True)
            return
        # TODO: запросить, кого удалить; placeholder - удалим последнего
        removed = self.participants.pop()
        await interaction.response.send_message(f"Участник {removed} удалён.", ephemeral=True)
        await self.update_pages(interaction)

    @button(emoji="⚙️", style=ButtonStyle.secondary)
    async def filter_menu(self, button: discord.ui.Button, interaction: discord.Interaction):
        # Показываем селект-меню для выбора тегов-фильтров
        # Список опций формируем из уникальных тегов всех игр (или из заранее известных)
        all_tags = {"Co-op", "Single-player", "Multiplayer", "Adventure", "RPG"}  # пример
        options = []
        for tag in sorted(all_tags):
            default = (tag == "Co-op")
            options.append(SelectOption(label=tag, value=tag, default=default))
        select = discord.ui.Select(placeholder="Выберите теги фильтрации",
                                   min_values=1, max_values=len(options), options=options)
        async def select_callback(select_interaction: discord.Interaction):
            self.selected_tags = set(select.values)
            await select_interaction.response.defer()
            await self.update_pages(select_interaction)
        select.callback = select_callback
        view = discord.ui.View()
        view.add_item(select)
        await interaction.response.send_message("Выберите теги для фильтрации:", view=view, ephemeral=True)

    @button(emoji="📝", style=ButtonStyle.secondary)
    async def sort_toggle(self, button: discord.ui.Button, interaction: discord.Interaction):
        # Переключаем сортировку по названию (прямой/обратный) и обновляем
        self.sort_ascending = not self.sort_ascending
        order = "возрастанию" if self.sort_ascending else "убыванию"
        await interaction.response.send_message(f"Сортировка по названию: {order}.", ephemeral=True)
        await self.update_pages(interaction)

    @button(emoji="❌", style=ButtonStyle.danger)
    async def close(self, button: discord.ui.Button, interaction: discord.Interaction):
        # Закрываем View: удаляем реакции и отключаем кнопки
        message = interaction.message if interaction.message else await interaction.original_response()
        try:
            await message.clear_reactions()
        except Exception:
            pass
        self.clear_items()  # отключаем все кнопки
        await interaction.response.edit_message(content="Меню закрыто.", embed=None, view=None)

# Пример использования: в команде или ивенте
# view = GamesView()
# await interaction.response.send_message("Сравнение игр:", view=view)
    
        
    













@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    Thread(target=run_flask, daemon=True).start()
    await bot.tree.sync()
    daily_link_check.start()
    discount_game_check.start()
    epic_free_check.start()
    health_check.start()

@bot.event
async def on_member_join(member):
    try:
        await member.send('Добро пожаловать! `/привязать_steam <ссылка>`')
    except:
        pass

@bot.event
async def on_member_update(before, after):
    prev = {a.name for a in before.activities if isinstance(a, discord.Game)}
    curr = {a.name for a in after.activities if isinstance(a, discord.Game)}
    new_games = curr - prev
    if not new_games:
        orig = ORIGINAL_NICKS.pop(after.id, None)
        if orig:
            try: await after.edit(nick=orig)
            except: pass
        return
    game = new_games.pop()
    sh = init_gspread_client()
    recs = sh.worksheet('Profiles').get_all_records()
    steam_url = next((r['steam_url'] for r in recs if r['discord_id'] == str(after.id)), None)
    if not steam_url: return
    ident = STEAM_URL_REGEX.match(steam_url).group(1)
    sid = ident if ident.isdigit() else resolve_steamid(ident)
    if not sid: return
    owned = fetch_owned_games(sid)
    if game not in owned: return
    ORIGINAL_NICKS[after.id] = before.nick or before.name
    try: await after.edit(nick=f"{ORIGINAL_NICKS[after.id]} | {game}")
    except: pass

# === Slash Commands ===
@bot.tree.command(name='привязать_steam')
@app_commands.describe(steam_url='Ссылка на профиль Steam')
async def link_steam(interaction: discord.Interaction, steam_url: str):
    # 1) Уведомляем Discord, что ответим позже
    await interaction.response.defer(ephemeral=True)

    # 2) Проверяем Google Sheets
    sh = init_gspread_client()
    try:
        p_ws = sh.worksheet('Profiles')
        idx, row = get_profile_row(p_ws, interaction.user.id)
    except gspread.exceptions.APIError:
        return await interaction.followup.send(
            '❗ Google Sheets временно недоступен, попробуйте через минуту.',
            ephemeral=True
        )

    # 3) Уже привязан тот же URL?
    if idx and row[1] == steam_url:
        return await interaction.followup.send(
            'ℹ️ Вы уже привязали этот профиль.',
            ephemeral=True
        )

    # 4) Проверка частой привязки
    if idx and row[2]:
        last = datetime.fromisoformat(row[2])
        if datetime.utcnow() - last < timedelta(hours=BIND_TTL_HOURS) and not SKIP_BIND_TTL:
            sh.worksheet('Blocked').append_row([str(interaction.user.id), 'Частая привязка'])
            return await interaction.followup.send(
                f'⏳ Попробуйте снова через {BIND_TTL_HOURS}ч.',
                ephemeral=True
            )

    # 5) Валидация ссылки
    if not STEAM_URL_REGEX.match(steam_url):
        return await interaction.followup.send(
            '❌ Некорректная ссылка.',
            ephemeral=True
        )

    # 6) Проверка доступности профиля
    try:
        r = requests.get(steam_url, timeout=10)
        r.raise_for_status()
    except:
        return await interaction.followup.send(
            '❌ Профиль недоступен.',
            ephemeral=True
        )

    # 7) Извлекаем имя и предлагаем подтвердить
    name_m = re.search(r'<title>(.*?) on Steam</title>', r.text)
    profile_name = name_m.group(1) if name_m else 'Unknown'
    view = ConfirmView(interaction.user.id, steam_url, profile_name, sh)

    return await interaction.followup.send(
        embed=Embed(description='Подтверждаете привязку профиля?'),
        view=view,
        ephemeral=True
    )
    
@bot.tree.command(name='отвязать_steam')
async def unlink_steam(interaction):
    sh = init_gspread_client()
    pws = sh.worksheet('Profiles')
    idx, _ = get_profile_row(pws, interaction.user.id)
    if not idx:
        return await safe_respond(interaction, content='ℹ️ Профиль не найден.', ephemeral=True)
    vals = pws.get_all_values()
    vals.pop(idx - 1)
    pws.clear()
    pws.append_rows(vals)
    gws = sh.worksheet('Games')
    all_games = gws.get_all_values()
    kept = [r for r in all_games if r[0] != str(interaction.user.id)]
    gws.clear()
    gws.append_rows(kept)
    await safe_respond(interaction, content='✅ Профиль отвязан.', ephemeral=True)

@bot.tree.command(name='найти_тиммейтов')
@app_commands.describe(игра='Название игры')
async def find_teammates(interaction, игра: str):
    await safe_respond(interaction, content='🔄 Поиск...', ephemeral=True)
    recs = init_gspread_client().worksheet('Games').get_all_records()
    matches = [(r['discord_id'], int(r['playtime'])) for r in recs if r['game_name'].lower() == игра.lower()]
    if not matches:
        return await safe_respond(interaction, content='Никто не играет в эту игру.', ephemeral=True)
    mentions = [f"{interaction.guild.get_member(int(uid)).mention} ({hrs}ч)" for uid, hrs in sorted(matches, key=lambda x: x[1], reverse=True) if interaction.guild.get_member(int(uid))]
    await interaction.followup.send(', '.join(mentions), ephemeral=True)

@bot.tree.command(name='общие_игры')
async def common_games(interaction: discord.Interaction, user: discord.Member):
    view = GamesView(interaction.user, [interaction.user, user])
    await view.render(interaction)


@tasks.loop(time=time(0,10))
async def daily_link_check():
    sh = init_gspread_client()
    gws = sh.worksheet('Games')
    vals = [HEADERS['Games']]
    for uid, url, _ in init_gspread_client().worksheet('Profiles').get_all_values()[1:]:
        try:
            requests.get(url, timeout=5).raise_for_status()
        except:
            continue
        ident = STEAM_URL_REGEX.match(url).group(1)
        sid = ident if ident.isdigit() else resolve_steamid(ident)
        if sid:
            for name, hrs in fetch_owned_games(sid).items():
                vals.append([uid, name, str(hrs)])
    gws.clear()
    gws.append_rows(vals, value_input_option='USER_ENTERED')

@tasks.loop(hours=12)
async def discount_game_check():
    sh = init_gspread_client()
    sws = sh.worksheet('SentSales')
    rows = sws.get_all_records()
    now = datetime.utcnow()
    keep = []
    for r in rows:
        try:
            dt = datetime.fromisoformat(r['discount_end'])
        except:
            continue
        if dt > now:
            keep.append([r['game_link'], r['discount_end']])
    vals = [HEADERS['SentSales']] + keep
    sws.clear()
    sws.append_rows(vals, value_input_option='USER_ENTERED')
    resp = requests.get('https://store.steampowered.com/search/?specials=1&discount=100')
    if not resp.ok:
        return
    soup = BeautifulSoup(resp.text, 'html.parser')
    ch = bot.get_channel(DISCOUNT_CHANNEL_ID)
    new = []
    for item in soup.select('.search_result_row')[:5]:
        pct_elem = item.select_one('.search_discount > span')
        pct = pct_elem.text.strip() if pct_elem else ''
        if pct != '-100%':
            continue
        title = item.select_one('.title').text.strip()
        link = item['href'].split('?')[0]
        end_elem = item.select_one('.search_discount_deadline')
        end_text = end_elem['data-enddate'] if end_elem and end_elem.has_attr('data-enddate') else None
        if not end_text or any(x[0] == link for x in keep):
            continue
        new.append([link, end_text])
        if ch:
            await ch.send(f'🔥 100% скидка: [{title}]({link}) до {end_text}')
    if new:
        sws.append_rows(new, value_input_option='USER_ENTERED')

@tasks.loop(hours=24)
async def epic_free_check():
    sh = init_gspread_client()
    ews = sh.worksheet('SentEpic')
    rows = ews.get_all_records()
    now = datetime.utcnow()
    keep = []

    # Сохраняем действующие раздачи
    for r in rows:
        try:
            dt = datetime.fromisoformat(r['offer_end'])
        except:
            continue
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        if dt > now:
            keep.append([r['game_title'], r['offer_end']])

    # Перезаписываем лист только с актуальными
    vals = [HEADERS['SentEpic']] + keep
    ews.clear()
    ews.append_rows(vals, value_input_option='USER_ENTERED')

    # Получаем новые раздачи
    data = requests.get(EPIC_API_URL).json().get('data', {})
    offers = data.get('Catalog', {}) \
                 .get('searchStore', {}) \
                 .get('elements', [])
    ch = bot.get_channel(EPIC_CHANNEL_ID)
    new = []

    for game in offers:
        promos = game.get('promotions') or {}
        for key in ('promotionalOffers', 'upcomingPromotionalOffers'):
            blocks = promos.get(key) or []
            for block in blocks:
                for o in block.get('promotionalOffers', []):
                    ts = o.get('endDate')
                    try:
                        if 'T' in ts:
                            et = datetime.fromisoformat(ts)
                        else:
                            et = datetime.fromtimestamp(int(ts) / 1000)
                    except:
                        continue
                    if et.tzinfo is not None:
                        et = et.replace(tzinfo=None)
                    title = game.get('title')
                    if title in [x[0] for x in keep]:
                        continue
                    if et > now:
                        new.append([title, et.isoformat()])
                        if ch:
                            slug = (
                                game.get('productSlug')
                                or game.get('catalogNs', {})
                                        .get('mappings', [{}])[0]
                                        .get('pageSlug')
                            )
                            url = (
                                f"https://www.epicgames.com/store/ru/p/{slug}"
                                if slug else None
                            )
                            ts_unix = int(et.timestamp())
                            await ch.send(
                                f"🎁 Бесплатно: [{title}]({url}) до <t:{ts_unix}:R>"
                            )

    # Записываем новые раздачи
    if new:
        ews.append_rows(new, value_input_option='USER_ENTERED')
        
@tasks.loop(hours=168)
async def health_check():
    mem = psutil.virtual_memory().percent
    cpu = psutil.cpu_percent()
    ch = bot.get_channel(LOG_CHANNEL_ID)
    if ch:
        await ch.send(f'📊 Память: {mem}%, CPU: {cpu}%')

if __name__ == '__main__':
    bot.run(DISCORD_TOKEN)
