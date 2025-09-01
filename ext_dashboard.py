# ext_dashboard.py
import os
from datetime import datetime
from dotenv import load_dotenv, find_dotenv
from typing import List, Tuple

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
)

from db import conn_ctx, get_booking, now_tz
from utils import TZ

# Load .env once
p = find_dotenv(usecwd=True)
if p:
    load_dotenv(p)
else:
    load_dotenv(".env", override=True)

ADMIN_GROUP_ID = int(os.environ["ADMIN_GROUP_ID"])

# ---------- DB helpers for extension ----------

def ensure_ext_tables():
    with conn_ctx() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS booking_meta(
            booking_id INTEGER PRIMARY KEY,
            service_done INTEGER NOT NULL DEFAULT 0,
            user_reply_remaining INTEGER NOT NULL DEFAULT 0,
            admin_reply_remaining INTEGER NOT NULL DEFAULT 0
        );""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_reply_sessions(
            admin_id INTEGER PRIMARY KEY,
            booking_id INTEGER NOT NULL,
            remaining INTEGER NOT NULL,
            started_at TEXT NOT NULL DEFAULT (datetime('now'))
        );""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS rating_sessions(
            user_id INTEGER PRIMARY KEY,
            booking_id INTEGER NOT NULL,
            remaining INTEGER NOT NULL,
            started_at TEXT NOT NULL DEFAULT (datetime('now'))
        );""")
        conn.commit()

def _fmt_when(st_iso: str, en_iso: str) -> str:
    s = datetime.fromisoformat(st_iso).astimezone(TZ)
    e = datetime.fromisoformat(en_iso).astimezone(TZ)
    return f"{s:%d %b %Y, %I:%M %p} → {e:%I:%M %p}"

def _mins_until(st_iso: str) -> int:
    try:
        s = datetime.fromisoformat(st_iso).astimezone(TZ)
        delta = s - now_tz()
        return max(0, int(delta.total_seconds() // 60))
    except Exception:
        return 0

async def after_paid_announce(context: ContextTypes.DEFAULT_TYPE, booking_id: int):
    """Send token + details to admin group right after payment marked PAID."""
    with conn_ctx() as conn:
        row = conn.execute("""
            SELECT b.id, b.tg_user_id, b.user_full_name, u.username,
                   s.name, r.name, b.starts_at, b.ends_at, COALESCE(b.token,'-')
            FROM bookings b
            JOIN services s ON s.id=b.service_id
            JOIN resources r ON r.id=b.resource_id
            LEFT JOIN users u ON u.tg_user_id=b.tg_user_id
            WHERE b.id=? AND b.status='paid'
        """, (booking_id,)).fetchone()
    if not row:
        return
    bid, uid, full, uname, sname, rname, st, en, token = row
    text = (
        "✅ Booking Confirmed\n"
        f"ID: #{bid}\n"
        f"User: {full} (@{uname or 'n/a'}) [{uid}]\n"
        f"Service: {sname}\n"
        f"Resource: {rname}\n"
        f"Time: {_fmt_when(st, en)}\n"
        f"Token: {token}"
    )
    await context.bot.send_message(chat_id=ADMIN_GROUP_ID, text=text)
    with conn_ctx() as conn:
        conn.execute("""
        INSERT OR IGNORE INTO booking_meta(booking_id, service_done, user_reply_remaining, admin_reply_remaining)
        VALUES(?,0,0,0)
        """, (booking_id,))
        conn.commit()

# ---------- /listbooking UI ----------

PAGE_SIZE = 10

def _fetch_paid_bookings(offset: int = 0) -> List[Tuple]:
    with conn_ctx() as conn:
        rows = conn.execute("""
            SELECT b.id, b.user_full_name, COALESCE(b.token,'-'), b.starts_at, b.ends_at,
                   COALESCE(m.service_done,0)
            FROM bookings b
            LEFT JOIN booking_meta m ON m.booking_id=b.id
            WHERE b.status='paid'
            ORDER BY b.starts_at DESC
            LIMIT ? OFFSET ?
        """, (PAGE_SIZE, offset)).fetchall()
    return rows

def _count_paid() -> int:
    with conn_ctx() as conn:
        n = conn.execute("SELECT COUNT(*) FROM bookings WHERE status='paid'").fetchone()[0]
    return int(n)

def _table_lines(rows: List[Tuple]) -> List[str]:
    # columns: Name | Token | Avail(min) | Done
    lines = []
    header = f"{'User':20}  {'Token':10}  {'Avail(min)':10}  {'Done':5}"
    lines.append("```\n" + header)
    lines.append("-"*len(header))
    for bid, name, token, st, en, done in rows:
        avail = _mins_until(st)
        d = "YES" if int(done)==1 else "NO"
        nm = (name or "-")[:20].ljust(20)
        tk = (token or "-")[:10].ljust(10)
        av = str(avail).rjust(10)
        dn = d.rjust(5)
        lines.append(f"{nm}  {tk}  {av}  {dn}")
    lines.append("```")
    return lines

def _kb_for_page(rows: List[Tuple], page: int, total: int):
    buttons = []
    for bid, name, token, st, en, done in rows:
        dmark = "☑️" if int(done)==1 else "⬜️"
        row = [
            InlineKeyboardButton(f"{dmark} Done #{bid}", callback_data=f"BLIST:DONE:{bid}:{page}"),
            InlineKeyboardButton("💬 Reply", callback_data=f"BLIST:REPLY:{bid}:{page}")
        ]
        if _mins_until(st) == 0:  # overdue or now
            row.append(InlineKeyboardButton("🔁 Reschedule", callback_data=f"BLIST:RS:{bid}:{page}"))
        buttons.append(row)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅ Prev", callback_data=f"BLIST:PAGE:{page-1}"))
    if (page+1)*PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Next ➡", callback_data=f"BLIST:PAGE:{page+1}"))
    if nav:
        buttons.append(nav)
    return InlineKeyboardMarkup(buttons)

async def cmd_listbooking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_GROUP_ID:
        return
    total = _count_paid()
    rows = _fetch_paid_bookings(0)
    if not rows:
        await update.message.reply_text("No PAID bookings yet.")
        return
    txt = "\n".join(_table_lines(rows))
    kb = _kb_for_page(rows, 0, total)
    await update.message.reply_text(txt, reply_markup=kb, parse_mode="Markdown")

async def on_blist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.message.chat.id != ADMIN_GROUP_ID:
        return
    parts = q.data.split(":")
    _, act = parts[0], parts[1]
    if act == "PAGE":
        page = int(parts[2])
        total = _count_paid()
        rows = _fetch_paid_bookings(page*PAGE_SIZE)
        txt = "\n".join(_table_lines(rows))
        kb = _kb_for_page(rows, page, total)
        await q.edit_message_text(txt, reply_markup=kb, parse_mode="Markdown")
    elif act == "DONE":
        bid = int(parts[2]); page = int(parts[3])
        with conn_ctx() as conn:
            row = conn.execute(
                "SELECT COALESCE(service_done,0) FROM booking_meta WHERE booking_id=?",
                (bid,)
            ).fetchone()
            cur = int(row[0]) if row else 0
            newv = 0 if cur == 1 else 1
            conn.execute("INSERT OR IGNORE INTO booking_meta(booking_id, service_done) VALUES(?,0)", (bid,))
            conn.execute("UPDATE booking_meta SET service_done=? WHERE booking_id=?", (newv, bid))
            conn.commit()
        if newv == 1:
            b = get_booking(bid)
            if b:
                uid = b[5]
                await context.bot.send_message(
                    chat_id=uid,
                    text=("✅ Your service is completed.\n"
                          "Please share your feedback or rating (you can send up to 5 messages in this thread).")
                )
                with conn_ctx() as conn:
                    conn.execute("""
                    INSERT INTO rating_sessions(user_id, booking_id, remaining)
                    VALUES(?,?,5)
                    ON CONFLICT(user_id) DO UPDATE SET booking_id=excluded.booking_id, remaining=5
                    """, (uid, bid))
                    conn.commit()
                await q.message.reply_text(f"Opened rating window for user [{uid}] (limit: 5).")
        total = _count_paid()
        rows = _fetch_paid_bookings(page*PAGE_SIZE)
        txt = "\n".join(_table_lines(rows))
        kb = _kb_for_page(rows, page, total)
        await q.edit_message_text(txt, reply_markup=kb, parse_mode="Markdown")

    elif act == "REPLY":
        bid = int(parts[2]); page = int(parts[3])
        admin_id = update.effective_user.id
        with conn_ctx() as conn:
            conn.execute("""
            INSERT INTO admin_reply_sessions(admin_id, booking_id, remaining)
            VALUES(?,?,7)
            ON CONFLICT(admin_id) DO UPDATE SET booking_id=excluded.booking_id, remaining=7
            """, (admin_id, bid))
            conn.commit()
        await q.message.reply_text(f"➡ Reply mode ON for booking #{bid} (limit: 7). Type your message…")
    elif act == "RS":
        bid = int(parts[2]); page = int(parts[3])
        b = get_booking(bid)
        if not b:
            await q.message.reply_text("Booking not found.")
            return
        uid = b[5]
        await context.bot.send_message(
            chat_id=uid,
            text=(f"⏰ Your booking #{bid} time has passed or is due.\n"
                  "Please use /book to pick a new slot. Mention your previous token to the admin if needed.")
        )
        await q.message.reply_text(f"Reschedule instruction sent to user [{uid}].")

# ---------- relay handlers ----------

async def handle_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_GROUP_ID:
        return
    if not update.message or not update.message.text or update.message.text.startswith("/"):
        return
    admin_id = update.effective_user.id
    with conn_ctx() as conn:
        row = conn.execute(
            "SELECT booking_id, remaining FROM admin_reply_sessions WHERE admin_id=?",
            (admin_id,)
        ).fetchone()
    if not row:
        return
    bid, left = int(row[0]), int(row[1])
    if left <= 0:
        with conn_ctx() as conn:
            conn.execute("DELETE FROM admin_reply_sessions WHERE admin_id=?", (admin_id,))
            conn.commit()
        await update.message.reply_text("Reply limit is over. Tap Reply again from /listbooking.")
        return
    b = get_booking(bid)
    if not b:
        await update.message.reply_text("Booking not found.")
        return
    uid = b[5]
    await context.bot.send_message(chat_id=uid, text=update.message.text)
    with conn_ctx() as conn:
        conn.execute("UPDATE admin_reply_sessions SET remaining=remaining-1 WHERE admin_id=?", (admin_id,))
        conn.commit()
    if left-1 <= 0:
        await update.message.reply_text("✅ Reply limit reached for this session.")

async def handle_user_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not update.message or not update.message.text:
        return
    uid = update.effective_user.id
    with conn_ctx() as conn:
        row = conn.execute(
            "SELECT booking_id, remaining FROM rating_sessions WHERE user_id=?", (uid,)
        ).fetchone()
    if not row:
        return
    bid, left = int(row[0]), int(row[1])
    if left <= 0:
        with conn_ctx() as conn:
            conn.execute("DELETE FROM rating_sessions WHERE user_id=?", (uid,))
            conn.commit()
        return
    await context.bot.send_message(
        chat_id=ADMIN_GROUP_ID,
        text=(f"📝 Rating/Response for booking #{bid}\n"
              f"From user [{uid}]:\n{update.message.text}")
    )
    with conn_ctx() as conn:
        conn.execute("UPDATE rating_sessions SET remaining=remaining-1 WHERE user_id=?", (uid,))
        conn.commit()
    if left-1 <= 0:
        await context.bot.send_message(chat_id=uid, text="🙏 Thanks for your feedback. The session is now closed.")

def wire_dashboard(app: Application):
    ensure_ext_tables()
    app.add_handler(CommandHandler("listbooking", cmd_listbooking), group=0)
    app.add_handler(CallbackQueryHandler(on_blist, pattern=r"^BLIST:(PAGE|DONE|REPLY|RS):"), group=0)
    app.add_handler(MessageHandler(filters.Chat(ADMIN_GROUP_ID) & filters.TEXT & ~filters.COMMAND, handle_admin_reply), group=0)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_user_rating), group=0)
