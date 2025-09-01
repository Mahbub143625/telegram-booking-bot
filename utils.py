# utils.py
import os, calendar, re
from datetime import date
import pytz
from telegram import InlineKeyboardMarkup, InlineKeyboardButton

# --- Timezone ---
TZ = pytz.timezone(os.getenv("TZ", "Asia/Dhaka"))

# --- Time helpers used by bot.py ---
def parse_hhmm(txt: str):
    h, m = [int(x) for x in txt.split(":")]
    from datetime import time
    return time(hour=h, minute=m)

# --- Calendar keyboard (month grid with prev/next) ---
def month_keyboard(year: int, month: int, min_date: date, max_date: date) -> InlineKeyboardMarkup:
    cal = calendar.Calendar(firstweekday=6)  # Sunday
    header = [InlineKeyboardButton(f"{calendar.month_name[month]} {year}", callback_data="IGNORE")]
    week_names = [InlineKeyboardButton(d, callback_data="IGNORE") for d in ["S", "M", "T", "W", "T", "F", "S"]]
    rows = [header, week_names]

    for week in cal.monthdatescalendar(year, month):
        btns = []
        for d in week:
            if d.month != month or d < min_date or d > max_date:
                btns.append(InlineKeyboardButton(" ", callback_data="IGNORE"))
            else:
                btns.append(InlineKeyboardButton(str(d.day), callback_data=f"DATE:{d.isoformat()}"))
        rows.append(btns)

    # nav
    import datetime as _dt
    first = _dt.date(year, month, 1)
    prev = (first - _dt.timedelta(days=1)).replace(day=1)
    nxt_m_base = first.replace(day=28) + _dt.timedelta(days=4)
    nxt = nxt_m_base.replace(day=1)
    nav = [
        InlineKeyboardButton("«", callback_data=f"CAL:{prev.year}:{prev.month}"),
        InlineKeyboardButton("·", callback_data="IGNORE"),
        InlineKeyboardButton("»", callback_data=f"CAL:{nxt.year}:{nxt.month}")
    ]
    rows.append(nav)
    return InlineKeyboardMarkup(rows)

# --- Main menu ---
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Create Booking", callback_data="MENU:CREATE")],
        [InlineKeyboardButton("🧾 My Booked", callback_data="MENU:MY")],
        [InlineKeyboardButton("🔄 Restart", callback_data="MENU:RESTART")],
    ])

# --- Normalization for auto Q/A ---
_word_re = re.compile(r"[^\w\s]", re.UNICODE)
def normalize_text(s: str) -> str:
    return _word_re.sub(" ", (s or "").lower()).strip()
