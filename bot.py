"""Telegram -> Google Calendar бот, рассчитанный на запуск в GitHub Actions.

Скрипт работает ~RUN_SECONDS секунд, получая сообщения через long polling,
затем завершается. Cron в GitHub Actions запускает его снова каждые 5 минут,
поэтому бот отвечает почти мгновенно. Сообщения, пришедшие в паузе между
запусками, Telegram хранит до 24 часов, так что ничего не теряется.

Почта календаря хранится в закреплённом сообщении в чате с ботом.
"""
import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from telegram import Bot

BOT_TOKEN = os.environ["BOT_TOKEN"]
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
RUN_SECONDS = int(os.getenv("RUN_SECONDS", "330"))
ALLOWED_IDS = {
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()
}

EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")
EVENT_RE = re.compile(
    r"^(\d{1,2})\.(\d{1,2})\.(\d{4})\s+"
    r"(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})\s+(.+)$",
    re.DOTALL,
)
PIN_PREFIX = "📧 Календарь: "

creds = service_account.Credentials.from_service_account_info(
    json.loads(os.environ["SERVICE_ACCOUNT_JSON"]),
    scopes=["https://www.googleapis.com/auth/calendar"],
)
SERVICE_EMAIL = creds.service_account_email
calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)


def insert_event(calendar_id, start, end, title):
    body = {
        "summary": title,
        "start": {"dateTime": start.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end.isoformat(), "timeZone": TIMEZONE},
    }
    event = calendar.events().insert(calendarId=calendar_id, body=body).execute()
    return event.get("htmlLink", "")


async def get_saved_email(bot: Bot, chat_id: int):
    chat = await bot.get_chat(chat_id)
    pinned = chat.pinned_message
    if pinned and pinned.text and pinned.text.startswith(PIN_PREFIX):
        return pinned.text[len(PIN_PREFIX):].strip()
    return None


async def handle_message(bot: Bot, msg):
    chat_id = msg.chat_id
    text = (msg.text or "").strip()
    if not text:
        return
    if ALLOWED_IDS and msg.from_user.id not in ALLOWED_IDS:
        return

    if text.startswith("/start") or text.startswith("/help"):
        await bot.send_message(
            chat_id,
            "Привет! Я создаю события в Google Календаре.\n\n"
            "1) Пришли свою Gmail-почту — я буду ставить события в её календарь.\n"
            f"   Дай доступ к календарю аккаунту:\n   {SERVICE_EMAIL}\n"
            "   (Настройки календаря → Доступ для отдельных пользователей → "
            "«Вносить изменения в мероприятия»).\n\n"
            "2) Присылай события так:\n"
            "   дд.мм.гггг чч:мм - чч:мм Название\n"
            "   Например: 15.11.2026 14:00 - 15:30 Встреча",
        )
        return

    if EMAIL_RE.match(text):
        email = text.lower()
        sent = await bot.send_message(
            chat_id,
            f"{PIN_PREFIX}{email}\n\n✅ Почта сохранена. Дай доступ к календарю "
            f"аккаунту {SERVICE_EMAIL} с правом «Вносить изменения в мероприятия».",
        )
        # Первая строка нужна только для парсинга, поэтому режем по переносу
        await bot.pin_chat_message(chat_id, sent.message_id, disable_notification=True)
        return

    m = EVENT_RE.match(text)
    if not m:
        await bot.send_message(
            chat_id,
            "Не понял формат. Нужно так:\nдд.мм.гггг чч:мм - чч:мм Название\n"
            "Или пришли почту, чтобы сменить календарь.",
        )
        return

    email = await get_saved_email(bot, chat_id)
    if email:
        email = email.split("\n")[0].strip()
    if not email:
        await bot.send_message(chat_id, "Сначала пришли мне почту, на которую ставить события.")
        return

    d, mo, y, h1, m1, h2, m2, title = m.groups()
    tz = ZoneInfo(TIMEZONE)
    try:
        start = datetime(int(y), int(mo), int(d), int(h1), int(m1), tzinfo=tz)
        end = datetime(int(y), int(mo), int(d), int(h2), int(m2), tzinfo=tz)
    except ValueError:
        await bot.send_message(chat_id, "Неверная дата или время. Проверь формат.")
        return
    if end <= start:
        end += timedelta(days=1)

    title = title.strip()
    try:
        link = await asyncio.to_thread(insert_event, email, start, end, title)
    except HttpError as e:
        status = getattr(e.resp, "status", None)
        reason = getattr(e, "reason", "") or str(e)
        await bot.send_message(
            chat_id,
            f"❌ Не удалось создать событие в календаре «{email}».\n"
            f"Код: {status}\nПричина Google: {reason}",
        )
        return

    await bot.send_message(
        chat_id,
        f"✅ Событие создано в {email}\n📌 {title}\n"
        f"🕒 {start:%d.%m.%Y %H:%M} – {end:%H:%M}\n{link}",
    )


async def main():
    deadline = time.monotonic() + RUN_SECONDS
    offset = None
    async with Bot(BOT_TOKEN) as bot:
        while True:
            remaining = int(deadline - time.monotonic())
            if remaining <= 2:
                break
            updates = await bot.get_updates(
                offset=offset,
                timeout=min(25, remaining - 1),
                allowed_updates=["message"],
            )
            for upd in updates:
                offset = upd.update_id + 1
                if upd.message:
                    try:
                        await handle_message(bot, upd.message)
                    except Exception as e:  # не даём одному сообщению уронить бота
                        print("Ошибка обработки:", repr(e))
        # подтверждаем последние обработанные сообщения
        if offset is not None:
            await bot.get_updates(offset=offset, timeout=0, limit=1)


if __name__ == "__main__":
    asyncio.run(main())
    
