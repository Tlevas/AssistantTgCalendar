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
