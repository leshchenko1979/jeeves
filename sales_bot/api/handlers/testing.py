"""Testing command handlers."""

import logging
from typing import Dict, List

from core.messaging.conductor import DialogConductor
from core.messaging.enums import DialogStatus
from core.telegram.client import app
from pyrogram import Client, filters
from pyrogram.raw import functions
from pyrogram.types import Message

logger = logging.getLogger(__name__)

# Store active test dialogs
test_dialogs: Dict[int, DialogConductor] = {}
# Store dialog messages for analysis
dialog_messages: Dict[int, List[Message]] = {}
# Testing group username
TESTING_GROUP = "@sales_bot_analysis"

# Mapping from dialog status to result tag
STATUS_TO_TAG = {
    DialogStatus.active: "#уточнение",  # Dialog is still active
    DialogStatus.closed: "#продажа",  # Successful sale
    DialogStatus.blocked: "#заблокировал",  # Blocked
    DialogStatus.rejected: "#отказ",  # Explicit rejection
    DialogStatus.not_qualified: "#неподходит",  # Not qualified
    DialogStatus.meeting_scheduled: "#успех",  # Meeting scheduled
}

# Tag descriptions for the message
TAG_DESCRIPTIONS = {
    "#уточнение": "Требуется уточнение деталей",
    "#продажа": "Успешная продажа",
    "#неподходит": "Клиент не соответствует критериям",
    "#отказ": "Отказ от покупки",
    "#успех": "Назначена встреча с клиентом",
    "#тест": "Тестовый диалог с отделом продаж",
    "#заблокировал": "Клиент заблокировал бота",
}


# Main command handlers
@app.on_message(filters.command("test_dialog"))
async def cmd_test_dialog(client: Client, message: Message):
    """Test dialog with sales bot."""
    user_id = message.from_user.id

    if user_id in test_dialogs:
        await message.reply(
            "⚠️ У вас уже есть активный тестовый диалог. Дождитесь его завершения."
        )
        return

    async def send_message(text: str) -> None:
        sent_msg = await message.reply(text)
        if user_id in dialog_messages:
            dialog_messages[user_id].append(sent_msg)

    try:
        conductor = DialogConductor(send_func=send_message)
        test_dialogs[user_id] = conductor
        dialog_messages[user_id] = [message]

        await conductor.start_dialog()
        logger.info(f"Started test dialog for user {user_id}")

    except Exception:
        await handle_error(
            message, "Не удалось запустить тестовый диалог. Попробуйте позже.", user_id
        )


@app.on_message(filters.command("stop") & filters.private)
async def cmd_stop_dialog(client: Client, message: Message):
    """Stop active test dialog."""
    user_id = message.from_user.id

    if user_id not in test_dialogs:
        await message.reply("У вас нет активного тестового диалога.")
        return

    try:
        thread_link = await forward_dialog_for_analysis(client, user_id)
        await cleanup_dialog(user_id)
        await send_completion_message(message, thread_link, stopped=True)

    except Exception:
        await handle_error(message, "Произошла ошибка при остановке диалога.", user_id)


@app.on_message(~filters.command("test_dialog") & filters.private)
async def on_test_message(client: Client, message: Message):
    """Handle messages in test dialog."""
    user_id = message.from_user.id

    if user_id not in test_dialogs:
        if not message.text.startswith("/"):
            await message.reply(
                "Тестовый диалог не активен. Используйте /test_dialog чтобы начать новый."
            )
        return

    try:
        if user_id in dialog_messages:
            dialog_messages[user_id].append(message)

        conductor = test_dialogs[user_id]
        is_completed, error = await conductor.handle_message(message.text)

        if error:
            await handle_error(
                message,
                f"Произошла ошибка при обработке сообщения: {error}\nДиалог завершен.",
                user_id,
            )
            return

        if not is_completed:
            return

        thread_link = await forward_dialog_for_analysis(client, user_id)
        await cleanup_dialog(user_id)
        await send_completion_message(message, thread_link)

    except Exception:
        await handle_error(
            message,
            "Произошла ошибка при обработке сообщения. Диалог завершен.",
            user_id,
        )


# Helper functions
async def cleanup_dialog(user_id: int):
    """Clean up dialog data for user."""
    if user_id in test_dialogs:
        del test_dialogs[user_id]
    if user_id in dialog_messages:
        del dialog_messages[user_id]


async def handle_error(message: Message, error: str, user_id: int):
    """Handle error and cleanup dialog."""
    logger.error(f"Error: {error}", exc_info=True)
    await cleanup_dialog(user_id)
    await message.reply(f"⚠️ {error}")


async def send_completion_message(
    message: Message, thread_link: str, stopped: bool = False
):
    """Send completion message with thread link and feedback instructions."""
    action = "остановлен" if stopped else "завершен"
    await message.reply(
        f"Диалог {action} и переслан в группу анализа.\n"
        f"Вот ссылка на тред: {thread_link}\n\n"
        "Пожалуйста, оцените сообщения бота:\n"
        "- Поставьте реакции 👍/👎\n"
        "- Ответьте на сообщение с комментарием\n"
        "- Записать общее впечатление (можно голосовым)"
    )


async def create_forum_topic(
    client: Client, group_id: int, title: str
) -> tuple[int, int]:
    """Create forum topic and return topic_id and channel_peer."""
    channel_peer = await client.resolve_peer(group_id)

    topic = await client.invoke(
        functions.channels.CreateForumTopic(
            channel=channel_peer,
            title=title,
            icon_color=0x6FB9F0,  # Light blue color
            random_id=client.rnd_id(),
        )
    )

    topic_id = topic.updates[0].id
    if not topic_id:
        logger.error("Failed to create forum topic")
        return 0, 0

    logger.info(f"Created forum topic: {topic_id}")
    return topic_id, channel_peer


async def create_thread_message(
    client: Client,
    group_id: int,
    topic_id: int,
    messages: List[Message],
    status: DialogStatus,
) -> Message:
    """Create initial thread message with dialog info."""
    result_tag = STATUS_TO_TAG.get(status, "#тест")
    tag_description = TAG_DESCRIPTIONS.get(result_tag, "")

    return await client.send_message(
        chat_id=group_id,
        reply_to_message_id=topic_id,
        text=f"📊 Информация о диалоге:\n"
        f"- Продавец: {messages[0].from_user.first_name}\n"
        f"- Дата: {messages[0].date.strftime('%Y-%m-%d')}\n"
        f"- Итог: {result_tag} - {tag_description}\n\n"
        f"💬 Диалог ниже.\n"
        f"Вы можете:\n"
        f"- Ответить на конкретное сообщение с комментарием\n"
        f"- Поставить реакцию на сообщение бота\n"
        f"- Записать общее впечатление (можно голосовым)",
    )


async def forward_messages_to_topic(
    client: Client, messages: List[Message], group_id: int, topic_id: int
) -> None:
    """Forward all dialog messages to the topic."""
    try:
        await client.invoke(
            functions.messages.ForwardMessages(
                from_peer=await client.resolve_peer(messages[0].chat.id),
                to_peer=await client.resolve_peer(group_id),
                top_msg_id=topic_id,
                id=[msg.id for msg in messages],
                random_id=[client.rnd_id() for _ in messages],
            )
        )
    except Exception as e:
        logger.error(f"Error forwarding messages: {e}")


async def forward_dialog_for_analysis(client: Client, user_id: int) -> str:
    """Forward dialog to testing group for analysis."""
    try:
        if user_id not in dialog_messages or user_id not in test_dialogs:
            logger.error(f"No messages or dialog found for user {user_id}")
            return ""

        messages = dialog_messages[user_id]
        conductor = test_dialogs[user_id]

        if not messages:
            logger.error("Empty messages list")
            return ""

        group = await client.get_chat(TESTING_GROUP)
        if not group or not group.id:
            logger.error("Failed to get testing group info")
            return ""

        title = f"Диалог с {messages[0].from_user.first_name}"
        topic_id, channel_peer = await create_forum_topic(client, group.id, title)
        if not topic_id:
            return ""

        thread_msg = await create_thread_message(
            client, group.id, topic_id, messages, conductor.get_current_status()
        )

        await forward_messages_to_topic(client, messages, group.id, topic_id)

        thread_link = f"https://t.me/c/{str(group.id)[4:]}/{topic_id}/{thread_msg.id}"
        logger.info(f"Generated thread link: {thread_link}")
        return thread_link

    except Exception as e:
        logger.error(f"Error forwarding dialog: {e}", exc_info=True)
        return ""