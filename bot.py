"""Telegram -> Google Calendar бот для запуска в GitHub Actions.

Скрипт работает ~RUN_SECONDS секунд (long polling), затем завершается;
cron в GitHub Actions запускает его снова каждые 5 минут.
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
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from dateparse import parse_event

BOT_TOKEN = os.environ["BOT_TOKEN"]
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
TZ = ZoneInfo(TIMEZONE)
RUN_SECONDS = int(os.getenv("RUN_SECONDS", "330"))
DEFAULT_MINUTES = int(os.getenv("DEFAULT_MINUTES", "60"))
ALLOWED_IDS = {
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()
}

EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")
PIN_PREFIX = "📧 Календарь: "

creds = service_account.Credentials.from_service_account_info(
    json.loads(os.environ["SERVICE_ACCOUNT_JSON"]),
    scopes=["https://www.googleapis.com/auth/calendar"],
)
SERVICE_EMAIL = creds.service_account_email
calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)

HELP = (
    "Примеры сообщений:\n"
    "• завтра 15:00 Стоматолог\n"
    "• в пятницу 18:30-20:00 Тренировка\n"
    "• сегодня в 9 Планёрка\n"
    "• через 3 дня в 10 Звонок маме\n"
    "• 15.11 14:00-15:30 Встреча\n"
    "• 15.11.2026 14:00 - 15:30 Встреча\n\n"
    "Если время окончания не указано, событие длится 1 час."
)


# ---------- Google Calendar ----------

def _insert(calendar_id, start, end, title):
    body = {
        "summary": title,
        "start": {"dateTime": start.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end.isoformat(), "timeZone": TIMEZONE},
    }
    return calendar.events().insert(calendarId=calendar_id, body=body).execute()


def _get(calendar_id, event_id):
    return calendar.events().get(calendarId=calendar_id, eventId=event_id).execute()


def _patch_times(calendar_id, event_id, start, end):
    body = {
        "start": {"dateTime": start.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end.isoformat(), "timeZone": TIMEZONE},
    }
    return calendar.events().patch(
        calendarId=calendar_id, eventId=event_id, body=body
    ).execute()


def _delete(calendar_id, event_id):
    calendar.events().delete(calendarId=calendar_id, eventId=event_id).execute()


# ---------- Оформление сообщений ----------

def keyboard(event_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{event_id}"),
        InlineKeyboardButton("⏱ +1 час", callback_data=f"plus:{event_id}"),
        InlineKeyboardButton("📅 На завтра", callback_data=f"tmr:{event_id}"),
    ]])


def render(email, title, start, end, link) -> str:
    if start.date() == end.date():
        when = f"{start:%d.%m.%Y %H:%M} – {end:%H:%M}"
    else:
        when = f"{start:%d.%m.%Y %H:%M} – {end:%d.%m.%Y %H:%M}"
    return f"✅ {email}\n📌 {title}\n🕒 {when}\n{link}"


async def get_email(bot: Bot, chat_id: int):
    chat = await bot.get_chat(chat_id)
    pinned = chat.pinned_message
    if pinned and pinned.text and pinned.text.startswith(PIN_PREFIX):
        return pinned.text[len(PIN_PREFIX):].split("\n")[0].strip()
    return None


# ---------- Сообщения ----------

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
            "   (право «Изменять и просматривать все сведения о мероприятиях»).\n\n"
            "2) Присылай события обычным текстом.\n\n" + HELP,
        )
        return

    if EMAIL_RE.match(text):
        email = text.lower()
        sent = await bot.send_message(
            chat_id,
            f"{PIN_PREFIX}{email}\n\n✅ Почта сохранена. Дай доступ к календарю "
            f"аккаунту {SERVICE_EMAIL} с правом «Изменять и просматривать все "
            "сведения о мероприятиях».",
        )
        await bot.pin_chat_message(chat_id, sent.message_id, disable_notification=True)
        return

    try:
        start, end, title = parse_event(text, datetime.now(TZ), DEFAULT_MINUTES)
    except ValueError as e:
        problem = {
            "date": "Не понял дату.",
            "time": "Не понял время.",
            "range": "Некорректное время.",
        }.get(str(e), "Не понял сообщение.")
        await bot.send_message(chat_id, f"🤔 {problem}\n\n{HELP}")
        return

    email = await get_email(bot, chat_id)
    if not email:
        await bot.send_message(chat_id, "Сначала пришли мне почту, на которую ставить события.")
        return

    try:
        ev = await asyncio.to_thread(_insert, email, start, end, title)
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
        render(email, title, start, end, ev.get("htmlLink", "")),
        reply_markup=keyboard(ev["id"]),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


# ---------- Нажатия на кнопки ----------

async def handle_callback(bot: Bot, q):
    if ALLOWED_IDS and q.from_user.id not in ALLOWED_IDS:
        await q.answer()
        return

    action, _, event_id = (q.data or "").partition(":")
    email = await get_email(bot, q.message.chat_id)
    if not email:
        await q.answer("Не нашёл сохранённую почту. Пришли её боту заново.", show_alert=True)
        return

    try:
        if action == "del":
            await asyncio.to_thread(_delete, email, event_id)
            await q.edit_message_text("🗑 Событие удалено")
            await q.answer("Удалено")
            return

        ev = await asyncio.to_thread(_get, email, event_id)
        start = datetime.fromisoformat(ev["start"]["dateTime"]).astimezone(TZ)
        end = datetime.fromisoformat(ev["end"]["dateTime"]).astimezone(TZ)
        if action == "plus":
            end += timedelta(hours=1)
        elif action == "tmr":
            start += timedelta(days=1)
            end += timedelta(days=1)
        else:
            await q.answer()
            return

        ev = await asyncio.to_thread(_patch_times, email, event_id, start, end)
        await q.edit_message_text(
            render(email, ev.get("summary", ""), start, end, ev.get("htmlLink", "")),
            reply_markup=keyboard(event_id),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        await q.answer("Готово")
    except HttpError as e:
        reason = getattr(e, "reason", "") or str(e)
        await q.answer(f"Ошибка Google: {reason}"[:190], show_alert=True)


# ---------- Основной цикл ----------

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
                allowed_updates=["message", "callback_query"],
            )
            for upd in updates:
                offset = upd.update_id + 1
                try:
                    if upd.message:
                        await handle_message(bot, upd.message)
                    elif upd.callback_query:
                        await handle_callback(bot, upd.callback_query)
                except Exception as e:  # одно сообщение не должно ронять бота
                    print("Ошибка обработки:", repr(e))
        if offset is not None:
            await bot.get_updates(offset=offset, timeout=0, limit=1)


if __name__ == "__main__":
    asyncio.run(main())
    
