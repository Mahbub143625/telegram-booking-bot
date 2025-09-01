# bot.py
import os, logging, re
from datetime import datetime, timedelta, date
from dotenv import load_dotenv
from typing import Optional
from telegram import CallbackQuery

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
)
from telegram.constants import ChatType
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, ContextTypes, filters
)

from utils import TZ, parse_hhmm, month_keyboard, main_menu, normalize_text
from db import (
    init_db, now_tz, upsert_user, list_services, list_resources,
    get_service, get_resource, count_overlapping, create_pending_booking,
    mark_paid, cancel_booking, get_booking, user_bookings, list_bookings,
    get_kv, set_kv, add_autoqa, all_autoqa
)

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("booking-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_GROUP_ID = int(os.environ["ADMIN_GROUP_ID"])  # must be negative
BOOKING_DAYS_AHEAD = int(os.environ.get("BOOKING_DAYS_AHEAD", "30"))

WELCOME_DEFAULT = "Hello! 😊 How can I help with booking today? Try /menu."

# Conversation states for booking
SVC, RES, CAL, PICK_TIME, PAY_METHOD, PAY_REF = range(6)
# States for /setconversation (admin)
SETQA_KEYS, SETQA_ANSWER = range(6, 8)

PAY_METHODS = [("bKash", "bkash"), ("Nagad", "nagad"), ("Card", "card"), ("Cash", "cash")]

# ----------------- Utility: menu -----------------
async def send_welcome(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    welcome = get_kv("welcome_text", WELCOME_DEFAULT)
    await context.bot.send_message(chat_id=chat_id, text=welcome)
    await context.bot.send_message(chat_id=chat_id, text="Choose an option:", reply_markup=main_menu())

# ----------------- Commands -----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.full_name or "", u.username)
    await send_welcome(update.effective_chat.id, context)

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_welcome(update.effective_chat.id, context)

async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_welcome(update.effective_chat.id, context)

async def cmd_my(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    rows = user_bookings(u.id, limit=10)
    if not rows:
        await update.message.reply_text("You have no bookings yet.")
        return
    lines = []
    for bid, svc, res, st, en, status, token in rows:
        s = datetime.fromisoformat(st).astimezone(TZ)
        e = datetime.fromisoformat(en).astimezone(TZ)
        lines.append(f"#{bid} – {svc}/{res} – {s:%d %b %Y, %I:%M %p}-{e:%I:%M %p} – {status.upper()} – token: {token}")
    await update.message.reply_text("\n".join(lines))

# admin: change welcome text from group
async def cmd_setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_GROUP_ID:
        return
    args = (update.message.text or "").split(maxsplit=1)
    if len(args) == 1:
        await update.message.reply_text("Usage: /setwelcome Your welcome text (menu auto-attached).")
        return
    set_kv("welcome_text", args[1].strip())
    await update.message.reply_text("✅ Welcome text updated.")

# admin: list bookings in group with pagination
async def cmd_listbooking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_GROUP_ID:
        return
    await _send_booking_page(update.effective_chat.id, context, page=1)

async def on_list_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    _, page_str = q.data.split(":")
    await _send_booking_page(q.message.chat_id, context, page=int(page_str), edit_message=q)

async def _send_booking_page(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    page: int = 1,
    edit_message: Optional[CallbackQuery] = None,
):
    per_page = 10
    offset = (page - 1) * per_page
    rows = list_bookings(offset=offset, limit=per_page)
    if not rows and page > 1:
        page = 1
        offset = 0
        rows = list_bookings(offset=0, limit=per_page)

    if not rows:
        text = "No bookings yet."
    else:
        lines = ["#  Service/Resource  Time                       Status  Token  Amount"]
        for bid, svc, res, st, en, status, token, amount in rows:
            s = datetime.fromisoformat(st).astimezone(TZ)
            e = datetime.fromisoformat(en).astimezone(TZ)
            lines.append(f"{bid:<3}{svc}/{res}  {s:%d %b %Y %I:%M%p}-{e:%I:%M%p}  {status.upper():<7} {token:<8} {amount}")
        text = "```\n" + "\n".join(lines) + "\n```"

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("« Prev", callback_data=f"LIST:{max(1, page-1)}"),
        InlineKeyboardButton(f"Page {page}", callback_data="IGNORE"),
        InlineKeyboardButton("Next »", callback_data=f"LIST:{page+1}")
    ]])
    if edit_message:
        await edit_message.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode="Markdown")

# ----------------- Menu callback -----------------
async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    action = q.data.split(":")[1]
    if action == "CREATE":
        await cmd_book_from_menu(q, context)
    elif action == "MY":
        fake = Update(update.update_id, message=None)
        # quickly reuse cmd_my by simulating with chat id
        u = q.from_user
        rows = user_bookings(u.id, limit=10)
        if not rows:
            await q.edit_message_text("You have no bookings yet.")
        else:
            lines = []
            for bid, svc, res, st, en, status, token in rows:
                s = datetime.fromisoformat(st).astimezone(TZ)
                e = datetime.fromisoformat(en).astimezone(TZ)
                lines.append(f"#{bid} – {svc}/{res} – {s:%d %b %Y, %I:%M %p}-{e:%I:%M %p} – {status.upper()} – token: {token}")
            await q.edit_message_text("\n".join(lines), reply_markup=main_menu())
    else:
        await q.message.delete()
        await send_welcome(q.message.chat_id, context)

# ----------------- Booking flow -----------------
async def cmd_book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    svcs = list_services()
    if not svcs:
        await update.message.reply_text("No services available.")
        return ConversationHandler.END
    kb = [[InlineKeyboardButton(s[1], callback_data=f"SVC:{s[0]}")] for s in svcs]
    await update.message.reply_text("Select a service:", reply_markup=InlineKeyboardMarkup(kb))
    return SVC

async def cmd_book_from_menu(q, context):
    svcs = list_services()
    if not svcs:
        await q.edit_message_text("No services available.")
        return
    kb = [[InlineKeyboardButton(s[1], callback_data=f"SVC:{s[0]}")] for s in svcs]
    await q.edit_message_text("Select a service:", reply_markup=InlineKeyboardMarkup(kb))

async def on_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    svc_id = int(q.data.split(":")[1])
    context.user_data["svc_id"] = svc_id
    res_list = list_resources(svc_id)
    if not res_list:
        await q.edit_message_text("No resources for this service.")
        return ConversationHandler.END
    rows = [[InlineKeyboardButton(f"{r[1]} (cap {r[2]})", callback_data=f"RES:{r[0]}")] for r in res_list]
    await q.edit_message_text("Select a resource:", reply_markup=InlineKeyboardMarkup(rows))
    return RES

async def on_resource(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    rid = int(q.data.split(":")[1])
    context.user_data["res_id"] = rid

    today = now_tz().date()
    max_d = today + timedelta(days=BOOKING_DAYS_AHEAD)
    kb = month_keyboard(today.year, today.month, today, max_d)
    await q.edit_message_text("Choose a date:", reply_markup=kb)
    return CAL

async def on_calendar_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    _, y, m = q.data.split(":"); y = int(y); m = int(m)
    today = now_tz().date()
    max_d = today + timedelta(days=BOOKING_DAYS_AHEAD)
    kb = month_keyboard(y, m, today, max_d)
    await q.edit_message_reply_markup(reply_markup=kb)

async def on_date_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    d = date.fromisoformat(q.data.split(":")[1])
    context.user_data["date"] = d

    svc = get_service(context.user_data["svc_id"])  # id,name,dur,price,step
    res = get_resource(context.user_data["res_id"]) # id,svc_id,name,cap,open,close
    duration = int(svc[2]); price = int(svc[3]); step = int(svc[4])
    open_t = parse_hhmm(res[4]); close_t = parse_hhmm(res[5])

    start_dt = TZ.localize(datetime.combine(d, open_t))
    end_dt   = TZ.localize(datetime.combine(d, close_t))

    options = []
    cur = start_dt
    while cur + timedelta(minutes=duration) <= end_dt:
        s = cur
        e = cur + timedelta(minutes=duration)
        cnt = count_overlapping(res[0], s.isoformat(), e.isoformat())
        if cnt < int(res[3]):
            options.append((s, e))
        cur += timedelta(minutes=step)

    if not options:
        await q.edit_message_text("No available slots on this date. Pick another date:")
        today = now_tz().date(); max_d = today + timedelta(days=BOOKING_DAYS_AHEAD)
        kb = month_keyboard(d.year, d.month, today, max_d)
        await q.edit_message_reply_markup(reply_markup=kb)
        return CAL

    rows, row = [], []
    for i, (s, e) in enumerate(options, start=1):
        label = s.strftime("%I:%M %p")
        row.append(InlineKeyboardButton(label, callback_data=f"TIME:{s.isoformat()}|{e.isoformat()}"))
        if i % 4 == 0:
            rows.append(row); row = []
    if row: rows.append(row)
    await q.edit_message_text(f"Available times on {d:%d %b %Y}:", reply_markup=InlineKeyboardMarkup(rows))
    context.user_data["duration"] = duration
    context.user_data["amount"] = price
    return PICK_TIME

async def on_time_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    data = q.data.split(":")[1]
    if "|" not in data:
        await q.edit_message_text("Sorry, invalid time selection. Try /book again.")
        return ConversationHandler.END
    s_iso, e_iso = data.split("|")
    context.user_data["start_iso"] = s_iso
    context.user_data["end_iso"] = e_iso

    buttons = [[InlineKeyboardButton(t, callback_data=f"PM:{v}")] for t, v in PAY_METHODS]
    await q.edit_message_text(
        f"Fee: {context.user_data['amount']} ৳\nSelect payment method:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return PAY_METHOD

async def on_payment_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    method = q.data.split(":")[1]
    context.user_data["pay_method"] = method

    if method in ("bkash", "nagad"):
        prompt = "Please enter the Transaction ID (TXID)."
    elif method == "card":
        prompt = "Please enter the last 4 digits of the card."
    else:
        prompt = "We will verify cash in person. Type 'ok' to proceed."

    await q.edit_message_text(prompt)
    return PAY_REF

async def on_payment_ref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ref = (update.message.text or "").strip()
    method = context.user_data.get("pay_method")

    if method in ("bkash", "nagad"):
        if len(ref) < 6:
            await update.message.reply_text("Please provide a valid TXID.")
            return PAY_REF
    elif method == "card":
        if not (ref.isdigit() and len(ref) == 4):
            await update.message.reply_text("Enter exactly 4 digits.")
            return PAY_REF
    else:
        if ref.lower() not in ("ok", "okay", "done"):
            await update.message.reply_text("Type 'ok' to proceed.")
            return PAY_REF

    context.user_data["pay_ref"] = ref if method != "cash" else None

    # Create pending booking (hold)
    u = update.effective_user
    upsert_user(u.id, u.full_name or "", u.username)

    svc_id = context.user_data["svc_id"]
    res_id = context.user_data["res_id"]
    s_iso  = context.user_data["start_iso"]
    e_iso  = context.user_data["end_iso"]
    amount = int(context.user_data.get("amount", 0))

    bid = create_pending_booking(
        tg_user_id=u.id, user_full_name=u.full_name or "",
        service_id=svc_id, resource_id=res_id,
        starts_at_iso=s_iso, ends_at_iso=e_iso,
        amount=amount, payment_method=method,
        payment_ref=context.user_data.get("pay_ref"),
    )

    if not bid:
        await update.message.reply_text("Sorry, that slot just filled up. Please choose another time with /book.")
        return ConversationHandler.END

    svc = get_service(svc_id)
    res = get_resource(res_id)
    s = datetime.fromisoformat(s_iso).astimezone(TZ)
    e = datetime.fromisoformat(e_iso).astimezone(TZ)

    text = (
        "🆕 New Booking (Pending)\n"
        f"ID: #{bid}\n"
        f"User: {u.full_name} (@{u.username or 'n/a'}) [{u.id}]\n"
        f"Service: {svc[1]}\n"
        f"Resource: {res[2]} (cap {res[3]})\n"
        f"When: {s:%d %b %Y, %I:%M %p} → {e:%I:%M %p}\n"
        f"Amount: {amount} ৳\n"
        f"Method: {method}\n"
        f"Ref: {context.user_data.get('pay_ref') or '-'}\n"
    )
    opaque = os.urandom(3).hex()
    kb = [[
        InlineKeyboardButton("✅ Mark Paid", callback_data=f"ADMIN:PAID:{bid}:{opaque}"),
        InlineKeyboardButton("🛑 Cancel",    callback_data=f"ADMIN:CANCEL:{bid}:{opaque}")
    ]]
    await context.bot.send_message(chat_id=ADMIN_GROUP_ID, text=text, reply_markup=InlineKeyboardMarkup(kb))
    await update.message.reply_text("Your request was sent for verification. You'll receive confirmation soon.")
    return ConversationHandler.END

# Admin booking actions
async def on_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.message.chat_id != ADMIN_GROUP_ID:
        return
    _, action, bid_str, _ = q.data.split(":")
    bid = int(bid_str)
    if action == "PAID":
        token = os.urandom(4).hex().upper()
        ok = mark_paid(bid, token)
        b = get_booking(bid)
        if not b:
            await q.edit_message_text("Booking not found.")
            return
        if ok:
            s = datetime.fromisoformat(b[7]).astimezone(TZ)
            e = datetime.fromisoformat(b[8]).astimezone(TZ)
            await q.edit_message_text(q.message.text + "\n\n✔️ Marked as PAID")
            await context.bot.send_message(
                chat_id=b[5],
                text=(f"✅ Booking Confirmed\nToken: {token}\nService: {b[2]}\n"
                      f"Resource: {b[4]}\nTime: {s:%d %b %Y, %I:%M %p} → {e:%I:%M %p}")
            )
        else:
            await q.edit_message_text(q.message.text + "\n\n⚠️ Cannot mark paid (cancelled/expired?)")
    else:
        cancel_booking(bid)
        b = get_booking(bid)
        await q.edit_message_text(q.message.text + "\n\n❌ Cancelled.")
        if b:
            await context.bot.send_message(chat_id=b[5], text=f"Sorry, your booking #{bid} was cancelled.")

# ----------------- Auto conversation setup (admin) -----------------
async def cmd_setconversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_GROUP_ID:
        return
    await update.message.reply_text("🛠️ Conversation setup\nSend *question keywords* (comma-separated):\nExample: ki khobor, ki obostha",
                                    parse_mode="Markdown")
    return SETQA_KEYS

async def setqa_keys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["qa_keys"] = [normalize_text(x) for x in (update.message.text or "").split(",") if x.strip()]
    await update.message.reply_text("Great. Now send the *reply answer* (single message).", parse_mode="Markdown")
    return SETQA_ANSWER

async def setqa_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keys = context.user_data.get("qa_keys", [])
    answer = update.message.text or ""
    if not keys or not answer.strip():
        await update.message.reply_text("❗ Keys or answer missing. Start again: /setconversation")
        return ConversationHandler.END
    add_autoqa(keys, answer.strip())
    await update.message.reply_text("✅ Thanks! Conversation flow updated. I’ll auto-reply for those keywords.")
    return ConversationHandler.END

# ----------------- General inquiries: forward to group / auto-reply -----------------
def _group_reply_state():
    # single admin group; store session info in KV
    return get_kv("reply_session", None)

def _set_group_reply_state(user_id: int, remain: int = 3, minutes: int = 10):
    until = (now_tz() + timedelta(minutes=minutes)).timestamp()
    set_kv("reply_session", {"user_id": user_id, "remain": remain, "until": until})

async def _open_reply_mode(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    _set_group_reply_state(user_id, remain=3, minutes=10)
    await context.bot.send_message(
        chat_id=ADMIN_GROUP_ID,
        text=f"➡️ Reply mode ON for user [{user_id}] (limit: 3). Type your message…"
    )

def _reply_allowed_for(user_id: int) -> bool:
    sess = _group_reply_state()
    if not sess: return False
    if sess.get("user_id") != user_id: return False
    if now_tz().timestamp() > sess.get("until", 0): return False
    if int(sess.get("remain", 0)) <= 0: return False
    return True

def _consume_reply():
    sess = _group_reply_state()
    if not sess: return
    remain = int(sess.get("remain", 0)) - 1
    sess["remain"] = max(0, remain)
    set_kv("reply_session", sess)

async def on_user_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # only private chats from users
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    text = normalize_text(update.message.text or "")

    # 1) Auto-Q/A
    for _, patterns, answer in all_autoqa():
        if any(p and re.search(rf"\b{re.escape(p)}\b", text) for p in patterns):
            await update.message.reply_text(answer, reply_markup=main_menu())
            return

    # 2) Forward to group as General Inquiry
    u = update.effective_user
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💬 Reply", callback_data=f"GR:REPLY:{u.id}"),
        InlineKeyboardButton("🔕 Mute 10m", callback_data=f"GR:MUTE:{u.id}"),
        InlineKeyboardButton("⛔ Stop", callback_data=f"GR:STOP:{u.id}")
    ]])
    await context.bot.send_message(
        chat_id=ADMIN_GROUP_ID,
        text=(f"📨 General Inquiry\nFrom: {u.full_name}\n"
              f"(@{u.username or 'n/a'}) [{u.id}]\nMessage:\n{text or '(empty)'}"),
        reply_markup=kb
    )
    # polite default back to user
    await update.message.reply_text(get_kv("welcome_text", WELCOME_DEFAULT), reply_markup=main_menu())

async def on_user_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # forward photos also to group
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    u = update.effective_user
    photo = update.message.photo[-1].file_id if update.message.photo else None
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💬 Reply", callback_data=f"GR:REPLY:{u.id}"),
        InlineKeyboardButton("🔕 Mute 10m", callback_data=f"GR:MUTE:{u.id}"),
        InlineKeyboardButton("⛔ Stop", callback_data=f"GR:STOP:{u.id}")
    ]])
    if photo:
        await context.bot.send_photo(
            chat_id=ADMIN_GROUP_ID, photo=photo,
            caption=(f"🧾 General Inquiry (photo)\nFrom: {u.full_name} (@{u.username or 'n/a'}) [{u.id}]\n"
                     f"Caption:\n{update.message.caption or '(no caption)'}"),
            reply_markup=kb
        )

# Group button actions for general inquiries
async def on_group_reply_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.message.chat_id != ADMIN_GROUP_ID:
        return
    _, action, uid = q.data.split(":")
    uid = int(uid)
    if action == "REPLY":
        await _open_reply_mode(context, uid)
    elif action == "MUTE":
        # simple: just open reply mode but with 0 remain; effectively nothing will send
        set_kv("reply_session", {"user_id": uid, "remain": 0, "until": (now_tz()+timedelta(minutes=10)).timestamp()})
        await q.message.reply_text("🔕 Muted replies for 10 minutes for this user.")
    else:  # STOP
        set_kv("reply_session", None)
        await q.message.reply_text("🛑 Reply mode stopped.")

# Relay group messages to the target user while reply mode is on
async def on_group_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_GROUP_ID:
        return
    sess = _group_reply_state()
    if not sess: return
    if now_tz().timestamp() > sess.get("until", 0): return
    user_id = int(sess.get("user_id"))
    if sess.get("remain", 0) <= 0: return
    if update.message.text:
        await context.bot.send_message(chat_id=user_id, text=update.message.text)
        _consume_reply()

async def on_group_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_GROUP_ID:
        return
    sess = _group_reply_state()
    if not sess: return
    if now_tz().timestamp() > sess.get("until", 0): return
    if sess.get("remain", 0) <= 0: return
    user_id = int(sess.get("user_id"))
    if update.message.photo:
        await context.bot.copy_message(
            chat_id=user_id,
            from_chat_id=update.effective_chat.id,
            message_id=update.message.message_id
        )
        _consume_reply()

# ----------------- Wiring -----------------
def get_app():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # Booking conversation
    conv = ConversationHandler(
        entry_points=[CommandHandler("book", cmd_book)],
        states={
            SVC: [CallbackQueryHandler(on_service, pattern=r"^SVC:\d+$")],
            RES: [CallbackQueryHandler(on_resource, pattern=r"^RES:\d+$")],
            CAL: [
                CallbackQueryHandler(on_calendar_nav, pattern=r"^CAL:\d{4}:\d{1,2}$"),
                CallbackQueryHandler(on_date_picked,  pattern=r"^DATE:\d{4}-\d{2}-\d{2}$"),
            ],
            PICK_TIME: [CallbackQueryHandler(on_time_picked, pattern=r"^TIME:.*$")],
            PAY_METHOD: [CallbackQueryHandler(on_payment_method, pattern=r"^PM:(bkash|nagad|card|cash)$")],
            PAY_REF: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_payment_ref)],
        },
        fallbacks=[CommandHandler("book", cmd_book)],
        allow_reentry=True,
    )

    # Public commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu",  cmd_menu))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("my", cmd_my))

    # Admin commands (group only)
    app.add_handler(CommandHandler("setwelcome", cmd_setwelcome))
    app.add_handler(CommandHandler("listbooking", cmd_listbooking))
    app.add_handler(CallbackQueryHandler(on_list_nav, pattern=r"^LIST:\d+$"))

    # Auto-conversation setup (group)
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("setconversation", cmd_setconversation)],
        states={
            SETQA_KEYS: [MessageHandler(filters.Chat(ADMIN_GROUP_ID) & filters.TEXT, setqa_keys)],
            SETQA_ANSWER: [MessageHandler(filters.Chat(ADMIN_GROUP_ID) & filters.TEXT, setqa_answer)],
        },
        fallbacks=[],
        allow_reentry=True,
    ))

    # Menu callbacks
    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^MENU:(CREATE|MY|RESTART)$"))

    # Booking admin actions in group
    app.add_handler(CallbackQueryHandler(on_admin, pattern=r"^ADMIN:(PAID|CANCEL):\d+:"))

    # General inquiry group buttons and relay
    app.add_handler(CallbackQueryHandler(on_group_reply_buttons, pattern=r"^GR:(REPLY|MUTE|STOP):\d+$"))
    app.add_handler(MessageHandler(filters.Chat(ADMIN_GROUP_ID) & filters.PHOTO, on_group_photo))
    app.add_handler(MessageHandler(filters.Chat(ADMIN_GROUP_ID) & filters.TEXT & ~filters.COMMAND, on_group_text))

    # User generic handlers (MUST be after conversation so booking states work)
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, on_user_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_user_text))

    # Booking conversation last so it doesn’t swallow generic messages unnecessarily
    app.add_handler(conv)

    return app

if __name__ == "__main__":
    get_app().run_polling(allowed_updates=Update.ALL_TYPES)
