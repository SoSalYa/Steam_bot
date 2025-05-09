import os
import re
import threading
import json
import base64
from datetime import time
import zoneinfo
from flask import Flask
import discord
from discord.ext import commands, tasks
from discord import ui, Embed, app_commands
import gspread
import requests

# Конфигурации из окружения
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
STEAM_API_KEY = os.getenv('STEAM_API_KEY')
GOOGLE_CREDS_B64 = os.getenv('GOOGLE_CREDS_B64')
GOOGLE_SHEET_ID = os.getenv('GOOGLE_SHEET_ID')
STEAM_ROLE_NAME = os.getenv('STEAM_ROLE_NAME', 'подвязан стим')
TEST_GUILD_ID = int(os.getenv('TEST_GUILD_ID', '123456789012345678'))

# Таймзона
KYIV_TZ = zoneinfo.ZoneInfo('Europe/Kyiv')

# Flask для keep-alive
app = Flask(__name__)
@app.route('/')
def home():
    return 'Bot is running.'

# Интенты Discord
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

# Инициализация бота
bot = commands.Bot(command_prefix='/', intents=intents)

# Инициализация Google Sheets
if not GOOGLE_CREDS_B64:
    raise ValueError('GOOGLE_CREDS_B64 not set')
creds_json = base64.b64decode(GOOGLE_CREDS_B64).decode()
gc = gspread.service_account_from_dict(json.loads(creds_json))
sh = gc.open_by_key(GOOGLE_SHEET_ID)
main_sheet = sh.sheet1

# Steam API утилиты
URL_PATTERN = re.compile(r'https?://steamcommunity\.com/(?P<type>id|profiles)/(?P<id>[^/]+)/?')

def parse_steam_url(url: str):
    m = URL_PATTERN.match(url)
    return (m.group('type'), m.group('id')) if m else (None, None)

def resolve_vanity(vanity: str):
    data = requests.get('https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/', params={'key': STEAM_API_KEY, 'vanityurl': vanity}).json()['response']
    return data.get('steamid') if data.get('success') == 1 else None

def get_player_summary(steamid: str):
    players = requests.get('https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/', params={'key': STEAM_API_KEY, 'steamids': steamid}).json()['response']['players']
    return players[0] if players else None

def get_owned_games(steamid):
    return requests.get('https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/', params={'key': STEAM_API_KEY, 'steamid': steamid, 'include_appinfo': True}).json()['response'].get('games', [])

def is_multiplayer(appid: int) -> bool:
    try:
        info = requests.get('https://store.steampowered.com/api/appdetails', params={'appids': appid}).json()[str(appid)]
        if info.get('success'):
            cats = info['data'].get('categories', [])
            return any('multiplayer' in c.get('description', '').lower() for c in cats)
    except:
        return False
    return False

# Вспомогательные функции

def get_steam_id_for_user(discord_id: int):
    for row in main_sheet.get_all_records():
        if str(row['discord_id']) == str(discord_id):
            return parse_steam_url(row['steam_url'])[1]
    return None

async def try_send_dm(user, text):
    try:
        await user.send(text)
    except Exception as e:
        print(f'Ошибка DM: {e}')

# View для подтверждения привязки
class ConfirmView(ui.View):
    def __init__(self, steam_type, steam_id, discord_id):
        super().__init__(timeout=60)
        self.steam_type = steam_type
        self.steam_id = steam_id
        self.discord_id = discord_id

    @ui.button(label='Да', style=discord.ButtonStyle.green)
    async def confirm(self, i: discord.Interaction, b: ui.Button):
        sid = self.steam_id
        steam_url = f'https://steamcommunity.com/{self.steam_type}/{sid}'
        nick = get_player_summary(sid)['personaname']
        # Удаляем старую
        for idx, r in enumerate(main_sheet.get_all_records(), start=2):
            if str(r['discord_id']) == str(self.discord_id): main_sheet.delete_rows(idx); break
        main_sheet.append_row([self.discord_id, steam_url, nick])
        role = discord.utils.get(i.guild.roles, name=STEAM_ROLE_NAME)
        m = i.guild.get_member(self.discord_id)
        if role and m: await m.add_roles(role)
        await i.response.send_message('Привязано!', ephemeral=True)
        self.stop()

    @ui.button(label='Нет', style=discord.ButtonStyle.red)
    async def deny(self, i, b):
        await i.response.send_message('Отменено, отправьте новую ссылку.', ephemeral=True)
        self.stop()

# События
@bot.event
async def on_ready():
    print('Logged in as', bot.user)
    guild = discord.Object(id=TEST_GUILD_ID)
    try:
        # Очищаем и синхронизируем только в тестовой гильдии
        bot.tree.clear_commands(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f'Commands synced to guild {TEST_GUILD_ID}')
    except Exception as e:
        print(f'Ошибка при синхронизации команд: {e}')
    daily_link_check.start()

@bot.event
async def on_member_join(member):
    await try_send_dm(member, 'Привет! Отправь ссылку на Steam.')

@bot.event
async def on_message(msg):
    if msg.author.bot or not isinstance(msg.channel, discord.DMChannel): return await bot.process_commands(msg)
    t, sid = parse_steam_url(msg.content)
    if not t:
        return await msg.channel.send('Неверно, нужен steamcommunity.com/...')
    if t == 'id':
        real = resolve_vanity(sid)
        if not real: return await msg.channel.send('Vanity не найден')
        sid = real
    if not get_player_summary(sid):
        return await msg.channel.send('Профиль не доступен')
    await msg.channel.send(f"{get_player_summary(sid)['personaname']}?", view=ConfirmView(t, sid, msg.author.id))

# Slash-команды

@bot.tree.command(name='перепривязать_steam', guild=discord.Object(id=TEST_GUILD_ID))
async def rebind(i: discord.Interaction):
    for idx, r in enumerate(main_sheet.get_all_records(), start=2):
        if str(r['discord_id']) == str(i.user.id): main_sheet.delete_rows(idx); break
    await i.response.send_message('Отправь новую ссылку в ЛС.', ephemeral=True)
    await try_send_dm(i.user, 'Новая ссылка на Steam:')

@bot.tree.command(name='общие_игры', guild=discord.Object(id=TEST_GUILD_ID))
@app_commands.describe(user='Пользователь')
async def common_games(i: discord.Interaction, user: discord.Member):
    await i.response.defer()
    sid1, sid2 = get_steam_id_for_user(i.user.id), get_steam_id_for_user(user.id)
    if not sid1 or not sid2:
        return await i.followup.send('Оба должны быть привязаны.', ephemeral=True)
    ids1 = {g['appid']:g['name'] for g in get_owned_games(sid1)}
    ids2 = {g['appid']:g['name'] for g in get_owned_games(sid2)}
    commons = [n for aid,n in ids1.items() if aid in ids2 and is_multiplayer(aid)]
    if not commons:
        return await i.followup.send('Нет общих MP-игр.')
    desc = '\n'.join(sorted(commons))
    await i.followup.send(embed=Embed(title='Общие MP-игры', description=desc[:1900]))

@bot.tree.command(name='найти_тиммейтов', guild=discord.Object(id=TEST_GUILD_ID))
@app_commands.describe(game='Название игры')
async def find_teammates(i: discord.Interaction, game: str):
    await i.response.defer(ephemeral=True)
    uids = [r['discord_id'] for r in main_sheet.get_all_records() if any(g['name'].lower()==game.lower() for g in get_owned_games(parse_steam_url(r['steam_url'])[1]))]
    if not uids: return await i.followup.send('Никто не играет.')
    await i.followup.send(' '.join(f'<@{uid}>' for uid in uids), ephemeral=True)

# Запуск
if __name__ == '__main__':
    threading.Thread(target=lambda:app.run(host='0.0.0.0', port=int(os.getenv('PORT',5000)))).start()
    bot.run(DISCORD_TOKEN)
