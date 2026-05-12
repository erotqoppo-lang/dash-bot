"""
Production-ready Telegram Bot for DASH Address Tracking
Requirements: python-telegram-bot>=20.0, aiohttp, aiosqlite
Install: pip install "python-telegram-bot>=20.0" aiohttp aiosqlite
"""

import asyncio
import logging
import re
import time
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
BLOCKCYPHER_BASE = "https://api.blockcypher.com/v1/dash/main"
POLLING_INTERVAL = 60  # seconds between blockchain checks
PRICE_CACHE_TTL = 120  # seconds to cache DASH/USD price

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────── PRICE CACHE ──────────────────────────────────────

_price_cache: dict = {"price": None, "fetched_at": 0.0}


async def get_dash_usd_price(session: aiohttp.ClientSession) -> Optional[float]:
    """Fetch current DASH/USD price from CoinGecko. Cached for PRICE_CACHE_TTL seconds."""
    now = time.time()
    if _price_cache["price"] is not None and (now - _price_cache["fetched_at"]) < PRICE_CACHE_TTL:
        return _price_cache["price"]
    url = "https://api.coingecko.com/api/v3/simple/price?ids=dash&vs_currencies=usd"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                logger.warning("CoinGecko price API returned %d", resp.status)
                return _price_cache["price"]
            data = await resp.json()
            price = float(data["dash"]["usd"])
            _price_cache["price"] = price
            _price_cache["fetched_at"] = now
            logger.info("DASH/USD price updated: $%.4f", price)
            return price
    except Exception as exc:
        logger.error("Failed to fetch DASH/USD price: %s", exc)
        return _price_cache["price"]


def dash_to_usd(amount_dash: float, price_usd: Optional[float]) -> str:
    if price_usd is None:
        return f"{amount_dash:.8f} DASH"
    return f"${amount_dash * price_usd:,.2f}"


# ─────────────────────────── CONVERSATION STATES ──────────────────────────────

WAITING_ADDRESS = 1

# ─────────────────────────── TRANSLATIONS ─────────────────────────────────────

TEXTS = {
    "en": {
        "welcome": "👋 Welcome to DASH Address Tracker!\nUse the menu below:",
        "menu_add": "➕ Add Address",
        "menu_list": "📋 My Addresses",
        "menu_delete": "🗑 Delete Address",
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
        "delete_choose": "🗑 <b>Choose address to delete:</b>",
        "delete_confirm": "❓ Are you sure you want to delete:\n<code>{address}</code>?",
        "delete_yes": "✅ Yes, delete",
        "delete_no": "❌ Cancel",
        "delete_done": "✅ Address deleted:\n<code>{address}</code>",
        "delete_no_addresses": "📭 No addresses to delete.",
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
        "menu_delete": "🗑 Ջնջել հասցե",
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
        "delete_choose": "🗑 <b>Ընտրեք ջնջելու հասցեն:</b>",
        "delete_confirm": "❓ Վստա՞հ եք որ ուզում եք ջնջել:\n<code>{address}</code>?",
        "delete_yes": "✅ Այո, ջնջել",
        "delete_no": "❌ Չեղարկել",
        "delete_done": "✅ Հասցեն ջնջված է:\n<code>{address}</code>",
        "delete_no_addresses": "📭 Ջնջելու հասցե չկա:",
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
    """Atomically increment and return the per-user receipt counter."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO user_receipt_counter (user_id, counter) VALUES (?, 1) "
            "ON CONFLICT(user_id) DO UPDATE SET counter = counter + 1",
            (user_id,),
        )
        await db.commit()
        async with db.execute(
            "SELECT counter FROM user_receipt_counter WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0]


async def add_address_db(user_id: int, address: str) -> bool:
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


async def delete_address_db(user_id: int, address: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "DELETE FROM watched_addresses WHERE user_id = ? AND address = ?",
            (user_id, address),
        )
        await db.commit()


async def get_user_addresses(user_id: int) -> list[str]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        async with db.execute(
            "SELECT address FROM watched_addresses WHERE user_id = ? ORDER BY added_at",
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]


async def get_all_watched_addresses() -> dict[str, list[int]]:
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
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as exc:
        logger.warning("safe_edit failed (%s), sending new message", exc)
        await query.message.reply_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)


# ─────────────────────────── KEYBOARDS ────────────────────────────────────────


def build_main_menu(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "menu_add"), callback_data="add_address")],
        [InlineKeyboardButton(t(lang, "menu_list"), callback_data="my_addresses")],
        [InlineKeyboardButton(t(lang, "menu_delete"), callback_data="delete_address")],
        [InlineKeyboardButton(t(lang, "menu_lang"), callback_data="change_lang")],
    ])


def build_language_menu(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(t(lang, "lang_en"), callback_data="set_lang_en"),
            InlineKeyboardButton(t(lang, "lang_hy"), callback_data="set_lang_hy"),
        ],
        [InlineKeyboardButton(t(lang, "back"), callback_data="main_menu")],
    ])


def build_back_button(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t(lang, "back"), callback_data="main_menu")]]
    )


def build_delete_list(addresses: list[str], lang: str) -> InlineKeyboardMarkup:
    """One button per address showing shortened form, plus Back button."""
    buttons = []
    for addr in addresses:
        short = f"{addr[:8]}...{addr[-6:]}"
        buttons.append([InlineKeyboardButton(f"🗑 {short}", callback_data=f"del_confirm:{addr}")])
    buttons.append([InlineKeyboardButton(t(lang, "back"), callback_data="main_menu")])
    return InlineKeyboardMarkup(buttons)


def build_delete_confirm(address: str, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(t(lang, "delete_yes"), callback_data=f"del_do:{address}"),
        InlineKeyboardButton(t(lang, "delete_no"), callback_data="delete_address"),
    ]])


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
        await safe_edit(query, t(lang, "welcome"), reply_markup=build_main_menu(lang))

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
        await safe_edit(query, t(lang, "choose_lang"), reply_markup=build_language_menu(lang))

    elif data == "set_lang_en":
        await set_user_language(user_id, "en")
        lang = "en"
        await safe_edit(query, t(lang, "lang_set"), reply_markup=build_main_menu(lang))

    elif data == "set_lang_hy":
        await set_user_language(user_id, "hy")
        lang = "hy"
        await safe_edit(query, t(lang, "lang_set"), reply_markup=build_main_menu(lang))

    elif data == "delete_address":
        addresses = await get_user_addresses(user_id)
        if not addresses:
            await safe_edit(query, t(lang, "delete_no_addresses"), reply_markup=build_back_button(lang))
        else:
            await safe_edit(query, t(lang, "delete_choose"), reply_markup=build_delete_list(addresses, lang))

    elif data.startswith("del_confirm:"):
        address = data.split(":", 1)[1]
        await safe_edit(
            query,
            t(lang, "delete_confirm", address=address),
            reply_markup=build_delete_confirm(address, lang),
        )

    elif data.startswith("del_do:"):
        address = data.split(":", 1)[1]
        await delete_address_db(user_id, address)
        logger.info("User %d deleted address %s", user_id, address)
        await safe_edit(
            query,
            t(lang, "delete_done", address=address),
            reply_markup=build_main_menu(lang),
        )


# ─────────────────────────── CONVERSATION: ADD ADDRESS ────────────────────────


async def add_address_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = await get_user_language(user_id)
    await safe_edit(query, t(lang, "ask_address"))
    return WAITING_ADDRESS


async def add_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    lang = await get_user_language(user_id)
    raw = update.message.text.strip()

    if not is_valid_dash_address(raw):
        await update.message.reply_text(t(lang, "invalid_address"), parse_mode=ParseMode.HTML)
        return WAITING_ADDRESS

    inserted = await add_address_db(user_id, raw)
    msg = t(lang, "address_saved", address=raw) if inserted else t(lang, "address_exists", address=raw)
    await update.message.reply_text(msg, reply_markup=build_main_menu(lang), parse_mode=ParseMode.HTML)
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
    receipt_number = await get_next_receipt_number(user_id)
    lang = await get_user_language(user_id)

    amount_usd = dash_to_usd(amount, price_usd)
    amount_dash = f"{amount:.8f}"
    rate = f"${price_usd:,.2f}" if price_usd is not None else "N/A"

    ts = tx_time if tx_time else int(time.time())
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    timestamp = dt.strftime("%Y-%m-%d %H:%M:%S UTC")

    tx_url = f"https://insight.dash.org/insight/tx/{txid}"
    txid_short = txid[:8]
    txid_tail = txid[-8:]

    if senders:
        senders_text = f"<code>{senders[0]}</code>" if len(senders) == 1 else \
            "\n" + "\n".join(f"<code>{s}</code>" for s in senders)
    else:
        senders_text = "<i>unknown</i>"

    text = t(
        lang, "deposit_notify",
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
        await bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML)
        logger.info(
            "Notified user %d | Receipt #%d | %s DASH ≈ %s | %s | %s",
            user_id, receipt_number, amount_dash, amount_usd, timestamp, txid,
        )
    except TelegramError as exc:
        logger.error("Failed to notify user %d: %s", user_id, exc)


# ─────────────────────────── DASH BLOCKCHAIN POLLER ───────────────────────────


async def fetch_address_txs(session: aiohttp.ClientSession, address: str) -> list[dict]:
    """
    Query BlockCypher API for transactions of a given address.
    Uses BlockCypher instead of insight.dash.org to avoid 403 errors.
    """
    url = f"{BLOCKCYPHER_BASE}/addrs/{address}/full?limit=10"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 429:
                logger.warning("BlockCypher rate limit hit for %s, retrying next cycle", address)
                return []
            if resp.status != 200:
                logger.warning("BlockCypher API returned %d for %s", resp.status, address)
                return []
            data = await resp.json()
            return data.get("txs", [])
    except Exception as exc:
        logger.error("Error fetching txs for %s: %s", address, exc)
        return []


def extract_tx_info(tx: dict, address: str) -> Optional[tuple[float, list[str]]]:
    """
    Extract received amount and senders from a BlockCypher transaction.
    BlockCypher uses satoshis (1 DASH = 100_000_000 satoshis).
    Returns (amount_dash, [sender, ...]) or None if address not in outputs.
    """
    total_satoshis = 0
    for output in tx.get("outputs", []):
        if address in output.get("addresses", []):
            try:
                total_satoshis += int(output.get("value", 0))
            except (TypeError, ValueError):
                pass

    if total_satoshis == 0:
        return None

    amount_dash = total_satoshis / 100_000_000

    seen: set[str] = set()
    senders: list[str] = []
    for inp in tx.get("inputs", []):
        for addr in inp.get("addresses", []):
            if addr and addr != address and addr not in seen:
                seen.add(addr)
                senders.append(addr)

    return amount_dash, senders


async def blockchain_poller(bot: Bot) -> None:
    """
    Background task: checks all watched addresses for new incoming transactions
    using BlockCypher API every POLLING_INTERVAL seconds.
    """
    logger.info("Blockchain poller started (interval: %ds)", POLLING_INTERVAL)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                price_usd = await get_dash_usd_price(session)
                watched = await get_all_watched_addresses()

                for address, user_ids in watched.items():
                    txs = await fetch_address_txs(session, address)
                    for tx in txs:
                        txid = tx.get("hash", "")
                        if not txid:
                            continue
                        if await is_tx_seen(txid, address):
                            continue
                        info = extract_tx_info(tx, address)
                        if info is None:
                            continue
                        amount, senders = info
                        await mark_tx_seen(txid, address)

                        # Parse BlockCypher ISO timestamp
                        tx_time = None
                        raw_time = tx.get("confirmed") or tx.get("received")
                        if raw_time:
                            try:
                                dt = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
                                tx_time = int(dt.timestamp())
                            except Exception:
                                pass

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
                    # Small delay between addresses to respect rate limits
                    await asyncio.sleep(1)

            except Exception as exc:
                logger.exception("Poller error: %s", exc)
            await asyncio.sleep(POLLING_INTERVAL)


# ─────────────────────────── APPLICATION SETUP ────────────────────────────────


def build_application() -> Application:
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(add_address_entry, pattern="^add_address$"),
        ],
        states={
            WAITING_ADDRESS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_address),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CommandHandler("start", cmd_start),
        ],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
    )

    # Handles all buttons outside the conversation
    panel_handler = CallbackQueryHandler(
        panel_click,
        pattern=(
            "^(main_menu|my_addresses|change_lang|set_lang_en|set_lang_hy"
            "|delete_address|del_confirm:.+|del_do:.+)$"
        ),
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(conv_handler)
    application.add_handler(panel_handler)

    return application


# ─────────────────────────── MAIN ─────────────────────────────────────────────


async def main() -> None:
    await init_db()
    application = build_application()

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
