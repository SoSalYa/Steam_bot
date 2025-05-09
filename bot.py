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
if GOOGLE_CREDS_B64:
    creds_json = base64.b64decode(GOOGLE_CREDS_B64).decode('utf-8')
    creds_dict = json.loads(creds_json)
    gc = gspread.service_account_from_dict(creds_dict)
else:
    raise ValueError('GOOGLE_CREDS_B64 not set')
sh = gc.open_by_key(GOOGLE_SHEET_ID)
main_sheet = sh.sheet1

# Steam API утилиты
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

# Проверка мультоплеера через Store API
def is_multiplayer(appid: int) -> bool:
    try:
        resp = requests.get(
            'https://store.steampowered.com/api/appdetails',
            params={'appids': appid}
        ).json().get(str(appid), {})
        if resp.get('success'):
            categories = resp.get('data', {}).get('categories', [])
            return any('multiplayer' in c.get('description', '').lower() for c in categories)
    except Exception:
        pass
    return False

# Получить steamid по discord_id из Google Sheets
def get_steam_id_for_user(discord_id: int):
    records = main_sheet.get_all_records()
    for row in records:
        if str(row.get('discord_id')) == str(discord_id):
            _, sid = parse_steam_url(row.get('steam_url', ''))
            return sid
    return None

# Представление с кнопками подтверждения
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
        existing = get_steam_id_for_user(self.discord_id)
        if existing:
            all_records = main_sheet.get_all_records()
            for idx, r in enumerate(all_records, start=2):
                if str(r.get('discord_id')) == str(self.discord_id):
                    main_sheet.delete_rows(idx)
                    break
        main_sheet.append_row([str(self.discord_id), steam_url, nickname])
        role = discord.utils.get(interaction.guild.roles, name=STEAM_ROLE_NAME)
        member = interaction.guild.get_member(self.discord_id)
        if role and member:
            await member.add_roles(role)
        await interaction.response.send_message('Привязка завершена! Роль выдана.', ephemeral=True)
        self.stop()

    @ui.button(label='Нет', style=discord.ButtonStyle.red)
    async def deny(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message('Отправьте новую ссылку на профиль Steam.', ephemeral=True)
        self.stop()

# Асинхронная отправка DM
async def try_send_dm(user: discord.User, text: str):
    try:
        dm = await user.create_dm()
        await dm.send(text)
    except Exception as e:
        print(f'Ошибка отправки DM: {e}')

# События
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    try:
        guild = discord.Object(id=TEST_GUILD_ID)
        await bot.tree.sync(guild=guild)
        print(f'Commands synced to guild {TEST_GUILD_ID}')
    except discord.errors.Forbidden:
        print(f'Не удалось синхронизировать команды для гильдии {TEST_GUILD_ID}: Missing Access')
    try:
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
        print('Global commands cleared and synced')
    except Exception as e:
        print(f'Ошибка при синхронизации глобальных команд: {e}')
    daily_link_check.start()

@bot.event
async def on_member_join(member: discord.Member):
    await try_send_dm(member, 'Привет! Пожалуйста, отправь ссылку на свой профиль Steam.')

@bot.event
async def on_message(message: discord.Message):
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
        await message.channel.send(
            f"{summary.get('personaname')} — это вы?",
            view=ConfirmView(steam_type, steam_id, message.author.id)
        )
    await bot.process_commands(message)

# Команда перепривязки
@bot.tree.command(name='перепривязать_steam', description='Перепривязать Steam-аккаунт', guild=discord.Object(id=TEST_GUILD_ID))
async def rebind(interaction: discord.Interaction):
    all_records = main_sheet.get_all_records()
    for idx, row in enumerate(all_records, start=2):
        if str(row.get('discord_id')) == str(interaction.user.id):
            main_sheet.delete_rows(idx)
            break
    await interaction.response.send_message('Напиши новую ссылку в ЛС.', ephemeral=True)
    await try_send_dm(interaction.user, 'Отправь новую ссылку на профиль Steam.')

# Ежедневная проверка ссылок
@tasks.loop(time=time(hour=0, minute=7, tzinfo=KYIV_TZ))
async def daily_link_check():
    records = main_sheet.get_all_records()
    for row in records:
        discord_id = int(row.get('discord_id'))
        _, sid = parse_steam_url(row.get('steam_url', ''))
        summary = get_player_summary(sid)
        user = bot.get_user(discord_id)
        if not summary or summary.get('communityvisibilitystate') != 3:
            await try_send_dm(user, 'Ваша привязка Steam больше не актуальна. Пожалуйста, перепривяжите ссылку через `/перепривязать_steam`.')

# Команда общие_игры с фильтром мультиплеера
@bot.tree.command(name='общие_игры', description='Показать общие мультиплеерные игры с пользователем', guild=discord.Object(id=TEST_GUILD_ID))
@app_commands.describe(user='Пользователь для сравнения')
async def common_games(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer()
    sid1 = get_steam_id_for_user(interaction.user.id)
    sid2 = get_steam_id_for_user(user.id)
    if not sid1 or not sid2:
        return await interaction.followup.send('Оба пользователя должны быть привязаны к Steam.', ephemeral=True)
    games1 = get_owned_games(sid1)
    games2 = get_owned_games(sid2)
    ids1 = {g['appid']: g['name'] for g in games1}
    ids2 = {g['appid']: g['name'] for g in games2}
    common_ids = set(ids1.keys()).intersection(ids2.keys())
    common_mp = []
    for appid in common_ids:
        if is_multiplayer(appid):
            common_mp.append(ids1[appid])
    if not common_mp:
        return await interaction.followup.send('У вас нет общих мультиплеерных игр.', ephemeral=False)
    common_mp.sort()
    desc = '\n'.join(common_mp)
    if len(desc) > 1900:
        fname = 'common_multiplayer.txt'
        with open(fname, 'w', encoding='utf-8') as f:
            f.write(desc)
        await interaction.followup.send('Список слишком длинный, смотрите файл:', file=discord.File(fname))
    else:
        embed = Embed(title='Общие мультиплеерные игры', description=desc)
        await interaction.followup.send(embed=embed)

# Команда найти_тиммейтов
@bot.tree.command(name='найти_тиммейтов', description='Найти тиммейтов по игре', guild=discord.Object(id=TEST_GUILD_ID))
@app_commands.describe(игра='Название игры для поиска')
async def find_teammates(interaction: discord.Interaction, игра: str):
    await interaction.response.defer(ephemeral=True)
    targets = []
    records = main_sheet.get_all_records()
    for row in records:
        _, sid = parse_steam_url(row.get('steam_url', ''))
        owned = get_owned_games(sid)
        if any(g['name'].lower() == игра.lower() for g in owned):
            targets.append(int(row.get('discord_id')))
    if not targets:
        return await interaction.followup.send('Никто не играет в эту игру.', ephemeral=True)
    mentions = [f"<@{uid}>" for uid in targets]
    await interaction.followup.send(' '.join(mentions), ephemeral=True)

# Запуск Flask и бота

def run_flask():
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    threading.Thread(target=run_flask).start()
    bot.run(DISCORD_TOKEN)
