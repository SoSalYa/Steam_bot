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
creds_json = base64.b64decode(GOOGLE_CREDS_B64).decode('utf-8')
gc = gspread.service_account_from_dict(json.loads(creds_json))
sh = gc.open_by_key(GOOGLE_SHEET_ID)
main_sheet = sh.sheet1

# Утилиты для Steam API
URL_PATTERN = re.compile(r'https?://steamcommunity\.com/(?P<type>id|profiles)/(?P<id>[^/]+)/?')

def parse_steam_url(url: str):
    m = URL_PATTERN.match(url)
    return (m.group('type'), m.group('id')) if m else (None, None)

def resolve_vanity(vanity: str):
    resp = requests.get(
        'https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/',
        params={'key': STEAM_API_KEY, 'vanityurl': vanity}
    ).json().get('response', {})
    return resp.get('steamid') if resp.get('success') == 1 else None

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
            categories = info['data'].get('categories', [])
            return any('multiplayer' in c.get('description', '').lower() for c in categories)
    except Exception:
        pass
    return False

# Вспомогательные функции

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

# View для подтверждения привязки
class ConfirmView(ui.View):
    def __init__(self, steam_type, steam_id, discord_id):
        super().__init__(timeout=60)
        self.steam_type = steam_type
        self.steam_id = steam_id
        self.discord_id = discord_id

    @ui.button(label='Да', style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        sid = self.steam_id
        steam_url = f'https://steamcommunity.com/{self.steam_type}/{sid}'
        nickname = get_player_summary(sid).get('personaname')
        # Удалить старую запись
        for idx, r in enumerate(main_sheet.get_all_records(), start=2):
            if str(r.get('discord_id')) == str(self.discord_id):
                main_sheet.delete_rows(idx)
                break
        main_sheet.append_row([self.discord_id, steam_url, nickname])
        role = discord.utils.get(interaction.guild.roles, name=STEAM_ROLE_NAME)
        member = interaction.guild.get_member(self.discord_id)
        if role and member:
            await member.add_roles(role)
        await interaction.response.send_message('Привязка завершена! Роль выдана.', ephemeral=True)
        self.stop()

    @ui.button(label='Нет', style=discord.ButtonStyle.red)
    async def deny(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message('Привязка отменена, отправьте новую ссылку.', ephemeral=True)
        self.stop()

# Ежедневная проверка ссылок
@tasks.loop(time=time(hour=0, minute=7, tzinfo=KYIV_TZ))
async def daily_link_check():
    for row in main_sheet.get_all_records():
        discord_id = int(row.get('discord_id'))
        _, sid = parse_steam_url(row.get('steam_url', ''))
        summary = get_player_summary(sid)
        user = bot.get_user(discord_id)
        if not summary or summary.get('communityvisibilitystate') != 3:
            await try_send_dm(user, 'Ваша привязка Steam устарела. Пожалуйста, перепривяжите через `/перепривязать_steam`.')

# События
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    guild = discord.Object(id=TEST_GUILD_ID)
    try:
        # Очистить и синхронизировать команды только в TEST_GUILD_ID
        bot.tree.clear_commands(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f'Commands synced to guild {TEST_GUILD_ID}')
    except Exception as e:
        print(f'Ошибка при синхронизации команд: {e}')
    daily_link_check.start()

@bot.event
async def on_member_join(member: discord.Member):
    await try_send_dm(member, 'Привет! Пожалуйста, отправь ссылку на свой профиль Steam.')

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return await bot.process_commands(message)
    if isinstance(message.channel, discord.DMChannel):
        t, sid = parse_steam_url(message.content)
        if not t:
            return await message.channel.send('Неверная ссылка, нужен формат steamcommunity.com/...')
        if t == 'id':
            real = resolve_vanity(sid)
            if not real:
                return await message.channel.send('Vanity URL не найден, попробуйте снова.')
            sid = real
        if not get_player_summary(sid):
            return await message.channel.send('Не удалось получить профиль, проверьте API-ключ.')
        await message.channel.send(
            f"{get_player_summary(sid).get('personaname')} — это вы?",
            view=ConfirmView(t, sid, message.author.id)
        )
    await bot.process_commands(message)

# Slash-команды
@bot.tree.command(name='перепривязать_steam', description='Перепривязать Steam-аккаунт', guild=discord.Object(id=TEST_GUILD_ID))
async def rebind(interaction: discord.Interaction):
    for idx, r in enumerate(main_sheet.get_all_records(), start=2):
        if str(r.get('discord_id')) == str(interaction.user.id):
            main_sheet.delete_rows(idx)
            break
    await interaction.response.send_message('Напиши новую ссылку в ЛС.', ephemeral=True)
    await try_send_dm(interaction.user, 'Отправьте новую ссылку на ваш профиль Steam.')

@bot.tree.command(name='общие_игры', description='Показать общие мультиплеерные игры', guild=discord.Object(id=TEST_GUILD_ID))
@app_commands.describe(user='Пользователь для сравнения')
async def common_games(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer()
    sid1 = get_steam_id_for_user(interaction.user.id)
    sid2 = get_steam_id_for_user(user.id)
    if not sid1 or not sid2:
        return await interaction.followup.send('Оба пользователя должны быть привязаны к Steam.', ephemeral=True)
    ids1 = {g['appid']: g['name'] for g in get_owned_games(sid1)}
    ids2 = {g['appid']: g['name'] for g in get_owned_games(sid2)}
    commons = [name for aid, name in ids1.items() if aid in ids2 and is_multiplayer(aid)]
    if not commons:
        return await interaction.followup.send('Общих мультиплеерных игр нет.', ephemeral=False)
    desc = '\n'.join(sorted(commons))
    if len(desc) > 1900:
        # Отправка файла, если список слишком длинный
        fname = 'common_mp.txt'
        with open(fname, 'w', encoding='utf-8') as f:
            f.write(desc)
        await interaction.followup.send('Список слишком длинный, смотрите файл:', file=discord.File(fname))
    else:
        await interaction.followup.send(embed=Embed(title='Общие мультиплеерные игры', description=desc))

@bot.tree.command(name='найти_тиммейтов', description='Найти тиммейтов по игре', guild=discord.Object(id=TEST_GUILD_ID))
@app_commands.describe(игра='Название игры')
async def find_teammates(interaction: discord.Interaction, игра: str):
    await interaction.response.defer(ephemeral=True)
    uids = []
    for r in main_sheet.get_all_records():
        sid = parse_steam_url(r.get('steam_url', ''))[1]
        if any(g['name'].lower() == игра.lower() for g in get_owned_games(sid)):
            uids.append(r.get('discord_id'))
    if not uids:
        return await interaction.followup.send('Никто не играет в эту игру.', ephemeral=True)
    mentions = ' '.join(f'<@{uid}>' for uid in uids)
    await interaction.followup.send(mentions, ephemeral=True)

# Запуск Flask и бота
if __name__ == '__main__':
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))).start()
    bot.run(DISCORD_TOKEN)
