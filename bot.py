import os
import re
import discord
from discord.ext import commands, tasks
from discord import app_commands, Embed
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
CACHE_TTL = timedelta(minutes=30)

# === Discord Intents ===
INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.presences = True
INTENTS.message_content = True

# === Flask Keep-Alive ===
app = Flask(__name__)
@app.route('/')
def index(): return jsonify(status='ok')
def run_flask(): app.run(host='0.0.0.0', port=PORT)

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
        if title not in [ws.title for ws in sh.worksheets()]: sh.add_worksheet(title, rows=1000, cols=20)
    for title, hdr in HEADERS.items():
        ws = sh.worksheet(title)
        if not ws.get_all_values(): ws.append_row(hdr)
    return sh

# === Regex & Cache ===
STEAM_REGEX = re.compile(r'^(?:https?://)?steamcommunity\.com/(?:id|profiles)/([\w\-]+)/?$')
steam_cache = {}
ORIGINAL_NICKS = {}

# === Utility: Safe respond ===
def safe_respond(interaction, **kwargs):
    try:
        if not interaction.response.is_done(): return interaction.response.send_message(**kwargs)
        return interaction.followup.send(**kwargs)
    except discord.NotFound:
        return

# === Steam Helpers ===
def resolve_steamid(vanity):
    resp = requests.get(
        'https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/',
        params={'key': STEAM_API_KEY, 'vanityurl': vanity}
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
    data = {g['name']: g['playtime_forever'] // 60 for g in games}
    steam_cache[steamid] = (now, data)
    return data

# === Discord Bot ===
bot = commands.Bot(command_prefix=PREFIX, intents=INTENTS)

@bot.event
async def on_member_join(member):
    try: await member.send('Добро пожаловать! Используйте `/привязать_steam <ссылка>`')
    except: pass

@bot.event
async def on_member_update(before, after):
    before_games = {a.name for a in before.activities if isinstance(a, discord.Game)}
    after_games = {a.name for a in after.activities if isinstance(a, discord.Game)}
    new_games = after_games - before_games
    if not new_games:
        orig = ORIGINAL_NICKS.pop(after.id, None)
        if orig:
            try: await after.edit(nick=orig)
            except: pass
        return
    game = new_games.pop()
    sh = init_gspread_client(); recs = sh.worksheet('Profiles').get_all_records()
    steam_url = next((r['steam_url'] for r in recs if r['discord_id']==str(after.id)), None)
    if not steam_url: return
    m = STEAM_REGEX.match(steam_url); steamid = m.group(1) if m and m.group(1).isdigit() else resolve_steamid(m.group(1))
    if not steamid: return
    games = fetch_owned_games(steamid)
    if game not in games: return
    ORIGINAL_NICKS[after.id] = before.nick or before.name
    try: await after.edit(nick=f"{ORIGINAL_NICKS[after.id]} | {game}")
    except: pass

# === Slash Commands ===
@bot.tree.command(name='привязать_steam')
@app_commands.describe(steam_url='Ссылка на профиль Steam')
async def link_steam(interaction, steam_url: str):
    await safe_respond(interaction, content='🔄 Проверка...', ephemeral=True)
    m = STEAM_REGEX.match(steam_url)
    if not m: return await safe_respond(interaction, content='❌ Некорректная ссылка.', ephemeral=True)
    sh = init_gspread_client(); pws = sh.worksheet('Profiles'); rows = pws.get_all_records()
    uid = str(interaction.user.id); existing = next((r for r in rows if r['discord_id']==uid), None)
    if existing and datetime.utcnow()-datetime.fromisoformat(existing['last_bound'])<timedelta(hours=24):
        return await safe_respond(interaction, content='⏳ Подождите перед повторной привязкой.', ephemeral=True)
    try: requests.get(steam_url, timeout=5).raise_for_status()
    except: return await safe_respond(interaction, content='❌ Профиль недоступен.', ephemeral=True)
    vanity = m.group(1); steamid = vanity if vanity.isdigit() else resolve_steamid(vanity)
    if not steamid: return await safe_respond(interaction, content='❌ Не удалось получить SteamID.', ephemeral=True)
    now_iso = datetime.utcnow().isoformat()
    if existing:
        idx = rows.index(existing)+2; pws.update(f'B{idx}:C{idx}', [[steam_url, now_iso]])
    else: pws.append_row([uid, steam_url, now_iso])
    # Update games sheet
    games = fetch_owned_games(steamid); gws = sh.worksheet('Games')
    old = [r for r in gws.get_all_values()[1:] if r[0]!=uid]
    gws.clear(); gws.append_row(HEADERS['Games'])
    for r in old: gws.append_row(r)
    for name, hrs in games.items(): gws.append_row([uid, name, str(hrs)])
    await safe_respond(interaction, content='✅ Профиль привязан!', ephemeral=True)

@bot.tree.command(name='отвязать_steam')
async def unlink_steam(interaction):
    sh = init_gspread_client(); pws = sh.worksheet('Profiles'); rows = pws.get_all_records()
    uid = str(interaction.user.id); existing = next((r for r in rows if r['discord_id']==uid), None)
    if not existing: return await safe_respond(interaction, content='ℹ️ Профиль не найден.', ephemeral=True)
    rows.remove(existing); pws.clear(); pws.append_row(HEADERS['Profiles'])
    for r in rows: pws.append_row(list(r.values()))
    gws = sh.worksheet('Games'); games = gws.get_all_values()[1:]; kept = [r for r in games if r[0]!=uid]
    gws.clear(); gws.append_row(HEADERS['Games'])
    for r in kept: gws.append_row(r)
    await safe_respond(interaction, content='✅ Профиль отвязан.', ephemeral=True)

@bot.tree.command(name='найти_тиммейтов')
@app_commands.describe(игра='Название игры')
async def find_teammates(interaction, игра: str):
    await safe_respond(interaction, content='🔄 Ищу...', ephemeral=True)
    recs = init_gspread_client().worksheet('Games').get_all_records()
    matches = [(r['discord_id'], int(r['playtime'])) for r in recs if r['game_name'].lower()==игра.lower()]
    if not matches: return await safe_respond(interaction, content='Никто не играет в эту игру.', ephemeral=True)
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
    desc = '\n'.join(f"**{g}** — вы: {h1}ч, {user.display_name}: {h2}ч" for g,h1,h2 in sorted(common, key=lambda x: x[1], reverse=True))
    await interaction.followup.send(embed=Embed(title=f'Общие игры с {user.display_name}', description=desc))

# === Sales & Epic Free Tasks ===
@tasks.loop(hours=12)
async def discount_game_check():
    sh = init_gspread_client(); sws = sh.worksheet('SentSales')
    rows = sws.get_all_records(); now = datetime.utcnow()
    fresh = [r for r in rows if datetime.fromisoformat(r['discount_end']) > now]
    sws.clear(); sws.append_row(HEADERS['SentSales'])
    for r in fresh:
        sws.append_row([r['game_link'], r['discount_end']])
    resp = requests.get('https://store.steampowered.com/search/?specials=1&discount=100')
    if not resp.ok:
        return
    soup = BeautifulSoup(resp.text, 'html.parser'); ch = bot.get_channel(DISCOUNT_CHANNEL_ID)
    for item in soup.select('.search_result_row')[:5]:
        link = item['href'].split('?')[0]
        if any(r['game_link'] == link for r in fresh):
            continue
        title = item.select_one('.title').text.strip()
        end_elem = item.select_one('.search_discount_deadline')
        end_text = end_elem['data-enddate'] if end_elem else 'неизвестно'
        if ch:
            await ch.send(f'🔥 100% скидка: [{title}]({link}) до {end_text}')
        sws.append_row([link, end_text])

@tasks.loop(hours=24)
async def epic_free_check():
    sh = init_gspread_client(); ews = sh.worksheet('SentEpic')
    rows = ews.get_all_records(); now = datetime.utcnow()
    fresh = [r for r in rows if datetime.fromisoformat(r['offer_end']) > now]
    ews.clear(); ews.append_row(HEADERS['SentEpic'])
    for r in fresh:
        ews.append_row([r['game_title'], r['offer_end']])
    data = requests.get(EPIC_API_URL).json().get('data', {})
    offers = data.get('Catalog', {}).get('searchStore', {}).get('elements', [])
    ch = bot.get_channel(EPIC_CHANNEL_ID)
    for game in offers:
        promos = game.get('promotions', {}).get('promotionalOffers') or []
        if not promos:
            continue
        offer = promos[0]['promotionalOffers'][0]
        end = datetime.fromtimestamp(offer['endDate'] / 1000)
        title = game['title']
        if any(r['game_title'] == title for r in fresh):
            continue
        if end > now and ch:
            await ch.send(f'🎁 Бесплатно: {title} до {end.isoformat()}')
            ews.append_row([title, end.isoformat()])

# === Health Check ===
@tasks.loop(hours=168)
async def health_check():
    mem = psutil.virtual_memory().percent; cpu = psutil.cpu_percent()
    ch = bot.get_channel(LOG_CHANNEL_ID)
    if ch:
        await ch.send(f'📊 Память: {mem}%, CPU: {cpu}%')

# === Bot Startup ===
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    Thread(target=run_flask, daemon=True).start()
    discount_game_check.start()
    epic_free_check.start()
    health_check.start()
    try:
        bot.tree.sync()
    except Exception as e:
        print('Sync error', e)

if __name__ == '__main__':
    bot.run(DISCORD_TOKEN)
