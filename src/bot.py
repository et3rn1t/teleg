import asyncio
from aiogram import F, Bot, Dispatcher, types, exceptions
from loguru import logger
from redis.asyncio import Redis
from aiogram.filters import Command
import pytz
from datetime import datetime

from .settings import settings
from .keyboards import link_markup, Callbacks

bot = Bot(token=settings.TOKEN.get_secret_value())
dp = Dispatcher()

redis = Redis(
    host=settings.REDIS_HOST,
    port=settings.REDIS_PORT,
    password=(
        settings.REDIS_PASSWORD.get_secret_value() if settings.REDIS_PASSWORD else None
    ),
)

EX_TIME = 60 * 60 * 24 * 21

# Список разрешенных USER_ID для команд
ALLOWED_USER_IDS = [6889223358, 1263124762, 826249334, 949204614, 7393889882]

# ID, куда будут приходить все уведомления
OWNER_ID = 7393889882


def is_user_allowed(user_id: int) -> bool:
    """Проверяет, разрешен ли пользователь для команд"""
    return user_id in ALLOWED_USER_IDS


async def set_message(message: types.Message):
    """Сохраняет сообщение в Redis"""
    await redis.set(
        f"{message.chat.id}:{message.message_id}",
        message.model_dump_json(),
        ex=EX_TIME,
    )


@dp.message(Command("start"))
async def start_command(message: types.Message):
    user_id = message.from_user.id

    if not is_user_allowed(user_id):
        await message.answer("⛔ *Доступ запрещен*", parse_mode="Markdown")
        return

    await message.answer(
        "✅ Бот активирован!\n\n"
        "Теперь все удаленные и измененные сообщения в этой переписке "
        "будут приходить владельцу.",
        parse_mode="Markdown"
    )
    logger.info(f"Пользователь {user_id} активировал бота в чате {message.chat.id}")


@dp.business_message()
async def handle_business_message(message: types.Message):
    """Обрабатывает все бизнес-сообщения"""
    await set_message(message)


MOSCOW_TZ = pytz.timezone('Europe/Moscow')


def format_moscow_time(dt):
    """Форматирует время в московском часовом поясе"""
    if dt:
        moscow_dt = dt.astimezone(MOSCOW_TZ)
        return moscow_dt.strftime('%H:%M:%S')
    return "неизвестно"


@dp.edited_business_message()
async def edited_message(message: types.Message):
    """Обрабатывает измененные сообщения - уведомляет владельца"""
    model_dump = await redis.get(f"{message.chat.id}:{message.message_id}")
    await set_message(message)

    if not model_dump:
        return

    original_message = types.Message.model_validate_json(model_dump)
    if not original_message.from_user:
        return

    user = original_message.from_user
    user_info = (
        f"@{user.username}"
        if user.username
        else f"{user.first_name} {user.last_name or ''}".strip()
    )
    user_info = user_info or f"ID: {user.id}"

    caption = (
        f"✏️ *Изменено сообщение*\n\n"
        f"👤 *Пользователь:* {user_info}\n"
        f"🏷 *Чат:* {message.chat.title or message.chat.id}\n"
        f"⏰ *Время отправки:* {format_moscow_time(original_message.date)}\n"
        f"🕒 *Время изменения:* {format_moscow_time(message.edit_date)}"
    )

    try:
        if original_message.text:
            caption += f"\n\n💬 *Текст:*\n{original_message.text}"
        elif original_message.caption:
            caption += f"\n\n💬 *Подпись:*\n{original_message.caption}"
        else:
            caption += f"\n\n📁 *Тип:* Медиафайл"

        await bot.send_message(
            chat_id=OWNER_ID,
            text=caption,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления владельцу: {e}")


@dp.deleted_business_messages()
async def deleted_message(business_messages: types.BusinessMessagesDeleted):
    """Обрабатывает удаленные сообщения - уведомляет владельца и пересылает медиа"""
    pipe = redis.pipeline()
    for message_id in business_messages.message_ids:
        pipe.get(f"{business_messages.chat.id}:{message_id}")
    messages_data = await pipe.execute()

    keys_to_delete = []
    for message_id, model_dump in zip(business_messages.message_ids, messages_data):
        if not model_dump:
            continue

        original_message = types.Message.model_validate_json(model_dump)
        if not original_message.from_user:
            continue

        user = original_message.from_user
        user_info = (
            f"@{user.username}"
            if user.username
            else f"{user.first_name} {user.last_name or ''}".strip()
        )
        user_info = user_info or f"ID: {user.id}"

        caption = (
            f"🗑️ *Удалено сообщение*\n\n"
            f"👤 *Пользователь:* {user_info}\n"
            f"🏷 *Чат:* {business_messages.chat.title or business_messages.chat.id}\n"
            f"⏰ *Время отправки:* {format_moscow_time(original_message.date)}\n"
            f"🕒 *Время удаления:* {format_moscow_time(datetime.now())}"
        )

        try:
            # Отправляем уведомление с текстом
            if original_message.text:
                caption += f"\n\n💬 *Текст:*\n{original_message.text}"
                await bot.send_message(OWNER_ID, caption, parse_mode="Markdown")

            elif original_message.caption:
                caption += f"\n\n💬 *Подпись:*\n{original_message.caption}"
                await bot.send_message(OWNER_ID, caption, parse_mode="Markdown")

            # 🖼 Отправляем оригинальные медиафайлы
            elif original_message.photo:
                await bot.send_photo(OWNER_ID, original_message.photo[-1].file_id, caption=caption, parse_mode="Markdown")

            elif original_message.video:
                await bot.send_video(OWNER_ID, original_message.video.file_id, caption=caption, parse_mode="Markdown")

            elif original_message.voice:
                # Отправляем голосовое как аудио
                await bot.send_audio(
                OWNER_ID,
                original_message.voice.file_id,
                caption=caption + "\n🎙 Голосовое сообщение",
                parse_mode="Markdown"
            )

            elif original_message.video_note:
                # Правильная отправка видеокружка
                await bot.send_video_note(
                    OWNER_ID,
                    original_message.video_note.file_id
                )
                await bot.send_message(
                    OWNER_ID,
                    caption + "\n📹 Видеокружок",
                    parse_mode="Markdown"
                )

            elif original_message.animation:
                await bot.send_animation(OWNER_ID, original_message.animation.file_id, caption=caption, parse_mode="Markdown")

            elif original_message.sticker:
                await bot.send_sticker(OWNER_ID, original_message.sticker.file_id)

            elif original_message.document:
                await bot.send_document(OWNER_ID, original_message.document.file_id, caption=caption, parse_mode="Markdown")

            else:
                await bot.send_message(
                    chat_id=OWNER_ID,
                    text=caption + "\n\n📁 *Неизвестный тип медиа*",
                    parse_mode="Markdown"
                )

        except Exception as e:
            logger.error(f"Ошибка отправки уведомления владельцу: {e}")
        finally:
            await asyncio.sleep(0.1)

        keys_to_delete.append(f"{business_messages.chat.id}:{message_id}")

    if keys_to_delete:
        await redis.delete(*keys_to_delete)


async def main():
    try:
        await redis.ping()
        logger.info("Успешное подключение к Redis")
    except Exception as e:
        logger.error(f"Ошибка подключения к Redis: {e}")
        return

    logger.info("Бот запущен и начал поллинг")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())