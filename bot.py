"""Telegram -> Google Calendar бот для запуска в GitHub Actions.

Скрипт работает ~RUN_SECONDS секунд (long polling), затем завершается;
workflow запускает его снова. Между запусками состояние не хранится, поэтому:
- почта календаря лежит в закреплённом сообщении в чате;
- для «Изменить» id события зашит в служебное сообщение, на которое отвечает пользователь;
- для «Всё равно добавить» исходный текст берётся из сообщения, на которое отвечает бот.
"""
import asyncio
import json
import os
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from telegram import (
    Bot,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    ReplyParameters,
)
from telegram.error import NetworkError

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
EDIT_PREFIX = "✏️ Изменение события\n"
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

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
    "Если время окончания не указано, событие длится 1 час.\n\n"
    "Команды:\n"
    "/today — события на сегодня\n"
    "/week — события на 7 дней\n\n"
    "Если на выбранное время уже есть событие, бот предупредит.\n"
    "Кнопка «✏️ Изменить» меняет название и время события."
)


# ---------- Вспомогательное ----------

def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ)


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


def _patch_event(calendar_id, event_id, start, end, title):
    body = {
        "summary": title,
        "start": {"dateTime": start.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end.isoformat(), "timeZone": TIMEZONE},
    }
    return calendar.events().patch(
        calendarId=calendar_id, eventId=event_id, body=body
    ).execute()


def _delete(calendar_id, event_id):
    calendar.events().delete(calendarId=calendar_id, eventId=event_id).execute()


def _list(calendar_id, t0, t1, limit=50):
    res = calendar.events().list(
        calendarId=calendar_id,
        timeMin=_aware(t0).isoformat(),
        timeMax=_aware(t1).isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=limit,
    ).execute()
    return res.get("items", [])


def _conflicts(calendar_id, start, end):
    """События с конкретным временем, которые пересекаются с [start, end)."""
    out = []
    for ev in _list(calendar_id, start, end, limit=10):
        if ev.get("status") == "cancelled":
            continue
        if "dateTime" not in ev.get("start", {}):  # события на весь день не считаем
            continue
        if ev.get("transparency") == "transparent":  # «свободен»
            continue
        out.append(ev)
    return out


# ---------- Оформление сообщений ----------

def keyboard(event_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{event_id}"),
            InlineKeyboardButton("✏️ Изменить", callback_data=f"edit:{event_id}"),
        ],
        [
            InlineKeyboardButton("⏱ +1 час", callback_data=f"plus:{event_id}"),
            InlineKeyboardButton("📅 На завтра", callback_data=f"tmr:{event_id}"),
        ],
    ])


def conflict_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Всё равно добавить", callback_data="force"),
        InlineKeyboardButton("✖️ Отмена", callback_data="cancel"),
    ]])


def render(email, title, start, end, link) -> str:
    if start.date() == end.date():
        when = f"{start:%d.%m.%Y %H:%M} – {end:%H:%M}"
    else:
        when = f"{start:%d.%m.%Y %H:%M} – {end:%d.%m.%Y %H:%M}"
    return f"✅ {email}\n📌 {title}\n🕒 {when}\n{link}"


def render_conflicts(conflicts) -> str:
    lines = []
    for ev in conflicts:
        s = _dt(ev["start"]["dateTime"])
        e = _dt(ev["end"]["dateTime"])
        lines.append(f"• {s:%d.%m %H:%M}–{e:%H:%M} {ev.get('summary', '(без названия)')}")
    return "⚠️ На это время уже есть:\n" + "\n".join(lines) + "\n\nДобавить всё равно?"


def google_error_text(email, e: HttpError) -> str:
    status = getattr(e.resp, "status", None)
    reason = getattr(e, "reason", "") or str(e)
    return (
        f"❌ Не удалось обратиться к календарю «{email}».\n"
        f"Код: {status}\nПричина Google: {reason}"
    )


def parse_error_text(e: ValueError) -> str:
    problem = {
        "date": "Не понял дату.",
        "time": "Не понял время.",
        "range": "Некорректное время.",
    }.get(str(e), "Не понял сообщение.")
    return f"🤔 {problem}\n\n{HELP}"


async def get_email(bot: Bot, chat_id: int):
    chat = await bot.get_chat(chat_id)
    pinned = chat.pinned_message
    if pinned and pinned.text and pinned.text.startswith(PIN_PREFIX):
        return pinned.text[len(PIN_PREFIX):].split("\n")[0].strip()
    return None


# ---------- /today и /week ----------

async def send_agenda(bot: Bot, chat_id: int, days: int):
    email = await get_email(bot, chat_id)
    if not email:
        await bot.send_message(chat_id, "Сначала пришли мне почту, на которую ставить события.")
        return

    now = datetime.now(TZ)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day1 = day0 + timedelta(days=days)

    try:
        items = await asyncio.to_thread(_list, email, day0, day1)
    except HttpError as e:
        await bot.send_message(chat_id, google_error_text(email, e))
        return

    by_day = {}
    for ev in items:
        if ev.get("status") == "cancelled":
            continue
        title = ev.get("summary", "(без названия)")
        s = ev.get("start", {})
        if "dateTime" in s:
            st = _dt(s["dateTime"])
            en = _dt(ev["end"]["dateTime"])
            d = st.date()
            line = f"{st:%H:%M}–{en:%H:%M} {title}"
        else:
            d = date.fromisoformat(s["date"])
            line = f"весь день · {title}"
        d = max(d, day0.date())
        by_day.setdefault(d, []).append(line)

    if not by_day:
        text = "Сегодня событий нет 🎉" if days == 1 else "На ближайшие 7 дней событий нет 🎉"
    else:
        blocks = []
        for d in sorted(by_day):
            head = f"📅 {WEEKDAYS[d.weekday()]} {d:%d.%m}"
            if d == day0.date():
                head += " (сегодня)"
            blocks.append(head + "\n" + "\n".join(f"• {x}" for x in by_day[d]))
        text = "\n\n".join(blocks)

    await bot.send_message(chat_id, text[:4000])


# ---------- Создание события ----------

async def create_and_report(bot: Bot, chat_id, email, start, end, title):
    ev = await asyncio.to_thread(_insert, email, start, end, title)
    await bot.send_message(
        chat_id,
        render(email, title, start, end, ev.get("htmlLink", "")),
        reply_markup=keyboard(ev["id"]),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


# ---------- Сообщения ----------

async def handle_message(bot: Bot, msg):
    chat_id = msg.chat_id
    text = (msg.text or "").strip()
    if not text:
        return
    if ALLOWED_IDS and msg.from_user.id not in ALLOWED_IDS:
        return

    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""

    if cmd in ("/start", "/help"):
        await bot.send_message(
            chat_id,
            "Привет! Я создаю события в Google Календаре.\n\n"
            "1) Пришли свою Gmail-почту — я буду ставить события в её календарь.\n"
            f"   Дай доступ к календарю аккаунту:\n   {SERVICE_EMAIL}\n"
            "   (право «Изменять и просматривать все сведения о мероприятиях»).\n\n"
            "2) Присылай события обычным текстом.\n\n" + HELP,
        )
        return

    if cmd == "/today":
        await send_agenda(bot, chat_id, 1)
        return
    if cmd == "/week":
        await send_agenda(bot, chat_id, 7)
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
        await bot.send_message(chat_id, parse_error_text(e))
        return

    email = await get_email(bot, chat_id)
    if not email:
        await bot.send_message(chat_id, "Сначала пришли мне почту, на которую ставить события.")
        return

    # --- ответ на служебное сообщение «Изменение события» ---
    rt = msg.reply_to_message
    if rt and rt.text and rt.text.startswith(EDIT_PREFIX):
        m = re.search(r"^ID: (\S+)", rt.text, re.M)
        if not m:
            await bot.send_message(chat_id, "Не нашёл id события. Нажми «✏️ Изменить» ещё раз.")
            return
        event_id = m.group(1)
        try:
            ev = await asyncio.to_thread(_patch_event, email, event_id, start, end, title)
        except HttpError as e:
            await bot.send_message(chat_id, google_error_text(email, e))
            return
        await bot.send_message(
            chat_id,
            "✏️ Обновлено\n" + render(email, title, start, end, ev.get("htmlLink", "")),
            reply_markup=keyboard(event_id),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return

    # --- обычное создание: сначала проверяем пересечения ---
    try:
        conflicts = await asyncio.to_thread(_conflicts, email, start, end)
        if conflicts:
            await bot.send_message(
                chat_id,
                render_conflicts(conflicts),
                reply_markup=conflict_keyboard(),
                reply_parameters=ReplyParameters(message_id=msg.message_id),
            )
            return
        await create_and_report(bot, chat_id, email, start, end, title)
    except HttpError as e:
        await bot.send_message(chat_id, google_error_text(email, e))


# ---------- Нажатия на кнопки ----------

async def handle_callback(bot: Bot, q):
    if ALLOWED_IDS and q.from_user.id not in ALLOWED_IDS:
        await q.answer()
        return

    action, _, event_id = (q.data or "").partition(":")
    chat_id = q.message.chat_id

    if action == "cancel":
        await q.edit_message_text("✖️ Отменено")
        await q.answer()
        return

    email = await get_email(bot, chat_id)
    if not email:
        await q.answer("Не нашёл сохранённую почту. Пришли её боту заново.", show_alert=True)
        return

    try:
        if action == "force":
            src = q.message.reply_to_message
            if not src or not src.text:
                await q.answer("Не нашёл исходное сообщение. Отправь событие заново.", show_alert=True)
                return
            try:
                start, end, title = parse_event(src.text, datetime.now(TZ), DEFAULT_MINUTES)
            except ValueError:
                await q.answer("Не смог заново разобрать сообщение. Отправь событие заново.", show_alert=True)
                return
            ev = await asyncio.to_thread(_insert, email, start, end, title)
            await q.edit_message_text(
                render(email, title, start, end, ev.get("htmlLink", "")),
                reply_markup=keyboard(ev["id"]),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            await q.answer("Добавлено")
            return

        if action == "del":
            await asyncio.to_thread(_delete, email, event_id)
            await q.edit_message_text("🗑 Событие удалено")
            await q.answer("Удалено")
            return

        if action == "edit":
            await bot.send_message(
                chat_id,
                f"{EDIT_PREFIX}ID: {event_id}\n\n"
                "Ответь на это сообщение новым текстом события, например:\n"
                "15.11 14:00-15:30 Новое название",
                reply_markup=ForceReply(input_field_placeholder="15.11 14:00-15:30 Название"),
            )
            await q.answer()
            return

        ev = await asyncio.to_thread(_get, email, event_id)
        start = _dt(ev["start"]["dateTime"])
        end = _dt(ev["end"]["dateTime"])
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
            try:
                updates = await bot.get_updates(
                    offset=offset,
                    timeout=min(25, remaining - 1),
                    allowed_updates=["message", "callback_query"],
                )
            except NetworkError as e:
                print("Сетевая ошибка, повтор:", repr(e))
                await asyncio.sleep(3)
                continue
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
            try:
                await bot.get_updates(offset=offset, timeout=0, limit=1)
            except NetworkError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
