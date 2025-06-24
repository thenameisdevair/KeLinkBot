# bot.py — KeLinkBot v4.0 (strict 6-hour rule, no grace window)

import os, re, logging, asyncio
from datetime import datetime, timedelta, date
import redis.asyncio as aioredis
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    MessageReactionHandler,
    filters,
)

# ── configuration & Redis ────────────────────────────────────────────────
load_dotenv(override=False)                               # keep Render vars
BOT_TOKEN  = os.getenv("BOT_TOKEN")
REDIS_URL  = os.getenv("REDIS_URL")                       # must be set by host
redis_db   = aioredis.from_url(REDIS_URL, decode_responses=True)

TTL_LOOKBACK = 21_600      # 6 hours in seconds
LINK_RE       = re.compile(r"https?://", re.I)

# ── helper functions ────────────────────────────────────────────────────
def seconds_to_midnight() -> int:
    tomorrow = datetime.utcnow().date() + timedelta(days=1)
    return int(
        (datetime.combine(tomorrow, datetime.min.time()) - datetime.utcnow())
        .total_seconds()
    )

async def daily_count(uid: int) -> int:
    return int(await redis_db.get(f"cnt:{date.today()}:{uid}") or 0)

async def bump_daily_count(uid: int) -> int:
    key = f"cnt:{date.today()}:{uid}"
    cnt = int(await redis_db.get(key) or 0) + 1
    await redis_db.set(key, cnt, ex=seconds_to_midnight())
    return cnt

async def mark_interaction(msg_id: int, uid: int):
    await redis_db.sadd(f"post:{msg_id}:interacted", uid)
    await redis_db.expire(f"post:{msg_id}:interacted", TTL_LOOKBACK)

async def has_fulfilled_rule(uid: int) -> bool:
    """True if user has interacted with every wrapped post in last 6 h."""
    now = int(datetime.utcnow().timestamp())
    post_ids = await redis_db.zrangebyscore("posts_last6h", now - TTL_LOOKBACK, now)

    for pid in post_ids:
        poster = await redis_db.get(f"post:{pid}:poster")
        if poster == str(uid):
            continue  # skip their own posts
        if not await redis_db.sismember(f"post:{pid}:interacted", uid):
            return False
    return True

# ── handlers ────────────────────────────────────────────────────────────
async def on_reaction(update: Update, _):
    if update.message_reaction and update.message_reaction.user:
        await mark_interaction(update.message_reaction.message_id,
                               update.message_reaction.user.id)

async def on_reply(update: Update, _):
    if update.message and update.message.reply_to_message:
        await mark_interaction(update.message.reply_to_message.message_id,
                               update.message.from_user.id)

async def on_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not LINK_RE.search(msg.text or ""):
        return
    uid = msg.from_user.id

    # 1️⃣ daily quota
    if await daily_count(uid) >= 3:
        await msg.delete()
        await ctx.bot.send_message(
            uid, "🚫 You have already shared 3 links today. Try again after 00:00 UTC."
        )
        return

    # 2️⃣ strict 6-hour interaction rule
    if not await has_fulfilled_rule(uid):
        await msg.delete()
        await ctx.bot.send_message(
            uid,
            "👀 Before sharing, please react or reply to every link posted in the last 6 hours."
        )
        return

    # 3️⃣ accepted → wrap and repost
    count = await bump_daily_count(uid)
    wrapped_text = f"🔗 {msg.from_user.mention_html()} shared a link ({count}/3 today)"
    button = InlineKeyboardButton("Open link 🔗", url=msg.text.strip())
    wrapped = await ctx.bot.send_message(
        msg.chat_id,
        wrapped_text,
        reply_markup=InlineKeyboardMarkup([[button]]),
        parse_mode="HTML",
    )
    await msg.delete()

    # 4️⃣ store metadata & interaction sets
    await redis_db.setex(f"post:{wrapped.message_id}:poster", TTL_LOOKBACK, uid)
    await mark_interaction(wrapped.message_id, uid)  # poster counts as interacted
    await redis_db.zadd(
        "posts_last6h",
        {wrapped.message_id: int(datetime.utcnow().timestamp())},
    )
    await redis_db.expire("posts_last6h", TTL_LOOKBACK)

# ── main (single, self-owned loop) ───────────────────────────────────────
def main():
    logging.basicConfig(level=logging.INFO)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(MessageReactionHandler(on_reaction))
    app.add_handler(MessageHandler(filters.REPLY, on_reply))
    app.add_handler(MessageHandler(filters.TEXT, on_link))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        app.run_polling(
            allowed_updates=["message", "message_reaction"],
            close_loop=False,          # keep our loop open
        )
    finally:
        loop.close()

if __name__ == "__main__":
    main()
