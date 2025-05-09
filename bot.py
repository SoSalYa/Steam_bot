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

# Конфигурации из окружающей среды
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
    data = requests.get(
        'https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/',
        params={'key': STEAM_API_KEY, 'vanityurl': vanity}
    ).json().get('response', {})
    return data.get('steamid') if data.get('success') == 1 else None

def get_player_summary(steamid: str):
    players = requests.get(
        'https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/',
        params={'key': STEAM_API_KEY, 'steamids': steamid}
    ).json().get('response', {}).get('players', [])
    return players[0] if players else None

def get_owned_games(steamid):
    return requests.get(
        'https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/',
        params={'key': STEAM_API_KEY, 'steamid': steamid, 'include_appinfo': True}
    ).json().get('response', {}).get('games', [])

def is_multiplayer(appid: int) -> bool:
    try:
        info = requests.get(
            'https://store.steampowered.com/api/appdetails',
            params={'appids': appid}
        ).json().get(str(appid), {})
        if info.get('success'):
            cats = info['data'].get('categories', [])
            return any('multiplayer' in c.get('description', '').lower() for c in cats)
    except:
        pass
    return False

# Вспомогательные

def get_steam_id_for_user(discord_id: int):
    for row in main_sheet.get_all_records():
        if str(row.get('discord_id')) == str(discord_id):
            return parse_steam_url(row.get('steam_url', ''))[1]
    return None

async def try_send_dm(user: discord.User, text: str):
    try:
        dm = await user.create_dm()
        await dm.send(text)
    except Exception as e:
        print(f'Ошибка отправки DM: {e}')

# Представление для подтверждения
class ConfirmView(ui.View):
    def __init__(self, steam_type, steam_id, discord_id):
        super().__init__(timeout=60)
        self.steam_type = steam_type
        self.steam_id = steam_id
        self.discord_id = discord_id

    @ui.button(label='Да', style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        steam_url = f'https://steamcommunity.com/{self.steam_type}/{self.steam_id}'
        nick = get_player_summary(self.steam_id).get('personaname')
        # удаляем старую запись
        for idx, r in enumerate(main_sheet.get_all_records(), start=2):
            if str(r.get('discord_id')) == str(self.discord_id):
                main_sheet.delete_rows(idx)
                break
        main_sheet.append_row([self.discord_id, steam_url, nick])
        role = discord.utils.get(interaction.guild.roles, name=STEAM_ROLE_NAME)
        member = interaction.guild.get_member(self.discord_id)
        if role and member:
            await member.add_roles(role)
        await interaction.response.send_message('Привязано!', ephemeral=True)
        self.stop()

    @ui.button(label='Нет', style=discord.ButtonStyle.red)
    async def deny(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message('Отменено, пришлите новую ссылку.', ephemeral=True)
        self.stop()

# Ежедневная проверка ссылок
@tasks.loop(time=time(hour=0, minute=7, tzinfo=KYIV_TZ))
async def daily_link_check():
    for r in main_sheet.get_all_records():
        discord_id = int(r.get('discord_id'))
        _, sid = parse_steam_url(r.get('steam_url', ''))
        summary = get_player_summary(sid)
        user = bot.get_user(discord_id)
        if not summary or summary.get('communityvisibilitystate') != 3:
            await try_send_dm(user, 'Привязка Steam устарела, перепривяжите через `/перепривязать_steam`.')

# События
@bot.event
async def on_ready():
    print('Logged in as', bot.user)
    guild = discord.Object(id=TEST_GUILD_ID)
    try:
        bot.tree.clear_commands(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f'Commands synced to guild {TEST_GUILD_ID}')
    except Exception as e:
        print(f'Ошибка при синхронизации команд: {e}')
    daily_link_check.start()

@bot.event
async def on_member_join(member: discord.Member):
    await try_send_dm(member, 'Привет! Отправь ссылку на свой Steam.')

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return await bot.process_commands(message)
    if isinstance(message.channel, discord.DMChannel):
        t, sid = parse_steam_url(message.content)
        if not t:
            return await message.channel.send('Неверная ссылка, нужен steamcommunity.com/...')
        if t == 'id':
            real = resolve_vanity(sid)
            if not real:
                return await message.channel.send('Vanity не найден')
            sid = real
        if not get_player_summary(sid):
            return await message.channel.send('Профиль недоступен')
        await message.channel.send(f"{get_player_summary(sid).get('personaname')}?", view=ConfirmView(t, sid, message.author.id))
    await bot.process_commands(message)

# Slash-команды

@bot.tree.command(name='перепривязать_steam', description='Перепривязать Steam', guild=discord.Object(id=TEST_GUILD_ID))
async def rebind(interaction: discord.Interaction):
    for idx, r in enumerate(main_sheet.get_all_records(), start=2):
        if str(r.get('discord_id')) == str(interaction.user.id):
            main_sheet.delete_rows(idx)
            break
    await interaction.response.send_message('Отправь новую ссылку в ЛС.', ephemeral=True)
    await try_send_dm(interaction.user, 'Новая ссылка:')

@bot.tree.command(name='общие_игры', description='Показать общие MP-игры', guild=discord.Object(id=TEST_GUILD_ID))
@app_commands.describe(user='Пользователь')
async def common_games(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer()
    sid1 = get_steam_id_for_user(interaction.user.id)
    sid2 = get_steam_id_for_user(user.id)
    if not sid1 or not sid2:
        return await interaction.followup.send('Оба должны быть привязаны.', ephemeral=True)
    ids1 = {g['appid']: g['name'] for g in get_owned_games(sid1)}
    ids2 = {g['appid']: g['name'] for g in get_owned_games(sid2)}
    commons = [name for aid, name in ids1.items() if aid in ids2 and is_multiplayer(aid)]
    if not commons:
        return await interaction.followup.send('Нет общих MP-игр.', ephemeral=False)
    desc = '\n'.join(sorted(commons))
    await interaction.followup.send(embed=Embed(title='Общие MP-игры', description=desc[:1900]))

@bot.tree.command(name='найти_тиммейтов', description='Найти тиммейтов по игре', guild=discord.Object(id=TEST_GUILD_ID))
@app_commands.describe(game='Название игры')
async def find_teammates(interaction: discord.Interaction, game: str):
    await interaction.response.defer(ephemeral=True)
    uids = []
    for r in main_sheet.get_all_records():
        sid = parse_steam_url(r.get('steam_url', ''))[1]
        if any(g['name'].lower() == game.lower() for g in get_owned_games(sid)):
            uids.append(r.get('discord_id'))
    if not uids:
        return await interaction.followup.send('Никто не играет.', ephemeral=True)
    await interaction.followup.send(' '.join(f'<@{uid}>' for uid in uids), ephemeral=True)

# Запуск бота и Flask
if __name__ == '__main__':
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))).start()
    bot.run(DISCORD_TOKEN)
