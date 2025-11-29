"""
Message Cleanup Module
Автоматическое удаление временных сообщений
"""

import discord
from discord.ext import tasks
from datetime import datetime, timedelta
from typing import Dict, Set
import asyncio


class MessageCleanup:
    """Класс для управления автоудалением сообщений"""
    
    def __init__(self, bot: discord.Client):
        self.bot = bot
        # Хранилище: {message_id: (channel_id, delete_at_time)}
        self.scheduled_deletions: Dict[int, tuple] = {}
        # Хранилище для tracking активных views
        self.active_views: Dict[int, discord.ui.View] = {}
        
    def start_cleanup_task(self):
        """Запускает фоновую задачу очистки"""
        if not self.cleanup_messages.is_running():
            self.cleanup_messages.start()
            print("✔ Message cleanup task started")
    
    def stop_cleanup_task(self):
        """Останавливает задачу"""
        self.cleanup_messages.cancel()
        print("Message cleanup task stopped")
    
    def schedule_deletion(self, message: discord.Message, delay_seconds: int = 300):
        """
        Добавляет сообщение в очередь на удаление
        
        Args:
            message: Discord сообщение для удаления
            delay_seconds: задержка в секундах (по умолчанию 5 минут)
        """
        delete_at = datetime.utcnow() + timedelta(seconds=delay_seconds)
        self.scheduled_deletions[message.id] = (message.channel.id, delete_at)
        print(f"Scheduled deletion for message {message.id} at {delete_at}")
    
    def cancel_deletion(self, message_id: int) -> bool:
        """
        Отменяет запланированное удаление
        
        Returns:
            True если удаление было отменено, False если сообщения не было в очереди
        """
        if message_id in self.scheduled_deletions:
            del self.scheduled_deletions[message_id]
            return True
        return False
    
    def register_view(self, message: discord.Message, view: discord.ui.View):
        """
        Регистрирует view для отслеживания
        При истечении timeout view автоматически удаляет сообщение
        """
        self.active_views[message.id] = view
        
        # Добавляем обработчик on_timeout
        original_timeout = view.on_timeout
        
        async def custom_timeout():
            # Вызываем оригинальный timeout если есть
            if original_timeout:
                await original_timeout()
            
            # Удаляем сообщение
            try:
                await message.delete()
                print(f"Deleted expired view message {message.id}")
            except discord.NotFound:
                pass
            except Exception as e:
                print(f"Error deleting view message: {e}")
            
            # Убираем из активных
            if message.id in self.active_views:
                del self.active_views[message.id]
        
        view.on_timeout = custom_timeout
    
    @tasks.loop(seconds=60)
    async def cleanup_messages(self):
        """
        Фоновая задача для удаления запланированных сообщений
        Проверяется каждую минуту
        """
        try:
            now = datetime.utcnow()
            to_delete = []
            
            # Находим сообщения, которые пора удалить
            for msg_id, (channel_id, delete_at) in self.scheduled_deletions.items():
                if now >= delete_at:
                    to_delete.append((msg_id, channel_id))
            
            # Удаляем сообщения
            for msg_id, channel_id in to_delete:
                try:
                    channel = self.bot.get_channel(channel_id)
                    if channel:
                        message = await channel.fetch_message(msg_id)
                        await message.delete()
                        print(f"✔ Auto-deleted message {msg_id}")
                except discord.NotFound:
                    pass  # Сообщение уже удалено
                except Exception as e:
                    print(f"Error auto-deleting message {msg_id}: {e}")
                finally:
                    # Убираем из очереди в любом случае
                    if msg_id in self.scheduled_deletions:
                        del self.scheduled_deletions[msg_id]
            
        except Exception as e:
            print(f"Error in cleanup_messages task: {e}")
    
    @cleanup_messages.before_loop
    async def before_cleanup_messages(self):
        """Ждёт готовности бота"""
        await self.bot.wait_until_ready()


class EphemeralView(discord.ui.View):
    """
    Базовый класс для View, которые автоматически удаляются
    """
    
    def __init__(self, cleanup_manager: MessageCleanup, timeout: float = 600):
        """
        Args:
            cleanup_manager: экземпляр MessageCleanup
            timeout: время жизни view в секундах
        """
        super().__init__(timeout=timeout)
        self.cleanup_manager = cleanup_manager
        self.message: discord.Message = None
    
    async def on_timeout(self):
        """При истечении timeout удаляет сообщение"""
        if self.message:
            try:
                await self.message.delete()
                print(f"Deleted expired ephemeral view message {self.message.id}")
            except discord.NotFound:
                pass
            except Exception as e:
                print(f"Error deleting ephemeral message: {e}")
    
    async def send(self, interaction: discord.Interaction, **kwargs):
        """
        Отправляет сообщение с view и регистрирует его для удаления
        """
        await interaction.response.send_message(view=self, **kwargs)
        self.message = await interaction.original_response()
        self.cleanup_manager.register_view(self.message, self)
        return self.message


class AutoDeleteButton(discord.ui.Button):
    """
    Кнопка, которая удаляет сообщение при нажатии
    """
    
    def __init__(self, label: str = "Delete", emoji: str = "🗑️", 
                 style=discord.ButtonStyle.danger, authorized_user: int = None):
        """
        Args:
            label: текст кнопки
            emoji: эмодзи
            style: стиль кнопки
            authorized_user: ID пользователя, который может нажать (None = любой)
        """
        super().__init__(label=label, emoji=emoji, style=style)
        self.authorized_user = authorized_user
    
    async def callback(self, interaction: discord.Interaction):
        # Проверяем права
        if self.authorized_user and interaction.user.id != self.authorized_user:
            await interaction.response.send_message(
                "❌ Only the command author can delete this message.",
                ephemeral=True
            )
            return
        
        # Удаляем сообщение
        try:
            await interaction.message.delete()
        except discord.NotFound:
            await interaction.response.send_message(
                "Message already deleted.",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"Error deleting message: {e}",
                ephemeral=True
            )


# === Вспомогательные функции ===

async def send_temporary_message(channel: discord.TextChannel, content: str = None, 
                                 embed: discord.Embed = None, 
                                 delete_after: int = 300) -> discord.Message:
    """
    Отправляет временное сообщение, которое автоматически удалится
    
    Args:
        channel: канал для отправки
        content: текст сообщения
        embed: embed
        delete_after: через сколько секунд удалить
    
    Returns:
        Отправленное сообщение
    """
    message = await channel.send(content=content, embed=embed, delete_after=delete_after)
    return message


async def send_with_delete_button(interaction: discord.Interaction, 
                                  embed: discord.Embed = None,
                                  content: str = None,
                                  ephemeral: bool = False) -> discord.Message:
    """
    Отправляет сообщение с кнопкой удаления
    
    Args:
        interaction: Discord interaction
        embed: embed для отправки
        content: текстовое содержимое
        ephemeral: ephemeral сообщение или нет
    
    Returns:
        Отправленное сообщение
    """
    view = discord.ui.View(timeout=600)
    delete_button = AutoDeleteButton(authorized_user=interaction.user.id)
    view.add_item(delete_button)
    
    await interaction.response.send_message(
        content=content,
        embed=embed,
        view=view,
        ephemeral=ephemeral
    )
    
    if not ephemeral:
        return await interaction.original_response()
    return None


def add_delete_button_to_view(view: discord.ui.View, authorized_user: int = None):
    """
    Добавляет кнопку удаления к существующему view
    
    Args:
        view: View для добавления кнопки
        authorized_user: ID авторизованного пользователя
    """
    delete_button = AutoDeleteButton(authorized_user=authorized_user)
    view.add_item(delete_button)


# === Пример использования ===

"""
# В main bot file:

cleanup_manager = MessageCleanup(bot)
cleanup_manager.start_cleanup_task()

# Для команды с автоудалением:
@bot.tree.command(name="example")
async def example_command(interaction: discord.Interaction):
    embed = discord.Embed(title="Temporary Message")
    view = discord.ui.View()
    # ... добавляем кнопки в view
    
    await interaction.response.send_message(embed=embed, view=view)
    msg = await interaction.original_response()
    
    # Удалится через 5 минут
    cleanup_manager.schedule_deletion(msg, delay_seconds=300)

# Для view с auto-delete:
class MyView(EphemeralView):
    def __init__(self, cleanup_manager):
        super().__init__(cleanup_manager, timeout=300)
        # добавляем кнопки
    
    @discord.ui.button(label="Click me")
    async def button_callback(self, interaction, button):
        await interaction.response.send_message("Clicked!")

# Использование:
view = MyView(cleanup_manager)
await view.send(interaction, embed=my_embed)
"""