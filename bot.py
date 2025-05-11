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
EPIC_API_URL = 'https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions'
CREDS_B64 = os.getenv('GOOGLE_CREDS_JSON_B64')
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')
BOT_TITLE = os.getenv('BOT_TITLE', 'SteamBotData')
DISCOUNT_CHANNEL_ID = int(os.getenv('DISCOUNT_CHANNEL_ID', '0'))
EPIC_CHANNEL_ID = int(os.getenv('EPIC_CHANNEL_ID', '0'))
LOG_CHANNEL_ID = int(os.getenv('LOG_CHANNEL_ID', '0'))
PREFIX = '/'
PORT = int(os.getenv('PORT', '5000'))
SKIP_BIND_TTL = os.getenv('SKIP_BIND_TTL', 'false').lower() in ['1', 'true', 'yes']
BIND_TTL_HOURS = int(os.getenv('BIND_TTL_HOURS', '24'))
CACHE_TTL = timedelta(minutes=30)

# === Discord Intents ===
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
    'SentSales': ['game_link', 'discount_end'],
    'SentEpic': ['game_title', 'offer_end']
}

def init_gspread_client():
    creds_bytes = base64.b64decode(CREDS_B64)
    creds_json = json.loads(creds_bytes)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, SCOPES)
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

# === Regex & Cache ===
STEAM_URL_REGEX = re.compile(r'^(?:https?://)?steamcommunity\.com/(?:id|profiles)/([\w\-]+)/?$')
steam_cache = {}
ORIGINAL_NICKS = {}

# === Utility: Safe respond ===
def safe_respond(interaction, **kwargs):
    try:
        if not interaction.response.is_done():
            return interaction.response.send_message(**kwargs)
        return interaction.followup.send(**kwargs)
    except discord.NotFound:
        pass

# === Steam API Helpers ===
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
        params={'key': STEAM_API_KEY, 'steamid': steamid,
                'include_appinfo': True, 'include_played_free_games': True}
    )
    games = resp.json().get('response', {}).get('games', []) if resp.ok else []
    data = {g['name']: g['playtime_forever'] // 60 for g in games}
    steam_cache[steamid] = (now, data)
    return data


def get_profile_row(ws, discord_id):
    vals = ws.get_all_values()
    for idx, row in enumerate(vals[1:], start=2):
        if row and row[0] == str(discord_id):
            return idx, row
    return None, None

# === Discord Bot ===
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
    new = curr - prev
    if not new:
        orig = ORIGINAL_NICKS.pop(after.id, None)
        if orig:
            try:
                await after.edit(nick=orig)
            except:
                pass
        return
    game = new.pop()
    sh = init_gspread_client()
    recs = sh.worksheet('Profiles').get_all_records()
    steam_url = next((r['steam_url'] for r in recs if r['discord_id'] == str(after.id)), None)
    if not steam_url:
        return
    ident = STEAM_URL_REGEX.match(steam_url).group(1)
    sid = ident if ident.isdigit() else resolve_steamid(ident)
    if not sid:
        return
    games = fetch_owned_games(sid)
    if game not in games:
        return
    ORIGINAL_NICKS[after.id] = before.nick or before.name
    try:
        await after.edit(nick=f"{ORIGINAL_NICKS[after.id]} | {game}")
    except:
        pass

# === Slash Commands ===
@bot.tree.command(name='привязать_steam')
@app_commands.describe(steam_url='Ссылка на профиль Steam')
async def link_steam(interaction: discord.Interaction, steam_url: str):
    await safe_respond(interaction, content='🔄 Проверка ссылки...', ephemeral=True)
    if not STEAM_URL_REGEX.match(steam_url):
        return await safe_respond(interaction, content='❌ Некорректная ссылка.', ephemeral=True)
    sh = init_gspread_client()
    pws = sh.worksheet('Profiles')
    idx, row = get_profile_row(pws, interaction.user.id)
    # Cooldown
    if idx and row[2] and not SKIP_BIND_TTL:
        last = datetime.fromisoformat(row[2])
        if datetime.utcnow() - last < timedelta(hours=BIND_TTL_HOURS):
            return await safe_respond(interaction, content=f'⏳ Попробуйте через {BIND_TTL_HOURS} часов.', ephemeral=True)
    try:
        requests.get(steam_url, timeout=5).raise_for_status()
    except:
        return await safe_respond(interaction, content='❌ Профиль недоступен.', ephemeral=True)
    ident = STEAM_URL_REGEX.match(steam_url).group(1)
    sid = ident if ident.isdigit() else resolve_steamid(ident)
    if not sid:
        return await safe_respond(interaction, content='❌ Не удалось получить SteamID.', ephemeral=True)
    now_iso = datetime.utcnow().isoformat()
    # Update or append
    if idx:
        pws.update(f'B{idx}:C{idx}', [[steam_url, now_iso]])
    else:
        pws.append_row([str(interaction.user.id), steam_url, now_iso])
    # Update games sheet
    games = fetch_owned_games(sid)
    gws = sh.worksheet('Games')
    old = [r for r in gws.get_all_values()[1:] if r[0] != str(interaction.user.id)]
    gws.clear()
    gws.append_row(HEADERS['Games'])
    for r in old:
        gws.append_row(r)
    for name, hrs in games.items():
        gws.append_row([str(interaction.user.id), name, str(hrs)])
    await safe_respond(interaction, content='✅ Профиль привязан!', ephemeral=True)

@bot.tree.command(name='отвязать_steam')
async def unlink_steam(interaction: discord.Interaction):
    sh = init_gspread_client()
    pws = sh.worksheet('Profiles')
    idx, row = get_profile_row(pws, interaction.user.id)
    if not idx:
        return await safe_respond(interaction, content='ℹ️ Профиль не найден.', ephemeral=True)
    all_vals = pws.get_all_values()
    all_vals.pop(idx - 1)
    pws.clear()
    pws.append_rows(all_vals)
    # Remove games
    gws = sh.worksheet('Games')
    games_vals = gws.get_all_values()
    filtered = [r for r in games_vals if r[0] != str(interaction.user.id)]
    gws.clear()
    gws.append_rows(filtered)
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
@app_commands.describe(user='Пользователь для сравнения')
async def common_games(interaction, user: discord.Member):
    await safe_respond(interaction, content='🔄 Сбор данных...', ephemeral=True)
    recs = init_gspread_client().worksheet('Games').get_all_records()
    data = {}
    for r in recs:
        data.setdefault(r['discord_id'], {})[r['game_name']] = int(r['playtime'])
    me, ot = str(interaction.user.id), str(user.id)
    if me not in data or ot not in data:
        return await safe_respond(interaction, content='❌ Нет данных для одного из пользователей.', ephemeral=True)
    common = [(g, data[me][g], data[ot][g]) for g in set(data[me]) & set(data[ot])]
    if not common:
        return await safe_respond(interaction, content='ℹ️ Общие игры не найдены.', ephemeral=True)
    desc = '\n'.join(f"**{g}** — вы: {h1}ч, {user.display_error}: {h2}ч" for g,h1,h2 in sorted(common, key=lambda x: x[1], reverse=True))
    await interaction.followup.send(embed=Embed(title=f'Общие игры с {user.display_name}', description=desc))

@tasks.loop(time=time(0,10))
async def daily_link_check():
    sh = init_gspread_client()
    gws = sh.worksheet('Games')
    gws.clear()
    gws.append_row(HEADERS['Games'])
    for uid, url, _ in init_gspread_client().worksheet('Profiles').get_all_values()[1:]:
        try:
            requests.get(url, timeout=5).raise_for_status()
        except:
            continue
        ident = STEAM_URL_REGEX.match(url).group(1)
        sid = ident if ident.isdigit() else resolve_steamid(ident)
        if sid:
            for name, hrs in fetch_owned_games(sid).items():
                gws.append_row([uid, name, str(hrs)])

@tasks.loop(hours=12)
async def discount_game_check():
    sh = init_gspread_client()
    sws = sh.worksheet('SentSales')
    rows = sws.get_all_records()
    now = datetime.utcnow()
    fresh = []
    for r in rows:
        try:
            dt = datetime.fromisoformat(r['discount_end'])
        except:
            continue
        if dt > now:
            fresh.append(r)
    sws.clear()
    sws.append_row(HEADERS['SentSales'])
    for r in fresh:
        sws.append_row([r['game_link'], r['discount_end']])
    resp = requests.get('https://store.steampowered.com/search/?specials=1&discount=100')
    if not resp.ok:
        return
    soup = BeautifulSoup(resp.text, 'html.parser')
    ch = bot.get_channel(DISCOUNT_CHANNEL_ID)
    for item in soup.select('.search_result_row')[:5]:
        pct = item.select_one('.discount_pct').text.strip()
        if pct != '-100%':
            continue
        title = item.select_one('.title').text.strip()
        link = item['href'].split('?')[0]
        end_elem = item.select_one('.search_discount_deadline')
        end_text = end_elem['data-enddate'] if end_elem and end_elem.has_attr('data-enddate') else None
        if not end_text:
            continue
        if any(r['game_link'] == link for r in fresh):
            continue
        if ch:
            await ch.send(f'🔥 100% скидка: [{title}]({link}) до {end_text}')
        sws.append_row([link, end_text])

@tasks.loop(hours=24)
async def epic_free_check():
    sh = init_gspread_client()
    ews = sh.worksheet('SentEpic')
    rows = ews.get_all_records()
    now = datetime.utcnow()
    fresh = []
    for r in rows:
        try:
            dt = datetime.fromisoformat(r['offer_end'])
        except:
            continue
        if dt > now:
            fresh.append(r)
    ews.clear()
    ews.append_row(HEADERS['SentEpic'])
    for r in fresh:
        ews.append_row([r['game_title'], r['offer_end']])
    data = requests.get(EPIC_API_URL).json().get('data', {})
    offers = data.get('Catalog', {}).get('searchStore', {}).get('elements', [])
    ch = bot.get_channel(EPIC_CHANNEL_ID)
    for game in offers:
        promos = game.get('promotions') or {}
        for key in ('upcomingPromotionalOffers', 'promotionalOffers'):
            for entry in promos.get(key, []):
                for offer in entry.get('promotionalOffers', []):
                    end_ts = offer.get('endDate')
                    try:
                        end = datetime.fromtimestamp(float(end_ts) / 1000)
                    except:
                        continue
                    title = game.get('title')
                    if any(r['game_title'] == title for r in fresh):
                        continue
                    if end > now and ch:
                        await ch.send(f'🎁 Бесплатно: {title} до {end.isoformat()}')
                        ews.append_row([title, end.isoformat()])

@tasks.loop(hours=168)
async def health_check():
    mem = psutil.virtual_memory().percent
    cpu = psutil.cpu_percent()
    ch = bot.get_channel(LOG_CHANNEL_ID)
    if ch:
        await ch.send(f'📊 Память: {mem}%, CPU: {cpu}%')

if __name__ == '__main__':
    bot.run(DISCORD_TOKEN)
