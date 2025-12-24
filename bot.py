# bot.py
# Works around Python 3.14 + python-telegram-bot run_polling() issues
# by running application manually with asyncio.run().

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from pytz import timezone as tz
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

import config  # TELEGRAM_BOT_TOKEN, ADMIN_IDS, TIMEZONE, CHANNEL_ID

# ---------------- Logging ----------------
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------- Paths ----------------
BASE_DIR = Path(__file__).parent
SUBSCRIBERS_PATH = BASE_DIR / "subscribers.json"
POSTS_PATH = BASE_DIR / "advent_posts.json"
STATE_PATH = BASE_DIR / "state.json"

# ---------------- Models ----------------
@dataclass
class StoredMessage:
    from_chat_id: int
    message_id: int

# ---------------- Helpers ----------------
DATE_RE = re.compile(r"^#(\d{4}-\d{2}-\d{2})$")


def now_local() -> datetime:
    return datetime.now(tz(config.TIMEZONE))


def today_str() -> str:
    return now_local().date().strftime("%Y-%m-%d")


def extract_date_from_first_line(text: str) -> Optional[str]:
    if not text:
        return None
    first = text.strip().splitlines()[0].strip()
    m = DATE_RE.match(first)
    return m.group(1) if m else None


def is_admin(user_id: Optional[int]) -> bool:
    return bool(user_id) and user_id in getattr(config, "ADMIN_IDS", [])


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------- Storage: subscribers ----------------
def load_subscribers() -> List[int]:
    raw = load_json(SUBSCRIBERS_PATH, [])
    try:
        return [int(x) for x in raw]
    except Exception:
        return []


def save_subscribers(ids: List[int]):
    save_json(SUBSCRIBERS_PATH, sorted(set(ids)))


# ---------------- Storage: posts ----------------
def load_posts() -> Dict[str, List[StoredMessage]]:
    raw = load_json(POSTS_PATH, {})
    posts: Dict[str, List[StoredMessage]] = {}
    if not isinstance(raw, dict):
        return posts
    for day, items in raw.items():
        if not isinstance(items, list):
            continue
        parsed: List[StoredMessage] = []
        for m in items:
            try:
                parsed.append(StoredMessage(int(m["from_chat_id"]), int(m["message_id"])))
            except Exception:
                continue
        posts[str(day)] = parsed
    return posts


def save_posts(posts: Dict[str, List[StoredMessage]]):
    raw = {day: [m.__dict__ for m in msgs] for day, msgs in posts.items()}
    save_json(POSTS_PATH, raw)


def get_posts_for_day(day: str) -> List[StoredMessage]:
    return load_posts().get(day, [])


# ---------------- Storage: state (prevent duplicate daily sends) ----------------
def load_state() -> dict:
    raw = load_json(STATE_PATH, {})
    return raw if isinstance(raw, dict) else {}


def save_state(state: dict):
    save_json(STATE_PATH, state)


# ---------------- UI keyboard ----------------
def build_keyboard(is_subscribed: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎁 Открыть подарок сегодня", callback_data="open_today")],
            [
                InlineKeyboardButton(
                    "Отписаться" if is_subscribed else "Подписаться",
                    callback_data="unsubscribe" if is_subscribed else "subscribe",
                )
            ],
        ]
    )


# ---------------- Channel handler ----------------
async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg:
        return

    chat_id = msg.chat.id
    text = msg.text or msg.caption or ""

    # /debug_id inside channel
    if (msg.text or "").strip() == "/debug_id":
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "🧪 DEBUG CHANNEL INFO\n\n"
                f"chat_id = {chat_id}\n"
                f"chat_type = {msg.chat.type}\n"
                f"title = {msg.chat.title}\n"
            ),
        )
        return

    day = extract_date_from_first_line(text)
    if not day:
        return  # ignore posts without #YYYY-MM-DD in first line

    # If wrong channel configured - tell channel id
    if chat_id != getattr(config, "CHANNEL_ID", None):
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⚠️ Я вижу пост с датой, но этот канал не совпадает с config.CHANNEL_ID.\n"
                f"Текущий chat_id канала: {chat_id}\n"
                "Скопируй его в config.py как CHANNEL_ID и перезапусти бота."
            ),
        )
        return

    posts = load_posts()
    posts.setdefault(day, [])

    # avoid duplicates
    if any(m.message_id == msg.message_id and m.from_chat_id == chat_id for m in posts[day]):
        return

    posts[day].append(StoredMessage(from_chat_id=chat_id, message_id=msg.message_id))
    save_posts(posts)

    logger.info("Saved channel post for %s: channel_id=%s message_id=%s", day, chat_id, msg.message_id)
    await context.bot.send_message(chat_id=chat_id, text=f"✅ Saved for {day}")


# ---------------- User commands ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subs = load_subscribers()
    is_sub = chat_id in subs

    await update.message.reply_text(
        "Привет! 🎄 Ты попал к боту Угадай что выкину. C 26 декабря по 11 января каждый день будешь получать маленькие приятности!! держи подарок на сегодня"
        ,
        reply_markup=build_keyboard(is_sub),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msgs = get_posts_for_day(today_str())
    if not msgs:
        await update.message.reply_text("На сегодня пост не найден 🕰")
        return

    for m in msgs:
        await context.bot.copy_message(update.effective_chat.id, m.from_chat_id, m.message_id)


# ---------------- Buttons ----------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.message:
        return
    await q.answer()

    chat_id = q.message.chat.id
    subs = load_subscribers()
    is_sub = chat_id in subs

    if q.data == "subscribe":
        if not is_sub:
            subs.append(chat_id)
            save_subscribers(subs)
        await q.message.reply_text("✅ Ты подписан!")
        await q.message.reply_text("Меню:", reply_markup=build_keyboard(True))

    elif q.data == "unsubscribe":
        if is_sub:
            subs.remove(chat_id)
            save_subscribers(subs)
        await q.message.reply_text("💔 Ты отписан.")
        await q.message.reply_text("Меню:", reply_markup=build_keyboard(False))

    elif q.data == "open_today":
        msgs = get_posts_for_day(today_str())
        if not msgs:
            await q.message.reply_text("На сегодня пост не найден 🕰")
            return
        for m in msgs:
            await context.bot.copy_message(chat_id, m.from_chat_id, m.message_id)


# ---------------- Admin commands ----------------
async def admin_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    if not is_admin(user_id):
        await update.message.reply_text("Команда только для администратора.")
        return

    day = today_str()
    subs = load_subscribers()
    msgs = get_posts_for_day(day)
    state = load_state()

    await update.message.reply_text(
        "📊 STATUS\n\n"
        f"🕰 NOW_LOCAL: {now_local().strftime('%Y-%m-%d %H:%M:%S')} ({config.TIMEZONE})\n"
        f"📅 Today: {day}\n"
        f"👥 Subscribers: {len(subs)}\n"
        f"🎁 Posts today: {len(msgs)}\n"
        f"✅ Last sent: {state.get('last_sent')}\n"
        f"📌 CHANNEL_ID: {getattr(config, 'CHANNEL_ID', None)}"
    )


async def admin_broadcast_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    if not is_admin(user_id):
        await update.message.reply_text("Команда только для администратора.")
        return
    await daily_broadcast(context, force=True)
    await update.message.reply_text("✅ broadcast_now выполнен.")


# ---------------- Scheduler (run every minute) ----------------
async def daily_broadcast(context: ContextTypes.DEFAULT_TYPE, force: bool = False):
    now = now_local()
    today = today_str()
    state = load_state()

    if not force:
        # Only at 14:50 local time
        if now.hour != 14 or now.minute != 10:
            return
        # Only once per day
        if state.get("last_sent") == today:
            return

    msgs = get_posts_for_day(today)
    if not msgs:
        logger.info("No posts for today (%s). Skip broadcast.", today)
        return

    subs = load_subscribers()
    if not subs:
        logger.info("No subscribers. Skip broadcast.")
        return

    sent = 0
    for chat_id in subs:
        try:
            for m in msgs:
                await context.bot.copy_message(chat_id, m.from_chat_id, m.message_id)
            sent += 1
        except Exception as exc:
            logger.warning("Broadcast error to chat %s: %s", chat_id, exc)

    if not force:
        state["last_sent"] = today
        save_state(state)

    logger.info("Broadcast sent for %s to %s subscribers", today, sent)


# ---------------- Build app ----------------
def build_app():
    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()

    # handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", admin_status))
    app.add_handler(CommandHandler("broadcast_now", admin_broadcast_now))

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, channel_post_handler))

    # run every minute (jobqueue timezone not needed)
    app.job_queue.run_repeating(lambda ctx: daily_broadcast(ctx, force=False), interval=60, first=5)

    return app


# ---------------- Manual runner for Python 3.14 ----------------
async def async_main():
    app = build_app()

    await app.initialize()
    await app.start()

    # Start polling via updater (manual, avoids Application.run_polling() issues)
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    logger.info("Bot is running. NOW_LOCAL=%s", now_local().isoformat())

    # Idle forever, until Ctrl+C
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()

