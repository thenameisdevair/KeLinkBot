# bot.py — KeLinkBot v3.2  (grace window + strict interaction rule, event-loop safe)
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
load_dotenv(override=False)        # don't overwrite variables set by Railway
BOT_TOKEN  = os.getenv("BOT_TOKEN")                       # required
REDIS_URL  = os.getenv("REDIS_URL", "redis://localhost:6379")
redis_db   = aioredis.from_url(REDIS_URL, decode_responses=True)

TTL_12H   = 43_200                         # 12 hours in seconds
GRACE_KEY = "enforce_after"                # Redis key for grace-period end
LINK_RE   = re.compile(r"https?://", re.I)

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
    await redis_db.expire(f"post:{msg_id}:interacted", TTL_12H)

async def has_fulfilled_rule(uid: int) -> bool:
    """True if user has reacted/replied to every post in last 12 h (after grace)."""
    now = int(datetime.utcnow().timestamp())
    enforce_after = int(await redis_db.get(GRACE_KEY) or (now + TTL_12H))
    if now < enforce_after:
        return True  # grace window still active

    post_ids = await redis_db.zrangebyscore("posts_last12h", now - TTL_12H, now)
    for pid in post_ids:
        if await redis_db.get(f"post:{pid}:poster") == str(uid):
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

    # 2️⃣ strict interaction rule (after grace)
    if not await has_fulfilled_rule(uid):
        await msg.delete()
        await ctx.bot.send_message(
            uid,
            "👀 Before sharing, please react or reply to every link posted in the last 12 hours."
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
    await redis_db.setex(f"post:{wrapped.message_id}:poster", TTL_12H, uid)
    await mark_interaction(wrapped.message_id, uid)  # poster counts as interacted
    await redis_db.zadd(
        "posts_last12h",
        {wrapped.message_id: int(datetime.utcnow().timestamp())},
    )
    await redis_db.expire("posts_last12h", TTL_12H)

# ── one-off: start / reset grace window ──────────────────────────────────
def ensure_grace_window():
    async def _inner():
        if not await redis_db.exists(GRACE_KEY):
            enforce_ts = int(datetime.utcnow().timestamp()) + TTL_12H
            await redis_db.set(GRACE_KEY, enforce_ts)
    asyncio.run(_inner())  # runs once; closes immediately

# ── main (single, self-owned loop) ───────────────────────────────────────
def main():
    logging.basicConfig(level=logging.INFO)

    ensure_grace_window()             # 12-hour grace timer starts now

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(MessageReactionHandler(on_reaction))
    app.add_handler(MessageHandler(filters.REPLY, on_reply))
    app.add_handler(MessageHandler(filters.TEXT, on_link))

    # Create our own event loop (needed on Windows + Python 3.13)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        app.run_polling(
            allowed_updates=["message", "message_reaction"],
            close_loop=False,          # don't auto-close our custom loop
        )
    finally:
        loop.close()

if __name__ == "__main__":
    main()
