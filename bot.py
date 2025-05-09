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
from discord import ui, Embed
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

# Initialize bot
bot = commands.Bot(command_prefix='/', intents=intents)

# Initialize Google Sheets
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
    if not m:
        return None, None
    return m.group('type'), m.group('id')

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

# Member join binding
@bot.event
async def on_member_join(member):
    try:
        dm = await member.create_dm()
        await dm.send('Привет! Пожалуйста, отправь ссылку на свой профиль Steam.')
    except:
        pass

@bot.event
async def on_message(message):
    if message.author.bot:
        return
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
        view = ConfirmView(steam_type, steam_id, message.author.id)
        await message.channel.send(f"{summary.get('personaname')} — это вы?", view=view)
    await bot.process_commands(message)

# Rebind command
@bot.slash_command(name='перепривязать_steam', description='Перепривязать Steam-аккаунт')
async def rebind(interaction: discord.Interaction):
    recs = main_sheet.get_all_records()
    for idx, row in enumerate(recs, start=2):
        if str(row['discord_id']) == str(interaction.user.id):
            main_sheet.delete_rows(idx)
            break
    await interaction.response.send_message('Напиши новую ссылку в ЛС.', ephemeral=True)
    dm = await interaction.user.create_dm()
    await dm.send('Отправь новую ссылку на профиль Steam.')

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
            try:
                dm = await user.create_dm()
                await dm.send('Ваша привязка Steam больше не актуальна. Пожалуйста, перепривяжите ссылку через `/перепривязать_steam`.')
            except:
                pass

@daily_link_check.before_loop
async def before_link_check():
    await bot.wait_until_ready()

daily_link_check.start()

# Common games command
@bot.slash_command(name='общие_игры', description='Показать общие игры с пользователем')
async def common_games(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer()
    def find(discord_id):
        for r in main_sheet.get_all_records():
            if str(r['discord_id']) == str(discord_id):
                return r['steam_url']
        return None
    url1 = find(interaction.user.id)
    url2 = find(user.id)
    if not url1 or not url2:
        await interaction.followup.send('Оба должны иметь привязанный Steam.'); return
    # Fetch owned games
    def fetch(sid):
        resp = requests.get(
            'https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/',
            params={'key': STEAM_API_KEY, 'steamid': sid, 'include_appinfo': True}
        ).json()
        return resp.get('response', {}).get('games', [])
    _, sid1 = parse_steam_url(url1)
    _, sid2 = parse_steam_url(url2)
    games1 = fetch(sid1)
    games2 = fetch(sid2)
    m2 = {g['appid']: g for g in games2}
    common = [(g['name'], m2[g['appid']].get('playtime_forever', 0)) for g in games1 if g['appid'] in m2]
    if not common:
        await interaction.followup.send('Общих игр нет.'); return
    common.sort(key=lambda x: x[1], reverse=True)
    desc = '\n'.join(f"{i+1}. {nm} — {(h//60 if h>0 else '?')} ч" for i, (nm, h) in enumerate(common))
    embed = Embed(title=f'Общие игры с {user.display_name} ({len(common)})', description=desc)
    await interaction.followup.send(embed=embed)

# Find teammates command
@bot.slash_command(name='найти_тиммейтов', description='Найти тиммейтов по игре')
async def find_teammates(interaction: discord.Interaction, игра: str):
    await interaction.response.defer(ephemeral=True)
    recs = main_sheet.get_all_records()
    found = []
    for r in recs:
        _, sid = parse_steam_url(r['steam_url'])
        games = requests.get(
            'https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/',
            params={'key': STEAM_API_KEY, 'steamid': sid, 'include_appinfo': True}
        ).json().get('response', {}).get('games', [])
        for g in games:
            if игра.lower() in g['name'].lower():
                h = g.get('playtime_forever', 0)
                found.append((r['discord_id'], h//60 if h>0 else '?'))
                break
    if not found:
        await interaction.followup.send(f'Никто не играет в "{игра}".'); return
    found.sort(key=lambda x: x[1] if isinstance(x[1], int) else -1, reverse=True)
    desc = '\n'.join(f'<@{uid}> — {hrs} ч' for uid, hrs in found)
    embed = Embed(title=f'Тиммейты по {игра}', description=desc)
    await interaction.followup.send(embed=embed)

# Run Flask and bot
if __name__ == '__main__':
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))).start()
    bot.run(DISCORD_TOKEN)
