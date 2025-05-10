import os
import re
import discord
from discord.ext import commands, tasks
from discord import app_commands, ui, Embed
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
CREDS_B64 = os.getenv('GOOGLE_CREDS_JSON_B64')
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')
BOT_TITLE = os.getenv('BOT_TITLE', 'SteamBotData')
DISCOUNT_CHANNEL_ID = int(os.getenv('DISCOUNT_CHANNEL_ID', '0'))
BETA_CHANNEL_ID = int(os.getenv('BETA_CHANNEL_ID', '0'))
LOG_CHANNEL_ID = int(os.getenv('LOG_CHANNEL_ID', '0'))
PREFIX = '/'
PORT = int(os.getenv('PORT', 5000))

# === Discord Intents ===
INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.presences = True
INTENTS.message_content = True

# === Flask App for Keep-Alive ===
app = Flask(__name__)

@app.route('/')
def index():
    return jsonify(status='ok')

def run_flask():
    app.run(host='0.0.0.0', port=PORT)

# === Google Sheets Setup ===
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]
REQUIRED_SHEETS = ['Profiles', 'Games', 'Blocked']
HEADERS = {
    'Profiles': ['discord_id', 'steam_url', 'last_bound'],
    'Games':    ['discord_id', 'game_name', 'playtime'],
    'Blocked':  ['discord_id', 'reason']
}

# === Utilities & Caches ===
STEAM_URL_REGEX = re.compile(r'^(?:https?://)?steamcommunity\.com/(?:id|profiles)/([A-Za-z0-9_\-]+)/?$')
STEAM_GAMES_CACHE = {}
CACHE_TTL = timedelta(minutes=30)
ORIGINAL_NICKNAMES = {}

# === Google Sheets Client ===

def init_gspread_client():
    creds_json = json.loads(base64.b64decode(CREDS_B64))
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, SCOPES)
    client = gspread.authorize(creds)
    if SPREADSHEET_ID:
        sh = client.open_by_key(SPREADSHEET_ID)
    else:
        sh = client.create(BOT_TITLE)
    for title in REQUIRED_SHEETS:
        if title not in [ws.title for ws in sh.worksheets()]:
            sh.add_worksheet(title, rows=1000, cols=20)
    for title, hdr in HEADERS.items():
        ws = sh.worksheet(title)
        if not ws.get_all_values():
            ws.append_row(hdr)
    return sh

# === Steam Helpers ===

def resolve_steamid(identifier: str) -> str | None:
    if identifier.isdigit():
        return identifier
    url = 'https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/'
    resp = requests.get(url, params={'key': STEAM_API_KEY, 'vanityurl': identifier})
    if not resp.ok:
        return None
    return resp.json().get('response', {}).get('steamid')


def parse_steam_url(url: str) -> str | None:
    m = STEAM_URL_REGEX.match(url)
    if not m:
        return None
    return resolve_steamid(m.group(1))


def fetch_owned_games(steamid: str) -> dict:
    url = 'https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/'
    params = {
        'key': STEAM_API_KEY,
        'steamid': steamid,
        'include_appinfo': True,
        'include_played_free_games': True
    }
    resp = requests.get(url, params=params)
    if not resp.ok:
        return {}
    games = resp.json().get('response', {}).get('games', [])
    return {g['name']: round(g['playtime_forever'] / 60) for g in games}


def get_profile_row(ws, discord_id: int):
    rows = ws.get_all_values()[1:]
    for idx, row in enumerate(rows, start=2):
        if row and row[0] == str(discord_id):
            return idx, row
    return None, None

# === Discord Bot Setup ===
bot = commands.Bot(command_prefix=PREFIX, intents=INTENTS)

# === Events ===

@bot.event
async def on_member_join(member: discord.Member):
    try:
        await member.send(
            'Добро пожаловать! Привяжите свой профиль Steam с помощью `/привязать_steam <ссылка>`.'
        )
    except:
        pass


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    before_games = {a.name for a in before.activities if isinstance(a, discord.Game)}
    after_games = {a.name for a in after.activities if isinstance(a, discord.Game)}
    new = after_games - before_games
    if not new:
        orig = ORIGINAL_NICKNAMES.pop(after.id, None)
        if orig:
            try:
                await after.edit(nick=orig)
            except:
                pass
        return
    game = new.pop()
    sh = init_gspread_client()
    profiles = sh.worksheet('Profiles').get_all_records()
    steam_url = next((r['steam_url'] for r in profiles if r['discord_id'] == str(after.id)), None)
    if not steam_url:
        return
    steamid = parse_steam_url(steam_url)
    if not steamid:
        return
    now = datetime.utcnow()
    cache = STEAM_GAMES_CACHE.get(steamid)
    if cache and now - cache[0] < CACHE_TTL:
        games_dict = cache[1]
    else:
        games_dict = fetch_owned_games(steamid)
        STEAM_GAMES_CACHE[steamid] = (now, games_dict)
    if game not in games_dict:
        return
    if after.id not in ORIGINAL_NICKNAMES:
        ORIGINAL_NICKNAMES[after.id] = before.nick or before.name
    new_nick = f"{ORIGINAL_NICKNAMES[after.id]} | {game}"
    try:
        await after.edit(nick=new_nick)
    except:
        pass

# === Confirm View ===

class ConfirmView(ui.View):
    def __init__(self, user_id, steam_url, profile_name, sheet):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.steam_url = steam_url
        self.profile_name = profile_name
        self.sheet = sheet

    @ui.button(label='Да', style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message('Это не ваш запрос.', ephemeral=True)
        sh = self.sheet
        p_ws = sh.worksheet('Profiles')
        idx, row = get_profile_row(p_ws, self.user_id)
        now_iso = datetime.utcnow().isoformat()
        if idx:
            p_ws.update(f'B{idx}', self.steam_url)
            p_ws.update(f'C{idx}', now_iso)
        else:
            p_ws.append_row([str(self.user_id), self.steam_url, now_iso])
        steamid = parse_steam_url(self.steam_url)
        if steamid:
            games = fetch_owned_games(steamid)
            g_ws = sh.worksheet('Games')
            old = [r for r in g_ws.get_all_values()[1:] if r[0] != str(self.user_id)]
            g_ws.clear()
            g_ws.append_row(HEADERS['Games'])
            for r in old:
                g_ws.append_row(r)
            for name, hrs in games.items():
                g_ws.append_row([str(self.user_id), name, str(hrs)])
        role = discord.utils.get(interaction.guild.roles, name='подвязан стим')
        member = interaction.guild.get_member(self.user_id)
        if role and member:
            await member.add_roles(role)
        try:
            await interaction.message.delete()
        except:
            pass
        await interaction.response.send_message(f'✅ Профиль {self.profile_name} привязан!', ephemeral=True)
        self.stop()

    @ui.button(label='Нет', style=discord.ButtonStyle.red)
    async def reject(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message('Это не ваш запрос.', ephemeral=True)
        try:
            await interaction.message.delete()
        except:
            pass
        await interaction.response.send_message('❗ Окей, отправьте новую ссылку командой `/привязать_steam`.', ephemeral=True)
        self.stop()

# === Slash Commands ===

@bot.tree.command(name='привязать_steam', description='Привязать или обновить профиль Steam')
@app_commands.describe(steam_url='Ссылка на ваш профиль Steam')
async def link_steam(interaction: discord.Interaction, steam_url: str):
    await interaction.response.defer(ephemeral=True)
    sh = init_gspread_client()
    p_ws = sh.worksheet('Profiles')
    b_ws = sh.worksheet('Blocked')
    idx, row = get_profile_row(p_ws, interaction.user.id)
    blocked_ids = [r[0] for r in b_ws.get_all_values()[1:]]
    if str(interaction.user.id) in blocked_ids:
        return await interaction.followup.send('❌ Вы заблокированы.', ephemeral=True)
    if idx and row[2] and datetime.utcnow() - datetime.fromisoformat(row[2]) < timedelta(hours=24):
        b_ws.append_row([str(interaction.user.id), 'Частая привязка'])
        return await interaction.followup.send('❌ Попробуйте через 24ч.', ephemeral=True)
    if not STEAM_URL_REGEX.match(steam_url):
        return await interaction.followup.send('❌ Некорректная ссылка.', ephemeral=True)
    try:
        r = requests.get(steam_url, timeout=10)
        r.raise_for_status()
    except:
        return await interaction.followup.send('❌ Профиль недоступен.', ephemeral=True)
    name_m = re.search(r'<title>(.*?) on Steam</title>', r.text)
    pname = name_m.group(1) if name_m else 'Unknown'
    view = ConfirmView(interaction.user.id, steam_url, pname, sh)
    await interaction.followup.send(embed=Embed(title='Подтверждаете профиль?', description=pname), view=view, ephemeral=True)

@bot.tree.command(name='найти_тиммейтов', description='Найти тиммейтов по игре')
@app_commands.describe(игра='Название игры')
async def find_teammates(interaction: discord.Interaction, игра: str):
    await interaction.response.defer(ephemeral=True)
    records = init_gspread_client().worksheet('Games').get_all_records()
    matches = [(r['discord_id'], int(r['playtime'])) for r in records if r['game_name'].lower() == игра.lower()]
    if not matches:
        return await interaction.followup.send('Никто на сервере не играет в эту игру.', ephemeral=True)
    mentions = [f"{interaction.guild.get_member(int(uid)).mention} ({hrs}ч)" for uid, hrs in sorted(matches, key=lambda x: x[1], reverse=True)]
    await interaction.followup.send(', '.join(mentions), ephemeral=True)

@bot.tree.command(name='общие_игры', description='Показать общие игры с пользователем')
@app_commands.describe(user='Пользователь для сравнения')
async def common_games(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer()
    records = init_gspread_client().worksheet('Games').get_all_records()
    data = {}
    for r in records:
        data.setdefault(r['discord_id'], {})[r['game_name']] = int(r['playtime'])
    me, ot = str(interaction.user.id), str(user.id)
    if me not in data or ot not in data:
        return await interaction.followup.send('Нет данных для одного из пользователей.', ephemeral=False)
    common = [(g, data[me][g], data[ot][g]) for g in set(data[me]) & set(data[ot])]
    if not common:
        return await interaction.followup.send('Общие игры не найдены.', ephemeral=False)
    desc = '\n'.join(f"**{g}** — вы: {h1}ч, {user.display_name}: {h2}ч" for g, h1, h2 in sorted(common, key=lambda x: x[1], reverse=True))
    await interaction.followup.send(embed=Embed(title=f'Общие игры с {user.display_name}', description=desc), ephemeral=False)

# === Background Tasks ===

@tasks.loop(time=time(0,10))
async def daily_link_check():
    sh = init_gspread_client()
    profiles = sh.worksheet('Profiles').get_all_values()[1:]
    games_ws = sh.worksheet('Games')
    games_ws.clear()
    games_ws.append_row(HEADERS['Games'])
    for uid, url, _ in profiles:
        try:
            r = requests.get(url, timeout=10)
        except:
            member = bot.get_guild(bot.guilds[0].id).get_member(int(uid))
            if member:
                await member.send('❗ Ошибка проверки. Обновите `/привязать_steam`.')
            continue
        if not r.ok:
            member = bot.get_guild(bot.guilds[0].id).get_member(int(uid))
            if member:
                await member.send('❗ Ваша ссылка Steam недействительна. Обновите `/привязать_steam`.')
            continue
        steamid = parse_steam_url(url)
        if steamid:
            for name, hrs in fetch_owned_games(steamid).items():
                games_ws.append_row([uid, name, str(hrs)])

@tasks.loop(hours=6)
async def discount_game_check():
    r = requests.get('https://store.steampowered.com/search/?specials=1&discount=100')
    if r.ok:
        soup = BeautifulSoup(r.text, 'html.parser')
        ch = bot.get_channel(DISCOUNT_CHANNEL_ID)
        if ch:
            for item in soup.select('.search_result_row')[:5]:
                title = item.select_one('.title').text.strip()
                link = item['href'].split('?')[0]
                await ch.send(f'🔥 100% скидка: [{title}]({link})')

@tasks.loop(hours=12)
async def beta_game_check():
    r = requests.get('https://store.steampowered.com/search/?filter=beta&sort_by=Released_DESC')
    if r.ok:
        soup = BeautifulSoup(r.text, 'html.parser')
        ch = bot.get_channel(BETA_CHANNEL_ID)
        if ch:
            for item in soup.select('.search_result_row')[:5]:
                title = item.select_one('.title').text.strip()
                link = item['href'].split('?')[0]
                await ch.send(f'🧪 Новая бета: [{title}]({link})')

@tasks.loop(time=time(0,10))
async def health_check():
    if datetime.utcnow().weekday() != 0:
        return
    mem = psutil.virtual_memory().percent
    ch = bot.get_channel(LOG_CHANNEL_ID)
    if ch:
        await ch.send(f'📊 Еженедельный отчёт памяти: {mem}%')

# === Bot Startup ===

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    Thread(target=run_flask, daemon=True).start()
    daily_link_check.start()
    discount_game_check.start()
    beta_game_check.start()
    health_check.start()
    try:
        await bot.tree.sync()
        print('Slash commands synced')
    except Exception as e:
        print(f'Error syncing commands: {e}')

if __name__ == '__main__':
    bot.run(DISCORD_TOKEN)
