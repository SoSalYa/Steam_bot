import os
import re
import discord
from discord.ext import commands, tasks
from discord import app_commands, ui, Embed, Member
from typing import List
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import requests
from datetime import datetime, timedelta, time
import base64
import json
from bs4 import BeautifulSoup
import psutil
from flask import Flask, jsonify
from threading import Thread

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

class GamesView(ui.View):
    def __init__(self, ctx_user: discord.Member, initial_users: List[discord.Member]):
        super().__init__(timeout=120)
        self.ctx_user = ctx_user
        self.users = initial_users[:]     # кто участвует
        self.sort_key = 'alphabet'        # 'alphabet', 'you', 'combined'
        self.sort_asc = True              # True = по возрастанию
        self.filters: set[str] = set()    # текстовые фильтры
        self.message: discord.Message | None = None
        self._build_buttons()

    def _build_buttons(self):
        self.clear_items()
        # все кнопки белые (secondary) и без текста, только эмодзи
        self.add_item(ui.Button(emoji="➕", style=discord.ButtonStyle.secondary, custom_id="add_user"))
        self.add_item(ui.Button(emoji="❌", style=discord.ButtonStyle.secondary, custom_id="remove_user"))
        self.add_item(ui.Button(emoji="📝", style=discord.ButtonStyle.secondary, custom_id="choose_sort"))
        self.add_item(ui.Button(emoji="⚙️", style=discord.ButtonStyle.secondary, custom_id="choose_filters"))
        self.add_item(ui.Button(emoji="✖️", style=discord.ButtonStyle.secondary, custom_id="close"))

    async def render(self, interaction: discord.Interaction):
        # 1) Собираем данные
        records = init_gspread_client().worksheet('Games').get_all_records()
        data: dict[int, dict[str,int]] = {}
        for r in records:
            uid = int(r['discord_id'])
            data.setdefault(uid, {})[r['game_name']] = int(r['playtime'])

        # 2) Общие
        sets = [set(data.get(u.id, {})) for u in self.users]
        common = set.intersection(*sets) if sets else set()

        # 3) Фильтрация по тексту
        if self.filters:
            common = {g for g in common if any(f.lower() in g.lower() for f in self.filters)}

        # 4) Сортировка
        if self.sort_key == 'alphabet':
            sorted_list = sorted(common, reverse=not self.sort_asc)
        elif self.sort_key == 'you':
            me_map = data.get(self.ctx_user.id, {})
            sorted_list = sorted(common, key=lambda g: me_map.get(g,0), reverse=not self.sort_asc)
        else:  # 'combined'
            sorted_list = sorted(
                common,
                key=lambda g: sum(data[u.id].get(g,0) for u in self.users),
                reverse=not self.sort_asc
            )

        # 5) Формируем текст
        lines = []
        for g in sorted_list:
            parts = [f"**{g}**"]
            for u in self.users:
                hrs = data.get(u.id, {}).get(g, 0)
                parts.append(f"{u.display_name}: {hrs}ч")
            lines.append(" — ".join(parts))

        # 6) Собираем Embed
        embed = Embed(
            title=f"Общие игры ({len(sorted_list)})",
            description="\n".join(lines[:20]) or "Нет общих игр."
        )
        arrow = "▲" if self.sort_asc else "▼"
        embed.add_field(name="Сортировка", value=f"{self.sort_key} {arrow}", inline=True)
        embed.add_field(name="Фильтры",     value=", ".join(self.filters) or "все", inline=True)
        embed.add_field(name="Участники",   value=", ".join(u.display_name for u in self.users), inline=False)

        # 7) Отправка или правка
        if self.message is None:
            # первое сообщение — через followup после defer
            self.message = await interaction.followup.send(embed=embed, view=self)
        else:
            # правим уже отправленное
            await self.message.edit(embed=embed, view=self)

    @ui.button(custom_id='add_user', emoji="➕")
    async def on_add_user(self, button: ui.Button, interaction: discord.Interaction):
        options = [
            ui.SelectOption(label=m.display_name, value=str(m.id))
            for m in interaction.guild.members
            if not m.bot and m not in self.users
        ]
        select = ui.Select(placeholder="Кого добавить?", options=options, custom_id='sel_add')
        async def sel_add_cb(sel: ui.Select, sel_int: discord.Interaction):
            member = interaction.guild.get_member(int(sel.values[0]))
            if member: self.users.append(member)
            await self.render(sel_int)
        select.callback = sel_add_cb
        await interaction.response.send_message("Выберите для добавления:", view=ui.View(select), ephemeral=True)

    @ui.button(custom_id='remove_user', emoji="❌")
    async def on_remove_user(self, button: ui.Button, interaction: discord.Interaction):
        if len(self.users) <= 1:
            return await interaction.response.send_message("Нельзя убрать — останется 0!", ephemeral=True)
        options = [ui.SelectOption(label=u.display_name, value=str(u.id)) for u in self.users]
        select = ui.Select(placeholder="Кого убрать?", options=options, custom_id='sel_rem')
        async def sel_rem_cb(sel: ui.Select, sel_int: discord.Interaction):
            uid = int(sel.values[0])
            self.users = [u for u in self.users if u.id != uid]
            await self.render(sel_int)
        select.callback = sel_rem_cb
        await interaction.response.send_message("Выберите для удаления:", view=ui.View(select), ephemeral=True)

    @ui.button(custom_id='choose_sort', emoji="📝")
    async def on_choose_sort(self, button: ui.Button, interaction: discord.Interaction):
        opts = [
            ui.SelectOption(label="По алфавиту",    value="alphabet"),
            ui.SelectOption(label="По вашим часам", value="you"),
            ui.SelectOption(label="По сумме",       value="combined"),
        ]
        select = ui.Select(placeholder="Сортировка", options=opts, custom_id='sel_sort')
        async def sel_sort_cb(sel: ui.Select, sel_int: discord.Interaction):
            self.sort_key = sel.values[0]
            await self.render(sel_int)
        select.callback = sel_sort_cb
        await interaction.response.send_message("Выберите сортировку:", view=ui.View(select), ephemeral=True)

    @ui.button(custom_id='choose_filters', emoji="⚙️")
    async def on_choose_filters(self, button: ui.Button, interaction: discord.Interaction):
        # ваши фильтры — расширяйте при необходимости
        opts = [
            ui.SelectOption(label="Co-op",    value="coop"),
            ui.SelectOption(label="Survival", value="survival"),
            ui.SelectOption(label="Horror",   value="horror"),
        ]
        select = ui.Select(placeholder="Фильтры", options=opts, custom_id='sel_filt',
                           min_values=0, max_values=len(opts))
        async def sel_filt_cb(sel: ui.Select, sel_int: discord.Interaction):
            self.filters = set(sel.values)
            await self.render(sel_int)
        select.callback = sel_filt_cb
        await interaction.response.send_message("Установите фильтры:", view=ui.View(select), ephemeral=True)

    @ui.button(custom_id='close', emoji="✖️")
    async def on_close(self, button: ui.Button, interaction: discord.Interaction):
        await self.message.delete()
        self.stop()



# === Bot Setup ===
bot = commands.Bot(command_prefix=PREFIX, intents=INTENTS)

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
async def common_games(interaction: discord.Interaction, user: Member):
    # бронируем ответ, чтобы можно было потом followup:
    await interaction.response.defer(ephemeral=False)
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
