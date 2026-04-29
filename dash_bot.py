"""
Production-ready Telegram Bot for DASH Address Tracking
Requirements: python-telegram-bot>=20.0, aiohttp, aiosqlite
Install: pip install "python-telegram-bot>=20.0" aiohttp aiosqlite
"""

import asyncio
import logging
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import aiosqlite
from telegram import (
    Bot,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ─────────────────────────── CONFIGURATION ────────────────────────────────────

BOT_TOKEN = "8555649605:AAFGw0uEClBfB0IstQN_FBySRw8INHp7MnM"  # Replace with your actual token
DATABASE_PATH = "dash_bot.db"
DASH_API_BASE = "https://insight.dash.org/insight-api"
POLLING_INTERVAL = 15  # seconds between blockchain checks
PRICE_CACHE_TTL = 15  # seconds to cache DASH/USD price

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────── PRICE CACHE ──────────────────────────────────────

_price_cache: dict = {"price": None, "fetched_at": 0.0}


async def get_dash_usd_price(session: aiohttp.ClientSession) -> Optional[float]:
    """
    Fetch current DASH/USD price from CoinGecko public API.
    Result is cached for PRICE_CACHE_TTL seconds to avoid hammering the API.
    Returns None if the request fails (caller should handle gracefully).
    """
    now = time.time()
    if _price_cache["price"] is not None and (now - _price_cache["fetched_at"]) < PRICE_CACHE_TTL:
        return _price_cache["price"]

    url = "https://api.coingecko.com/api/v3/simple/price?ids=dash&vs_currencies=usd"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                logger.warning("CoinGecko price API returned %d", resp.status)
                return _price_cache["price"]  # return stale value if available
            data = await resp.json()
            price = float(data["dash"]["usd"])
            _price_cache["price"] = price
            _price_cache["fetched_at"] = now
            logger.info("DASH/USD price updated: $%.4f", price)
            return price
    except Exception as exc:
        logger.error("Failed to fetch DASH/USD price: %s", exc)
        return _price_cache["price"]  # return stale value on error


def dash_to_usd(amount_dash: float, price_usd: Optional[float]) -> str:
    """Format DASH amount as USD string. Falls back to DASH display if price unavailable."""
    if price_usd is None:
        return f"{amount_dash:.8f} DASH"
    usd = amount_dash * price_usd
    return f"${usd:,.2f}"


# ─────────────────────────── CONVERSATION STATES ──────────────────────────────

WAITING_ADDRESS = 1

# ─────────────────────────── TRANSLATIONS ─────────────────────────────────────

TEXTS = {
    "en": {
        "welcome": "👋 Welcome to DASH Address Tracker!\nUse the menu below:",
        "menu_add": "➕ Add Address",
        "menu_list": "📋 My Addresses",
        "menu_lang": "🌐 Change Language",
        "ask_address": "📥 Please enter your DASH address:",
        "invalid_address": "❌ Invalid DASH address format. Please try again or /cancel:",
        "address_saved": "✅ Address <code>{address}</code> saved successfully!",
        "address_exists": "⚠️ Address <code>{address}</code> is already in your list.",
        "no_addresses": "📭 You have no saved addresses yet.",
        "addresses_header": "📋 <b>Your DASH Addresses:</b>",
        "choose_lang": "🌐 Choose your language:",
        "lang_set": "✅ Language set to English.",
        "lang_en": "🇬🇧 English",
        "lang_hy": "🇦🇲 Armenian",
        "back": "⬅️ Back",
        "cancel": "🚫 Operation cancelled.",
        "deposit_notify": (
            "💰 <b>Incoming DASH Transaction!</b>\n\n"
            "📋 Receipt #{receipt_number}\n"
            "🕐 Time: <b>{timestamp}</b>\n"
            "📤 From: {senders_text}\n"
            "📬 To: <code>{address}</code>\n"
            "💵 Amount: <b>{amount_usd}</b>  <i>({amount_dash} DASH)</i>\n"
            "📈 Rate: <b>1 DASH = {rate}</b>\n"
            "🔗 TX: <a href=\"{tx_url}\">{txid_short}...{txid_tail}</a>"
        ),
    },
    "hy": {
        "welcome": "👋 Բարի գալուստ DASH Address Tracker!\nՕգտագործեք ստորև ընտրացանկը:",
        "menu_add": "➕ Ավելացնել հասցե",
        "menu_list": "📋 Իմ հասցեները",
        "menu_lang": "🌐 Փոխել լեզուն",
        "ask_address": "📥 Մուտքագրեք ձեր DASH հասցեն:",
        "invalid_address": "❌ DASH հասցեի սխալ ձևաչափ: Փորձեք կրկին կամ /cancel:",
        "address_saved": "✅ Հասցե <code>{address}</code> հաջողությամբ պահպանված է:",
        "address_exists": "⚠️ Հասցե <code>{address}</code> արդեն ձեր ցուցակում է:",
        "no_addresses": "📭 Դուք դեռ պահպանված հասցե չունեք:",
        "addresses_header": "📋 <b>Ձեր DASH հասցեները:</b>",
        "choose_lang": "🌐 Ընտրեք ձեր լեզուն:",
        "lang_set": "✅ Լեզուն սահմանված է հայերեն:",
        "lang_en": "🇬🇧 Անգլերեն",
        "lang_hy": "🇦🇲 Հայերեն",
        "back": "⬅️ Հետ",
        "cancel": "🚫 Գործողությունը չեղարկված է:",
        "deposit_notify": (
            "💰 <b>Մուտքային DASH Գործարք!</b>\n\n"
            "📋 Անդորրագիր #{receipt_number}\n"
            "🕐 Ժամը: <b>{timestamp}</b>\n"
            "📤 Ուղարկողը: {senders_text}\n"
            "📬 Ստացողը: <code>{address}</code>\n"
            "💵 Գումար: <b>{amount_usd}</b>  <i>({amount_dash} DASH)</i>\n"
            "📈 Կուրս: <b>1 DASH = {rate}</b>\n"
            "🔗 TX: <a href=\"{tx_url}\">{txid_short}...{txid_tail}</a>"
        ),
    },
}

# ─────────────────────────── DATABASE ─────────────────────────────────────────


async def init_db() -> None:
    """Initialize SQLite database with required tables."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS watched_addresses (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                address     TEXT    NOT NULL,
                added_at    INTEGER NOT NULL,
                UNIQUE(user_id, address)
            );

            CREATE TABLE IF NOT EXISTS user_settings (
                user_id     INTEGER PRIMARY KEY,
                language    TEXT NOT NULL DEFAULT 'en'
            );

            CREATE TABLE IF NOT EXISTS user_receipt_counter (
                user_id     INTEGER PRIMARY KEY,
                counter     INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS seen_transactions (
                txid        TEXT    NOT NULL,
                address     TEXT    NOT NULL,
                PRIMARY KEY (txid, address)
            );
            """
        )
        await db.commit()
    logger.info("Database initialised at %s", DATABASE_PATH)


async def get_user_language(user_id: int) -> str:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        async with db.execute(
            "SELECT language FROM user_settings WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else "en"


async def set_user_language(user_id: int, lang: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO user_settings (user_id, language) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET language = excluded.language",
            (user_id, lang),
        )
        await db.commit()


async def get_next_receipt_number(user_id: int) -> int:
    """
    Atomically increment and return the per-user receipt counter.
    Each user has their own isolated counter stored in user_receipt_counter.
    The counter persists across bot restarts.
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        # Upsert: insert with counter=1 on first use, increment on subsequent uses
        await db.execute(
            """
            INSERT INTO user_receipt_counter (user_id, counter) VALUES (?, 1)
            ON CONFLICT(user_id) DO UPDATE SET counter = counter + 1
            """,
            (user_id,),
        )
        await db.commit()
        async with db.execute(
            "SELECT counter FROM user_receipt_counter WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0]


async def add_address_db(user_id: int, address: str) -> bool:
    """Returns True if inserted, False if already exists."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO watched_addresses (user_id, address, added_at) VALUES (?, ?, ?)",
                (user_id, address, int(time.time())),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def get_user_addresses(user_id: int) -> list[str]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        async with db.execute(
            "SELECT address FROM watched_addresses WHERE user_id = ? ORDER BY added_at",
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]


async def get_all_watched_addresses() -> dict[str, list[int]]:
    """Returns {address: [user_id, ...]} mapping for the poller."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        async with db.execute(
            "SELECT address, user_id FROM watched_addresses"
        ) as cursor:
            rows = await cursor.fetchall()
    result: dict[str, list[int]] = {}
    for address, user_id in rows:
        result.setdefault(address, []).append(user_id)
    return result


async def is_tx_seen(txid: str, address: str) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM seen_transactions WHERE txid = ? AND address = ?",
            (txid, address),
        ) as cursor:
            return await cursor.fetchone() is not None


async def mark_tx_seen(txid: str, address: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO seen_transactions (txid, address) VALUES (?, ?)",
            (txid, address),
        )
        await db.commit()


# ─────────────────────────── HELPERS ──────────────────────────────────────────

DASH_ADDRESS_RE = re.compile(r"^[X7][1-9A-HJ-NP-Za-km-z]{25,34}$")


def is_valid_dash_address(address: str) -> bool:
    return bool(DASH_ADDRESS_RE.match(address.strip()))


def t(lang: str, key: str, **kwargs) -> str:
    text = TEXTS.get(lang, TEXTS["en"]).get(key, key)
    return text.format(**kwargs) if kwargs else text


async def safe_edit(
    query: CallbackQuery, text: str, reply_markup=None, parse_mode=ParseMode.HTML
) -> None:
    """Edit message text; send a new message if the edit fails."""
    try:
        await query.edit_message_text(
            text=text, reply_markup=reply_markup, parse_mode=parse_mode
        )
    except BadRequest as exc:
        logger.warning("safe_edit – edit failed (%s), sending new message", exc)
        await query.message.reply_text(
            text=text, reply_markup=reply_markup, parse_mode=parse_mode
        )


# ─────────────────────────── KEYBOARDS ────────────────────────────────────────


def build_main_menu(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t(lang, "menu_add"), callback_data="add_address")],
            [InlineKeyboardButton(t(lang, "menu_list"), callback_data="my_addresses")],
            [InlineKeyboardButton(t(lang, "menu_lang"), callback_data="change_lang")],
        ]
    )


def build_language_menu(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(t(lang, "lang_en"), callback_data="set_lang_en"),
                InlineKeyboardButton(t(lang, "lang_hy"), callback_data="set_lang_hy"),
            ],
            [InlineKeyboardButton(t(lang, "back"), callback_data="main_menu")],
        ]
    )


def build_back_button(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t(lang, "back"), callback_data="main_menu")]]
    )


# ─────────────────────────── COMMAND HANDLERS ─────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang = await get_user_language(user_id)
    await update.message.reply_text(
        text=t(lang, "welcome"),
        reply_markup=build_main_menu(lang),
        parse_mode=ParseMode.HTML,
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    lang = await get_user_language(user_id)
    await update.message.reply_text(
        text=t(lang, "cancel"),
        reply_markup=build_main_menu(lang),
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


# ─────────────────────────── CALLBACK HANDLERS ────────────────────────────────


async def panel_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Central dispatcher for all inline button clicks outside ConversationHandler."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = await get_user_language(user_id)
    data = query.data

    if data == "main_menu":
        await safe_edit(
            query,
            t(lang, "welcome"),
            reply_markup=build_main_menu(lang),
        )

    elif data == "my_addresses":
        addresses = await get_user_addresses(user_id)
        if not addresses:
            text = t(lang, "no_addresses")
        else:
            lines = [t(lang, "addresses_header")]
            for i, addr in enumerate(addresses, 1):
                lines.append(f"{i}. <code>{addr}</code>")
            text = "\n".join(lines)
        await safe_edit(query, text, reply_markup=build_back_button(lang))

    elif data == "change_lang":
        await safe_edit(
            query,
            t(lang, "choose_lang"),
            reply_markup=build_language_menu(lang),
        )

    elif data == "set_lang_en":
        await set_user_language(user_id, "en")
        lang = "en"
        await safe_edit(
            query,
            t(lang, "lang_set"),
            reply_markup=build_main_menu(lang),
        )

    elif data == "set_lang_hy":
        await set_user_language(user_id, "hy")
        lang = "hy"
        await safe_edit(
            query,
            t(lang, "lang_set"),
            reply_markup=build_main_menu(lang),
        )


# ─────────────────────────── CONVERSATION: ADD ADDRESS ────────────────────────


async def add_address_entry(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Entry point triggered by the 'add_address' inline button."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = await get_user_language(user_id)
    await safe_edit(query, t(lang, "ask_address"))
    return WAITING_ADDRESS


async def add_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive and validate the DASH address text message."""
    user_id = update.effective_user.id
    lang = await get_user_language(user_id)
    raw = update.message.text.strip()

    if not is_valid_dash_address(raw):
        await update.message.reply_text(
            t(lang, "invalid_address"), parse_mode=ParseMode.HTML
        )
        return WAITING_ADDRESS  # stay in state, let user retry

    inserted = await add_address_db(user_id, raw)

    if inserted:
        msg = t(lang, "address_saved", address=raw)
    else:
        msg = t(lang, "address_exists", address=raw)

    await update.message.reply_text(
        msg,
        reply_markup=build_main_menu(lang),
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


# ─────────────────────────── DEPOSIT NOTIFICATION ─────────────────────────────


async def notify_deposit(
    bot: Bot,
    user_id: int,
    address: str,
    amount: float,
    txid: str,
    price_usd: Optional[float] = None,
    tx_time: Optional[int] = None,
    senders: Optional[list[str]] = None,
) -> None:
    """
    Send deposit notification to a user.
    Uses the per-user receipt counter — each call atomically increments it.
    Displays amount in USD, sender address(es), timestamp and a clickable TX link.
    """
    receipt_number = await get_next_receipt_number(user_id)
    lang = await get_user_language(user_id)

    amount_usd = dash_to_usd(amount, price_usd)
    amount_dash = f"{amount:.8f}"
    rate = f"${price_usd:,.2f}" if price_usd is not None else "N/A"

    # Build timestamp string — use blockchain tx time if available, else now
    ts = tx_time if tx_time else int(time.time())
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    timestamp = dt.strftime("%Y-%m-%d %H:%M:%S UTC")

    # Clickable TX link — show first 8 and last 8 chars to keep it readable
    tx_url = f"https://insight.dash.org/insight/tx/{txid}"
    txid_short = txid[:8]
    txid_tail = txid[-8:]

    # Format sender list — wrap each address in <code> tags, one per line if multiple
    if senders:
        if len(senders) == 1:
            senders_text = f"<code>{senders[0]}</code>"
        else:
            lines = "\n        ".join(f"<code>{s}</code>" for s in senders)
            senders_text = f"\n        {lines}"
    else:
        senders_text = "<i>unknown</i>"

    text = t(
        lang,
        "deposit_notify",
        receipt_number=receipt_number,
        timestamp=timestamp,
        senders_text=senders_text,
        address=address,
        amount_usd=amount_usd,
        amount_dash=amount_dash,
        rate=rate,
        txid=txid,
        tx_url=tx_url,
        txid_short=txid_short,
        txid_tail=txid_tail,
    )
    try:
        await bot.send_message(
            chat_id=user_id, text=text, parse_mode=ParseMode.HTML
        )
        logger.info(
            "Notified user %d | Receipt #%d | %s DASH ≈ %s | %s | %s",
            user_id,
            receipt_number,
            amount_dash,
            amount_usd,
            timestamp,
            txid,
        )
    except TelegramError as exc:
        logger.error("Failed to notify user %d: %s", user_id, exc)


# ─────────────────────────── DASH BLOCKCHAIN POLLER ───────────────────────────


async def fetch_address_txs(
    session: aiohttp.ClientSession, address: str
) -> list[dict]:
    """Query the Dash Insight API for transactions of a given address."""
    url = f"{DASH_API_BASE}/addrs/{address}/txs?from=0&to=10"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                logger.warning("Insight API %s returned %d", url, resp.status)
                return []
            data = await resp.json()
            return data.get("items", [])
    except Exception as exc:
        logger.error("Error fetching txs for %s: %s", address, exc)
        return []


def extract_tx_info(tx: dict, address: str) -> Optional[tuple[float, list[str]]]:
    """
    Extract received amount and sender addresses from a transaction.
    Returns (amount_dash, [sender_address, ...]) or None if the watched
    address is not among the outputs (i.e. did not receive anything).

    Sender addresses are pulled from vin[].addr — the Insight API populates
    this field for standard P2PKH inputs. Duplicates are removed while
    preserving order. The watched address itself is excluded from the sender
    list (handles change outputs / self-sends cleanly).
    """
    # ── Amount received by watched address ───────────────────────────────────
    total = 0.0
    for vout in tx.get("vout", []):
        script_pub_key = vout.get("scriptPubKey", {})
        if address in script_pub_key.get("addresses", []):
            try:
                total += float(vout.get("value", 0))
            except (TypeError, ValueError):
                pass

    if total == 0:
        return None

    # ── Sender addresses from inputs ─────────────────────────────────────────
    seen: set[str] = set()
    senders: list[str] = []
    for vin in tx.get("vin", []):
        addr = vin.get("addr", "").strip()
        if addr and addr != address and addr not in seen:
            seen.add(addr)
            senders.append(addr)

    return total, senders


async def blockchain_poller(bot: Bot) -> None:
    """
    Background task: periodically checks all watched addresses for new
    incoming transactions and triggers deposit notifications.
    Fetches current DASH/USD price once per cycle (cached) and includes
    the USD equivalent in every deposit notification.
    """
    logger.info("Blockchain poller started (interval: %ds)", POLLING_INTERVAL)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # Fetch price once per polling cycle; shared across all notifications
                price_usd = await get_dash_usd_price(session)

                watched = await get_all_watched_addresses()
                for address, user_ids in watched.items():
                    txs = await fetch_address_txs(session, address)
                    for tx in txs:
                        txid = tx.get("txid", "")
                        if not txid:
                            continue
                        if await is_tx_seen(txid, address):
                            continue
                        info = extract_tx_info(tx, address)
                        if info is None:
                            continue
                        amount, senders = info
                        # Mark before notifying to prevent double-notification
                        await mark_tx_seen(txid, address)
                        tx_time = tx.get("time") or tx.get("blocktime") or None
                        for user_id in user_ids:
                            await notify_deposit(
                                bot=bot,
                                user_id=user_id,
                                address=address,
                                amount=amount,
                                txid=txid,
                                price_usd=price_usd,
                                tx_time=tx_time,
                                senders=senders,
                            )
            except Exception as exc:
                logger.exception("Poller error: %s", exc)
            await asyncio.sleep(POLLING_INTERVAL)


# ─────────────────────────── APPLICATION SETUP ────────────────────────────────


def build_application() -> Application:
    application = Application.builder().token(BOT_TOKEN).build()

    # ── ConversationHandler for Add Address flow ──────────────────────────────
    conv_handler = ConversationHandler(
        entry_points=[
            # Triggered only by the 'add_address' callback; does NOT clash with
            # panel_click because ConversationHandler intercepts first.
            CallbackQueryHandler(add_address_entry, pattern="^add_address$"),
        ],
        states={
            WAITING_ADDRESS: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, add_address
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CommandHandler("start", cmd_start),
        ],
        # Allow the conversation to be restarted from any point
        allow_reentry=True,
        # Per-user, per-chat isolation
        per_user=True,
        per_chat=True,
    )

    # ── Global callback handler for all non-conversation buttons ─────────────
    # Note: patterns that are entry_points of conv_handler are intentionally
    # excluded here to avoid duplicate handling.
    panel_handler = CallbackQueryHandler(
        panel_click,
        pattern="^(main_menu|my_addresses|change_lang|set_lang_en|set_lang_hy)$",
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(conv_handler)
    application.add_handler(panel_handler)

    return application


# ─────────────────────────── MAIN ─────────────────────────────────────────────


async def main() -> None:
    await init_db()

    application = build_application()

    # Start the blockchain poller as a background task
    async with application:
        poller_task = asyncio.create_task(
            blockchain_poller(application.bot),
            name="blockchain_poller",
        )
        logger.info("Starting bot polling …")
        await application.start()
        await application.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        # Run until interrupted
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            logger.info("Shutdown signal received.")
        finally:
            poller_task.cancel()
            try:
                await poller_task
            except asyncio.CancelledError:
                pass
            await application.updater.stop()
            await application.stop()


if __name__ == "__main__":
    asyncio.run(main())
