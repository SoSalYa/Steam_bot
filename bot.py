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
import psutil  # for health monitoring
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
INTENTS = discord.Intents.default()
INTENTS.members = True
PREFIX = '/'
PORT = int(os.getenv('PORT', 5000))

# === Flask App for Keep-Alive ===
app = Flask('')
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
STEAM_GAMES_CACHE: dict[str, tuple[datetime, dict]] = {}
CACHE_TTL = timedelta(minutes=30)
ORIGINAL_NICKNAMES: dict[int, str] = {}

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

def resolve_steamid(identifier: str) -> str | None:
    if identifier.isdigit():
        return identifier
    url = 'https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/'
    r = requests.get(url, params={'key': STEAM_API_KEY, 'vanityurl': identifier})
    if r.ok:
        return r.json().get('response', {}).get('steamid')
    return None

def parse_steam_url(url: str) -> str | None:
    m = STEAM_URL_REGEX.match(url)
    return resolve_steamid(m.group(1)) if m else None

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
    return {g['name']: round(g['playtime_forever']/60) for g in games}

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
            'Добро пожаловать! Привяжите профиль Steam: `/привязать_steam <ссылка>`.'
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
            await after.edit(nick=orig)
        return
    game = new.pop()
    sh = init_gspread_client()
    prof_ws = sh.worksheet('Profiles')
    profiles = prof_ws.get_all_records()
    steam_url = next((r['steam_url'] for r in profiles if r['discord_id']==str(after.id)), None)
    if not steam_url:
        return
    steamid = parse_steam_url(steam_url)
    if not steamid:
        return
    now = datetime.utcnow()
    cache = STEAM_GAMES_CACHE.get(steamid)
    if cache and now - cache[0] < CACHE_TTL:
        games = cache[1]
    else:
        games = fetch_owned_games(steamid)
        STEAM_GAMES_CACHE[steamid] = (now, games)
    if game not in games:
        return
    if after.id not in ORIGINAL_NICKNAMES:
        ORIGINAL_NICKNAMES[after.id] = before.nick or before.name
    new_nick = f"{ORIGINAL_NICKNAMES[after.id]} | {game}"
    await after.edit(nick=new_nick)

# === Slash Commands ===
@bot.tree.command(name='привязать_steam', description='Привязать или обновить профиль Steam')
@app_commands.describe(steam_url='Ссылка на ваш профиль Steam')
async def link_steam(interaction: discord.Interaction, steam_url: str):
    await interaction.response.defer(ephemeral=True)
    sh = init_gspread_client()
    p_ws = sh.worksheet('Profiles')
    b_ws = sh.worksheet('Blocked')
    idx, row = get_profile_row(p_ws, interaction.user.id)
    blocked = [r[0] for r in b_ws.get_all_values()[1:]]
    if str(interaction.user.id) in blocked:
        return await interaction.followup.send('❌ Вы заблокированы.', ephemeral=True)
    if idx and row[2] and datetime.utcnow()-datetime.fromisoformat(row[2])<timedelta(hours=24):
        b_ws.append_row([str(interaction.user.id),'Частая привязка'])
        return await interaction.followup.send('❌ Попробуйте через 24ч.', ephemeral=True)
    if not STEAM_URL_REGEX.match(steam_url):
        return await interaction.followup.send('❌ Некорректная ссылка.', ephemeral=True)
    try:
        r = requests.get(steam_url,timeout=10); r.raise_for_status()
    except:
        return await interaction.followup.send('❌ Профиль недоступен.', ephemeral=True)
    name_m = re.search(r'<title>(.*?) on Steam</title>',r.text)
    pname=name_m.group(1) if name_m else 'Unknown'
    view=ConfirmView(interaction.user.id,steam_url,pname,sh)
    await interaction.followup.send(embed=Embed(title='Подтвердите профиль',description=pname),view=view,ephemeral=True)

@bot.tree.command(name='найти_тиммейтов',description='Найти тиммейтов по игре')
@app_commands.describe(игра='Название игры')
async def find_tm(interaction: discord.Interaction, игра: str):
    await interaction.response.defer(ephemeral=True)
    ws=init_gspread_client().worksheet('Games')
    rec=ws.get_all_records()
    m=[(r['discord_id'],int(r['playtime']))for r in rec if r['game_name'].lower()==игра.lower()]
    if not m: return await interaction.followup.send('Никто не играет.',ephemeral=True)
    text=', '.join(f"{interaction.guild.get_member(int(uid)).mention} ({hrs}ч)" for uid,hrs in sorted(m,key=lambda x:x[1],reverse=True))
    await interaction.followup.send(text,ephemeral=True)

@bot.tree.command(name='общие_игры',description='Показать общие игры')
@app_commands.describe(user='Пользователь')
async def common_g(interaction: discord.Interaction,user:discord.Member):
    await interaction.response.defer()
    rec=init_gspread_client().worksheet('Games').get_all_records()
    d={};
    for r in rec: d.setdefault(r['discord_id'],{})[r['game_name']]=int(r['playtime'])
    me,ot=str(interaction.user.id),str(user.id)
    if me not in d or ot not in d: return await interaction.followup.send('Нет данных.',ephemeral=False)
    cm=[(g,d[me][g],d[ot][g])for g in set(d[me])&set(d[ot])]
    if not cm: return await interaction.followup.send('Общие игры не найдены.',ephemeral=False)
    desc='\n'.join(f"**{g}** — вы: {h1}ч, {user.display_name}: {h2}ч" for g,h1,h2 in sorted(cm,key=lambda x:x[1],reverse=True))
    await interaction.followup.send(embed=Embed(title=f'Общие игры с {user.display_name}',description=desc))

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
        except Exception:
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

@tasks.loop(hours=6)(hours=6)
async def discount_game_check():
    r=requests.get('https://store.steampowered.com/search/?specials=1&discount=100')
    if r.ok:
        soup=BeautifulSoup(r.text,'html.parser')
        ch=bot.get_channel(DISCOUNT_CHANNEL_ID)
        if ch:
            for row in soup.select('.search_result_row')[:5]:
                title=row.select_one('.title').text.strip();link=row['href'].split('?')[0]
                await ch.send(f'🔥 100% скидка: [{title}]({link})')

@tasks.loop(hours=12)
async def beta_game_check():
    r=requests.get('https://store.steampowered.com/search/?filter=beta&sort_by=Released_DESC')
    if r.ok:
        soup=BeautifulSoup(r.text,'html.parser')
        ch=bot.get_channel(BETA_CHANNEL_ID)
        if ch:
            for row in soup.select('.search_result_row')[:5]:
                title=row.select_one('.title').text.strip();link=row['href'].split('?')[0]
                await ch.send(f'🧪 Новая бета: [{title}]({link})')

@tasks.loop(time=time(0,10))
async def health_check():
    if datetime.utcnow().weekday()!=0: return
    mem=psutil.virtual_memory().percent
    ch=bot.get_channel(LOG_CHANNEL_ID)
    if ch: await ch.send(f'📊 Еженедельный отчёт памяти: {mem}%')

# === Bot Startup ===
def main():
    Thread(target=run_flask).start()
    bot.run(DISCORD_TOKEN)

if __name__=='__main__':
    main()
