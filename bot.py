import os
import re
import discord
from discord.errors import HTTPException
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

# === Кэш данных по играм ===
class _GamesDataCache:
    def __init__(self):
        self.timestamp = datetime.min
        self.data: dict[int, dict] = {}

    def is_fresh(self, ttl: timedelta) -> bool:
        return datetime.utcnow() - self.timestamp < ttl

    def update(self, new_data: dict[int, dict]):
        self.data = new_data
        self.timestamp = datetime.utcnow()

# Инстанс глобального кэша
GAMES_CACHE = _GamesDataCache()

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
SHEETS_CACHE_TTL = 300

# === Intents ===
INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.presences = True
INTENTS.message_content = True

# === Bot Setup ===
bot = commands.Bot(command_prefix='/', intents=INTENTS)

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
    'Games':    ['discord_id', 'appid', 'game_name', 'playtime'],
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
    data = {g['appid']: (g['name'], g['playtime_forever']//60) for g in games}
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
        batch = [HEADERS['Games']] + old + [
    [str(self.user_id), str(appid), name, str(hrs)]
    for appid, (name, hrs) in games.items()
]
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
    if _sheets_cache["data"] is None or (now - _sheets_cache["timestamp"]) > SHEETS_CACHE_TTL:
        # Здесь должен быть код получения данных с помощью gspread или другого API
        data = []  # TODO: заменить на реальный запрос к Google Sheets
        _sheets_cache["data"] = data
        _sheets_cache["timestamp"] = now
    return _sheets_cache["data"]

STEAM_TAGS = [
    "Co-op", "Remote Play Together", "Multiplayer", "Singleplayer",
    "Adventure", "Action", "RPG", "Strategy",
    "Horror", "Survival", "Puzzle", "Simulation",
    "Sports", "Racing", "Platformer", "Shooter",
]


class GamesView(View):
    def __init__(self, ctx_user: discord.Member, initial_users: List[discord.Member]):
        super().__init__(timeout=120)
        self.ctx_user = ctx_user
        self.users = initial_users[:6]
        self.sort_key = 'alphabet'
        self.sort_asc = True
        self.filters = {'co_op'}
        self.pages: List[Embed] = []
        self.page_idx = 0
        self.message: discord.Message | None = None

        for cid, emoji, cb in [
            ('add_user',       '➕', self.on_add_user),
            ('remove_user',    '➖', self.on_remove_user),
            ('choose_sort',    '📝', self.on_choose_sort),
            ('choose_filters', '⚙️', self.on_choose_filters),
            ('close',          '❌', self.on_close),
        ]:
            btn = discord.ui.Button(custom_id=cid, style=discord.ButtonStyle.secondary, emoji=emoji)
            btn.callback = cb
            self.add_item(btn)

    def _fetch_games_data(self):
        now = datetime.utcnow()
        if GAMES_CACHE.is_fresh(CACHE_TTL):
            return GAMES_CACHE.data
        records = init_gspread_client().worksheet('Games').get_all_records()
        data = {}
        for r in records:
            uid = int(r['discord_id'])
            appid = int(r['appid'])
            data.setdefault(uid, {})[appid] = {
                'name': r['game_name'],
                'hrs': int(r['playtime'])
            }
        GAMES_CACHE.update(data)
        return data

    def _needs_rebuild(self):
        state = (
            tuple(u.id for u in self.users),
            tuple(sorted(self.filters)),
            self.sort_key, self.sort_asc
        )
        if state != getattr(self, '_last_state', None):
            self._last_state = state
            return True
        return False

    def _build_pages(self, data):
        sets = [set(data.get(u.id, {})) for u in self.users]
        common = set.intersection(*sets) if sets else set()
        if self.filters:
            filtered = set()
            for appid in common:
                tags = fetch_game_tags(appid)
                if any(f.replace('_',' ') in tags for f in self.filters):
                    filtered.add(appid)
            common = filtered

        if self.sort_key == 'alphabet':
            sorted_list = sorted(
                common,
                key=lambda a: data[self.ctx_user.id][a]['name'].lower(),
                reverse=not self.sort_asc
            )
        elif self.sort_key == 'you':
            me_map = data.get(self.ctx_user.id, {})
            sorted_list = sorted(
                common,
                key=lambda a: me_map.get(a, {}).get('hrs', 0),
                reverse=not self.sort_asc
            )
        else:
            sorted_list = sorted(
                common,
                key=lambda a: sum(data[u.id].get(a, {}).get('hrs',0) for u in self.users),
                reverse=not self.sort_asc
            )

        self.pages.clear()
        per_page = 10
        total = len(sorted_list)
        for i in range(0, total, per_page):
            chunk = sorted_list[i:i+per_page]
            desc = "\n".join(
                "**" + data[self.ctx_user.id][appid]['name'] + "** — " +
                " — ".join(f"{u.display_name}: {data[u.id].get(appid,{}).get('hrs',0)}ч"
                           for u in self.users)
                for appid in chunk
            ) or "Нет общих игр."
            emb = Embed(title=f"Общие игры ({total})", description=desc)
            emb.add_field(name="Сортировка", value=f"{self.sort_key}{' ▲' if self.sort_asc else ' ▼'}", inline=True)
            emb.add_field(name="Фильтры",     value=", ".join(self.filters) or "все", inline=True)
            emb.add_field(name="Участники",   value=", ".join(u.display_name for u in self.users), inline=False)
            emb.set_footer(text=f"Стр. {len(self.pages)+1}/{(total-1)//per_page+1}")
            self.pages.append(emb)

    async def render(self, interaction: discord.Interaction):
        print("[GamesView] render(): start")
        # 1) Получаем данные
        data = self._fetch_games_data()
        print(f"[GamesView] Got data for {len(data)} users")

        # 2) Проверяем, нужно ли перестраивать страницы
        needs = self._needs_rebuild()
        print(f"[GamesView] needs_rebuild = {needs}")
        if needs:
            self._build_pages(data)
            self.page_idx = 0
            print(f"[GamesView] Built {len(self.pages)} pages")

        # 3) Готовим embed
        self.page_idx = max(0, min(self.page_idx, len(self.pages)-1))
        embed = self.pages[self.page_idx]
        print(f"[GamesView] Prepared embed for page {self.page_idx+1}")

        # 4) Отправка
        if self.message is None:
            print("[GamesView] Sending initial message...")
            await interaction.response.send_message(embed=embed, view=self)
            self.message = await interaction.original_response()
            print("[GamesView] Initial message sent, ID =", self.message.id)
            return

        print("[GamesView] Editing existing message...")
        await self.message.edit(embed=embed, view=self)
        print("[GamesView] Message edited")

    async def refresh(self):
        try:
            data = self._fetch_games_data()
            if self._needs_rebuild():
                self._build_pages(data)
                self.page_idx = 0
            self.page_idx = max(0, min(self.page_idx, len(self.pages)-1))
            embed = self.pages[self.page_idx]
            await self.message.edit(embed=embed, view=self)

            has_left = any(r.emoji=="⬅️" for r in self.message.reactions)
            has_right= any(r.emoji=="➡️" for r in self.message.reactions)
            me = self.message.author

            if self.page_idx>0 and not has_left:  await self.message.add_reaction("⬅️")
            if self.page_idx==0 and has_left:    await self.message.remove_reaction("⬅️", me)
            if self.page_idx< len(self.pages)-1 and not has_right: await self.message.add_reaction("➡️")
            if self.page_idx==len(self.pages)-1 and has_right:     await self.message.remove_reaction("➡️", me)
        except Exception as e:
            print(f"[GamesView.refresh] {e}")

    async def on_add_user(self, interaction: discord.Interaction):
        options = [
            SelectOption(label=m.display_name, value=str(m.id))
            for m in interaction.guild.members if not m.bot and m not in self.users
        ][:25]
        select = discord.ui.Select(placeholder="Кого добавить?", options=options)
        async def cb(sel, inter):
            uid = int(sel.values[0])
            member = interaction.guild.get_member(uid)
            if member and len(self.users)<6: self.users.append(member)
            await inter.response.edit_message(view=self)
            await self.refresh()
        select.callback = cb
        self.clear_items()
        self.add_item(select)
        for cid, emoji, cb2 in [
            ('close','❌',self.on_close),
        ]:
            btn=discord.ui.Button(custom_id=cid, style=discord.ButtonStyle.secondary, emoji=emoji)
            btn.callback=cb2
            self.add_item(btn)
        await interaction.response.edit_message(view=self)

    async def on_remove_user(self, interaction: discord.Interaction):
        options=[SelectOption(label=u.display_name,value=str(u.id))for u in self.users][:25]
        select=discord.ui.Select(placeholder="Кого убрать?",options=options)
        async def cb(sel, inter):
            self.users=[u for u in self.users if u.id!=int(sel.values[0])]
            await inter.response.edit_message(view=self)
            await self.refresh()
        select.callback=cb
        self.clear_items(); self.add_item(select)
        btn=discord.ui.Button(custom_id='close',style=discord.ButtonStyle.secondary,emoji='❌')
        btn.callback=self.on_close; self.add_item(btn)
        await interaction.response.edit_message(view=self)

    async def on_choose_sort(self, interaction: discord.Interaction):
        opts=[SelectOption(label="По алфавиту",value="alphabet",default=self.sort_key=="alphabet"),
              SelectOption(label="По вашим часам",value="you",default=self.sort_key=="you"),
              SelectOption(label="По сумме часов",value="combined",default=self.sort_key=="combined")]
        select=discord.ui.Select(placeholder="Сортировка",options=opts)
        async def cb(sel, inter):
            self.sort_key=sel.values[0]
            await inter.response.edit_message(view=self)
            await self.refresh()
        select.callback=cb
        self.clear_items(); self.add_item(select)
        btn=discord.ui.Button(custom_id='close',style=discord.ButtonStyle.secondary,emoji='❌')
        btn.callback=self.on_close; self.add_item(btn)
        await interaction.response.edit_message(view=self)

    async def on_choose_filters(self, interaction: discord.Interaction):
        opts=[]
        for tag in STEAM_TAGS:
            key=tag.lower().replace(" ","_")
            opts.append(SelectOption(label=tag,value=key,default=key in self.filters))
        select=discord.ui.Select(placeholder="Фильтры",options=opts,min_values=0,max_values=len(opts))
        async def cb(sel, inter):
            self.filters=set(sel.values)
            await inter.response.edit_message(view=self)
            await self.refresh()
        select.callback=cb
        self.clear_items(); self.add_item(select)
        btn=discord.ui.Button(custom_id='close',style=discord.ButtonStyle.secondary,emoji='❌')
        btn.callback=self.on_close; self.add_item(btn)
        await interaction.response.edit_message(view=self)

    async def on_close(self, interaction: discord.Interaction):
        await self.message.clear_reactions()
        self.clear_items()
        await self.message.edit(content="Закрыто",embed=None,view=None)
        
         
    

    
    
        
    







@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}, syncing commands…')
    try:
        await bot.tree.sync()                   # синхронизируем все глобальные команды
        print(" • Synced global commands.")
    except Exception as e:
        print(f" ! Failed to sync commands: {e}")
    print("Sync complete.")

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

@bot.tree.command(name='общие_игры', description='Показать общие игры')
async def common_games(interaction: discord.Interaction, user: discord.Member):
    print(f"[DEBUG] common_games called in guild {interaction.guild_id}")
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
            # вот этот цикл должен быть с отступом в 12 пробелов (4 → функции + 8 → if)
            for appid, (name, hrs) in fetch_owned_games(sid).items():
                vals.append([uid, str(appid), name, str(hrs)])
    # и дальше продолжение функции…
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


async def start_bot():
    backoff = 5  # начальная задержка в секундах
    while True:
        try:
            await bot.start(DISCORD_TOKEN)
            return
        except HTTPException as e:
            if e.status == 429:
                retry_after = None
                if hasattr(e.response, 'headers'):
                    retry_after = e.response.headers.get('Retry-After')
                wait = int(retry_after) if retry_after and retry_after.isdigit() else backoff
                print(f"[429] Rate limited, sleeping for {wait}s before retrying...")
                await asyncio.sleep(wait)
                backoff = min(backoff * 2, 60)
                continue
            raise

if __name__ == '__main__':
    # 1) Запускаем Flask-поток прямо при старте, чтобы открыть порт
    Thread(target=run_flask, daemon=True).start()
    # 2) Затем стартуем бота (блокирующий вызов)
    bot.run(DISCORD_TOKEN)
