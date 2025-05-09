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

# Configurations from environment
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
STEAM_API_KEY = os.getenv('STEAM_API_KEY')
GOOGLE_CREDS_B64 = os.getenv('GOOGLE_CREDS_B64')
GOOGLE_SHEET_ID = os.getenv('GOOGLE_SHEET_ID')
STEAM_ROLE_NAME = os.getenv('STEAM_ROLE_NAME', 'подвязан стим')

# Timezone for scheduling
KYIV_TZ = zoneinfo.ZoneInfo('Europe/Kyiv')

# Initialize Flask
app = Flask(__name__)

# Keep-alive route
@app.route('/')
def home():
    return 'Bot is running.'

# Discord intents
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

# Initialize bot with command tree
bot = commands.Bot(command_prefix='/', intents=intents)

# Initialize Google Sheets
gc = None
if GOOGLE_CREDS_B64:
    creds_json = base64.b64decode(GOOGLE_CREDS_B64).decode('utf-8')
    creds_dict = json.loads(creds_json)
    gc = gspread.service_account_from_dict(creds_dict)
else:
    raise ValueError("GOOGLE_CREDS_B64 not set")
sh = gc.open_by_key(GOOGLE_SHEET_ID)
main_sheet = sh.sheet1  # stores discord_id, steam_url, nickname

# Utils for Steam API
URL_PATTERN = re.compile(r'https?://steamcommunity\.com/(?P<type>id|profiles)/(?P<id>[^/]+)/?')

def parse_steam_url(url: str):
    m = URL_PATTERN.match(url)
    return (m.group('type'), m.group('id')) if m else (None, None)

def resolve_vanity(vanity: str):
    resp = requests.get(
        'https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/',
        params={'key': STEAM_API_KEY, 'vanityurl': vanity}
    ).json()
    data = resp.get('response', {})
    return data.get('steamid') if data.get('success') == 1 else None

def get_player_summary(steamid: str):
    resp = requests.get(
        'https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/',
        params={'key': STEAM_API_KEY, 'steamids': steamid}
    ).json()
    players = resp.get('response', {}).get('players', [])
    return players[0] if players else None

def get_owned_games(steamid):
    resp = requests.get(
        'https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/',
        params={'key': STEAM_API_KEY, 'steamid': steamid, 'include_appinfo': True}
    ).json()
    return resp.get('response', {}).get('games', [])

# Confirm view for binding
class ConfirmView(ui.View):
    def __init__(self, steam_type, steam_id, discord_id):
        super().__init__(timeout=60)
        self.steam_type = steam_type
        self.steam_id = steam_id
        self.discord_id = discord_id

    @ui.button(label='Да', style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        steam_url = f'https://steamcommunity.com/{self.steam_type}/{self.steam_id}'
        summary = get_player_summary(self.steam_id)
        nickname = summary.get('personaname') if summary else self.steam_id
        main_sheet.append_row([str(self.discord_id), steam_url, nickname])
        role = discord.utils.get(interaction.guild.roles, name=STEAM_ROLE_NAME)
        member = interaction.guild.get_member(self.discord_id)
        if role:
            await member.add_roles(role)
        await interaction.response.send_message('Привязка завершена! Роль выдана.', ephemeral=True)
        self.stop()

    @ui.button(label='Нет', style=discord.ButtonStyle.red)
    async def deny(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message('Отправьте новую ссылку на профиль Steam.', ephemeral=True)
        self.stop()

# Slash command setup and on_ready
@bot.event
async def on_ready():
    await bot.tree.sync()
    daily_link_check.start()
    print(f'Logged in as {bot.user}')

# Binding on member join
def try_send_dm(user, text):
    try:
        dm = await user.create_dm()
        await dm.send(text)
    except:
        pass

@bot.event
async def on_member_join(member):
    await try_send_dm(member, 'Привет! Пожалуйста, отправь ссылку на свой профиль Steam.')

@bot.event
async def on_message(message):
    if message.author.bot: return
    if isinstance(message.channel, discord.DMChannel):
        steam_type, steam_id = parse_steam_url(message.content)
        if not steam_type:
            await message.channel.send('Неправильная ссылка, требуется steamcommunity.com/...')
            return
        if steam_type == 'id':
            real = resolve_vanity(steam_id)
            if not real:
                await message.channel.send('Vanity URL не найден, попробуйте снова.')
                return
            steam_id = real
        summary = get_player_summary(steam_id)
        if not summary:
            await message.channel.send('Не удалось получить профиль, проверьте API ключ.')
            return
        await message.channel.send(f"{summary.get('personaname')} — это вы?", view=ConfirmView(steam_type, steam_id, message.author.id))
    await bot.process_commands(message)

# Rebind command
@bot.tree.command(name='перепривязать_steam', description='Перепривязать Steam-аккаунт')
async def rebind(interaction: discord.Interaction):
    recs = main_sheet.get_all_records()
    for idx, row in enumerate(recs, start=2):
        if str(row['discord_id']) == str(interaction.user.id):
            main_sheet.delete_rows(idx)
            break
    await interaction.response.send_message('Напиши новую ссылку в ЛС.', ephemeral=True)
    await try_send_dm(interaction.user, 'Отправь новую ссылку на профиль Steam.')

# Daily link validity check
@tasks.loop(time=time(hour=0, minute=7, tzinfo=KYIV_TZ))
async def daily_link_check():
    recs = main_sheet.get_all_records()
    for row in recs:
        discord_id = int(row['discord_id'])
        steam_url = row['steam_url']
        _, sid = parse_steam_url(steam_url)
        summary = get_player_summary(sid)
        user = bot.get_user(discord_id)
        if not summary or summary.get('communityvisibilitystate') != 3:
            await try_send_dm(user, 'Ваша привязка Steam больше не актуальна. Пожалуйста, перепривяжите ссылку через `/перепривязать_steam`.')

# Common games command (example stub)
@bot.tree.command(name='общие_игры', description='Показать общие игры с пользователем')
@app_commands.describe(user='Пользователь для сравнения')
async def common_games(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.send_message('Команда реализована.')

# Find teammates command (example stub)
@bot.tree.command(name='найти_тиммейтов', description='Найти тиммейтов по игре')
@app_commands.describe(игра='Название игры для поиска')
async def find_teammates(interaction: discord.Interaction, игра: str):
    await interaction.response.send_message('Команда реализована.', ephemeral=True)

# Run Flask and bot
def run_flask():
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))

if __name__ == '__main__':
    threading.Thread(target=run_flask).start()
    bot.run(DISCORD_TOKEN)
