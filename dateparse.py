"""Разбор сообщений вида «завтра 15:00-16:30 Название» без внешних API."""
import re
from datetime import date, datetime, time, timedelta

WEEKDAYS = {
    "понедельник": 0, "пн": 0,
    "вторник": 1, "вт": 1,
    "среда": 2, "среду": 2, "ср": 2,
    "четверг": 3, "чт": 3,
    "пятница": 4, "пятницу": 4, "пт": 4,
    "суббота": 5, "субботу": 5, "сб": 5,
    "воскресенье": 6, "вс": 6,
}
_WD = "|".join(sorted(WEEKDAYS, key=len, reverse=True))

_NUM_DATE = re.compile(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}|\d{2}))?(?![\d:])")
_DAY_WORD = re.compile(r"(сегодня|завтра|послезавтра)(?![а-яё])", re.I)
_THROUGH = re.compile(r"через\s+(?:(\d+)\s+)?(дн\w*|день|недел\w*)", re.I)
_WEEKDAY = re.compile(
    rf"(?:(?:в|во|на)\s+)?(?:(?:следующ\w+|эт\w+)\s+)?({_WD})(?![а-яё])", re.I
)

_RANGE = re.compile(
    r"(?:(?:в|с)\s+)?(\d{1,2})(?::(\d{2}))?\s*(?:[-–—]|до)\s*(\d{1,2})(?::(\d{2}))?(?![\d:.])",
    re.I,
)
_SINGLE = re.compile(r"(?:(?:в|с)\s+)?(\d{1,2}):(\d{2})(?![\d])", re.I)
_HOUR = re.compile(
    r"(?:в|на)\s+(\d{1,2})(?![\d:.])(?:\s+час(?:ов|а)?(?![а-яё]))?", re.I
)


def _parse_date(s: str, today: date):
    s = s.lstrip()

    m = _NUM_DATE.match(s)
    if m:
        d, mo, y = int(m[1]), int(m[2]), m[3]
        try:
            if y:
                year = int(y) + (2000 if len(y) == 2 else 0)
                return date(year, mo, d), s[m.end():]
            result = date(today.year, mo, d)
            if result < today:
                result = date(today.year + 1, mo, d)
            return result, s[m.end():]
        except ValueError:
            return None

    m = _DAY_WORD.match(s)
    if m:
        shift = {"сегодня": 0, "завтра": 1, "послезавтра": 2}[m[1].lower()]
        return today + timedelta(days=shift), s[m.end():]

    m = _THROUGH.match(s)
    if m:
        n = int(m[1]) if m[1] else 1
        days = n * 7 if m[2].lower().startswith("нед") else n
        return today + timedelta(days=days), s[m.end():]

    m = _WEEKDAY.match(s)
    if m:
        wd = WEEKDAYS[m[1].lower()]
        delta = (wd - today.weekday()) % 7 or 7  # то же число — на следующей неделе
        return today + timedelta(days=delta), s[m.end():]

    return None


def _parse_time(s: str):
    s = s.lstrip()
    m = _RANGE.match(s)
    if m:
        h1, m1 = int(m[1]), int(m[2] or 0)
        h2, m2 = int(m[3]), int(m[4] or 0)
        return (h1, m1, h2, m2), s[m.end():]
    m = _SINGLE.match(s)
    if m:
        return (int(m[1]), int(m[2]), None, None), s[m.end():]
    m = _HOUR.match(s)
    if m:
        return (int(m[1]), 0, None, None), s[m.end():]
    return None


def parse_event(text: str, now: datetime, default_minutes: int = 60):
    """Возвращает (start, end, title). При ошибке — ValueError('date'|'time'|'range')."""
    tz = now.tzinfo
    parsed = _parse_date(text.strip(), now.date())
    if not parsed:
        raise ValueError("date")
    day, rest = parsed

    parsed = _parse_time(rest)
    if not parsed:
        raise ValueError("time")
    (h1, m1, h2, m2), rest = parsed

    try:
        start = datetime.combine(day, time(h1, m1), tzinfo=tz)
        if h2 is None:
            end = start + timedelta(minutes=default_minutes)
        else:
            end = datetime.combine(day, time(h2, m2), tzinfo=tz)
            if end <= start:
                end += timedelta(days=1)  # событие через полночь
    except ValueError:
        raise ValueError("range")

    title = rest.strip().lstrip("-–—:,. ").strip() or "Событие"
    return start, end, title
