import asyncio
import logging
import os
import sqlite3
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ============ CONFIG ============
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_IDS = [int(x.strip()) for x in os.getenv("ADMIN_CHAT_IDS", "").split(",") if x.strip().isdigit()]
DB_PATH = os.getenv("DB_PATH", "dash_watch.db")

# Интервал проверки (сек). Для 18 адресов 30-60 — комфортно.
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "45"))
# Сколько запросов к блокчейну одновременно
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "3"))
# Пауза после каждого запроса внутри воркера (сек) — мягкий rate limit
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.3"))
# Сколько неподтверждённых/новых tx проверять за раз на адрес
MAX_TX_PER_CHECK = 50
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "4"))

# Список Insight-серверов через запятую; при сбое бот переключается на следующий.
INSIGHT_BASES = [
    x.strip().rstrip("/")
    for x in os.getenv("INSIGHT_BASES", "https://insight.dash.org/insight-api").split(",")
    if x.strip()
]
EXPLORER_TX_URL = "https://insight.dash.org/insight/tx/{txid}"

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("dashbot")

ADD_ADDRESS = 1
scan_lock = asyncio.Lock()

# ============ DATABASE ============
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watched_addresses (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                address TEXT NOT NULL,
                label TEXT,
                created_at INTEGER NOT NULL,
                seeded INTEGER NOT NULL DEFAULT 0,
                UNIQUE(user_id, address)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_transactions (
                txid TEXT NOT NULL,
                address TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (txid, address, user_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                language TEXT DEFAULT 'en'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_receipt_counter (
                user_id INTEGER PRIMARY KEY,
                counter INTEGER DEFAULT 0
            )
        """)
        # Миграция старой БД: колонка seeded
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(watched_addresses)").fetchall()]
        if "seeded" not in cols:
            conn.execute("ALTER TABLE watched_addresses ADD COLUMN seeded INTEGER NOT NULL DEFAULT 0")
            conn.execute("""
                UPDATE watched_addresses SET seeded = 1
                WHERE EXISTS (
                    SELECT 1 FROM seen_transactions s
                    WHERE s.user_id = watched_addresses.user_id AND s.address = watched_addresses.address
                )
            """)
    logger.info("✅ Database ready")


def get_user_language(user_id: int) -> str:
    with db() as conn:
        row = conn.execute("SELECT language FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
        return row["language"] if row else "en"


def set_user_language(user_id: int, lang: str):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO user_settings(user_id, language) VALUES (?, ?)", (user_id, lang))


def add_address_to_db(user_id: int, address: str, label: str = "") -> bool:
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO watched_addresses(user_id, address, label, created_at) VALUES (?, ?, ?, ?)",
                (user_id, address.strip(), label.strip(), int(time.time())),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def remove_address_from_db(user_id: int, address_id: int) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT address FROM watched_addresses WHERE id = ? AND user_id = ?", (address_id, user_id)
        ).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM watched_addresses WHERE id = ? AND user_id = ?", (address_id, user_id))
        conn.execute("DELETE FROM seen_transactions WHERE user_id = ? AND address = ?", (user_id, row["address"]))
        return True


def get_addresses(user_id: int) -> List[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT id, address, label FROM watched_addresses WHERE user_id = ? ORDER BY id DESC", (user_id,)
        ).fetchall()


def get_subscriptions(user_ids: List[int]) -> List[sqlite3.Row]:
    if not user_ids:
        return []
    marks = ",".join("?" * len(user_ids))
    with db() as conn:
        return conn.execute(
            f"SELECT user_id, address, seeded FROM watched_addresses WHERE user_id IN ({marks})", user_ids
        ).fetchall()


def get_seen_set(user_id: int, address: str) -> set:
    with db() as conn:
        rows = conn.execute(
            "SELECT txid FROM seen_transactions WHERE user_id = ? AND address = ?", (user_id, address)
        ).fetchall()
        return {r["txid"] for r in rows}


def mark_tx_seen(user_id: int, txid: str, address: str):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_transactions(user_id, txid, address) VALUES (?, ?, ?)",
            (user_id, txid, address),
        )


def seed_in_db(user_id: int, address: str, txids: List[str]):
    with db() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO seen_transactions(user_id, txid, address) VALUES (?, ?, ?)",
            [(user_id, t, address) for t in txids],
        )
        conn.execute(
            "UPDATE watched_addresses SET seeded = 1 WHERE user_id = ? AND address = ?", (user_id, address)
        )


def increment_receipt_counter(user_id: int) -> int:
    with db() as conn:
        conn.execute(
            """INSERT INTO user_receipt_counter(user_id, counter) VALUES (?, 1)
               ON CONFLICT(user_id) DO UPDATE SET counter = counter + 1""",
            (user_id,),
        )
        return conn.execute("SELECT counter FROM user_receipt_counter WHERE user_id = ?", (user_id,)).fetchone()[0]


# ============ NETWORK ============
_price_cache = {"price": 0.0, "time": 0.0}
PRICE_CACHE_TTL = 300
_base_index = 0  # какой Insight-сервер сейчас основной


def get_dash_price_usd() -> float:
    now = time.time()
    if _price_cache["price"] > 0 and (now - _price_cache["time"]) < PRICE_CACHE_TTL:
        return _price_cache["price"]
    apis = [
        "https://api.binance.com/api/v3/ticker/price?symbol=DASHUSDT",
        "https://api.coingecko.com/api/v3/simple/price?ids=dash&vs_currencies=usd",
    ]
    for url in apis:
        try:
            r = requests.get(url, timeout=10)
            if r.status_code != 200:
                continue
            data = r.json()
            if "price" in data:
                price = float(data["price"])
            elif "dash" in data:
                price = float(data["dash"]["usd"])
            else:
                continue
            if price > 0:
                _price_cache.update(price=price, time=now)
                return price
        except Exception as e:
            logger.warning(f"Price API error: {e}")
    return _price_cache["price"]


def insight_get(path: str, retries: int = 3) -> Optional[dict]:
    """GET к Insight с retry, backoff на 429 и переключением серверов. Выполняется в потоке."""
    global _base_index
    n = len(INSIGHT_BASES)
    for offset in range(n):
        idx = (_base_index + offset) % n
        url = INSIGHT_BASES[idx] + path
        for attempt in range(retries):
            try:
                r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    _base_index = idx
                    time.sleep(REQUEST_DELAY)
                    return r.json()
                if r.status_code == 404:
                    return None
                if r.status_code == 429:
                    wait = float(r.headers.get("Retry-After", 2 ** attempt * 2))
                    logger.warning(f"429 от {INSIGHT_BASES[idx]}, жду {wait:.0f}с")
                    time.sleep(min(wait, 30))
                    continue
                logger.warning(f"HTTP {r.status_code} от {url}")
                time.sleep(1 + attempt)
            except (requests.RequestException, ValueError) as e:
                logger.warning(f"Ошибка запроса {url}: {e}")
                time.sleep(1 + attempt)
        # все попытки на этом сервере провалились — пробуем следующий
    return None


def parse_tx(tx: dict, address: str) -> dict:
    received = 0.0
    for vout in tx.get("vout", []):
        if address in (vout.get("scriptPubKey", {}).get("addresses") or []):
            try:
                received += float(vout.get("value") or 0)
            except (TypeError, ValueError):
                pass

    vin_addrs = [v.get("addr") for v in tx.get("vin", []) if v.get("addr")]
    outgoing = address in vin_addrs  # адрес платил сам -> это сдача, не депозит
    senders = []
    for a in vin_addrs:
        if a != address and a not in senders:
            senders.append(a)

    ts = tx.get("time") or tx.get("blocktime")
    if ts:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)
        time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
    else:
        time_str = "Just now"

    return {
        "amount": received,
        "outgoing": outgoing,
        "senders": ", ".join(senders) if senders else "Unknown",
        "time_str": time_str,
    }


# ============ TRANSLATIONS ============
TRANSLATIONS = {
    "en": {
        "start": "🤖 **Dash Deposit Bot Active**\n\n⏱ Scan: every {interval}s\n📊 Watching: {count} address(es)\n⚡ Insight API\n\nUse menu below 👇",
        "main_menu": "Main Menu:",
        "add_prompt": "➕ **Add DASH Address**\n\nSend address (starts with X or 7, 34 chars).\nOptional label: `address | label`",
        "invalid_address": "❌ Invalid DASH address",
        "address_added": "✅ Address added",
        "address_exists": "❌ Already exists",
        "no_addresses": "📭 No addresses",
        "address_list_header": "📚 **Watched Addresses:**\n",
        "remove_success": "✅ Removed",
        "checking": "🔄 Scanning...",
        "scan_complete": "✅ Scan complete!\n💰 Found: {count} new deposit(s)",
        "deposit_notification": "📬 **Receipt #{num}**\n\n💰 **NEW DASH DEPOSIT!** 💰\n\n📥 **To:** {address}\n\n💸 **From:** {senders}\n\n💵 **Amount:** {amount:.8f} DASH\n\n💵 **USD:** ${usd_value:.2f}\n\n🕒 **Time:** {time}\n\n🔗 **TXID:** {txid}",
        "btn_add_address": "➕ Add Address",
        "btn_my_addresses": "📚 My Addresses",
        "btn_check_now": "🔄 Check Now",
        "btn_language": "🌐 Language",
        "btn_back": "⬅️ Back",
        "btn_remove": "❌ Remove #{id}",
    },
    "hy": {
        "start": "🤖 **Dash Deposit Bot-ը ակտիվ է**\n\n⏱ Ստուգում ամեն {interval}վ\n📊 Դիտարկվածներ՝ {count}\n⚡ Insight API\n\nՕգտագործեք մենյուն 👇",
        "main_menu": "Գլխավոր մենյու:",
        "add_prompt": "➕ **Ավելացնել DASH հասցե**\n\nՀասցե (սկսվում է X կամ 7, 34 նիշ).\nԸնտրովի անուն՝ `հասցե | անուն`",
        "invalid_address": "❌ Սխալ DASH հասցե",
        "address_added": "✅ Ավելացված է",
        "address_exists": "❌ Արդեն կա",
        "no_addresses": "📭 Հասցեներ չկան",
        "address_list_header": "📚 **Դիտարկվող հասցեներ:**\n",
        "remove_success": "✅ Հեռացված է",
        "checking": "🔄 Ստուգում...",
        "scan_complete": "✅ Ստուգումն ավարտվել է!\n💰 Գտնված՝ {count}",
        "deposit_notification": "📬 **Չեք #{num}**\n\n💰 **ՆՈՐ DASH ՄՈՒՏՔ!** 💰\n\n📥 **Հասցե՝** {address}\n\n💸 **Ուղարկողից՝** {senders}\n\n💵 **Գումար՝** {amount:.8f} DASH\n\n💵 **USD՝** ${usd_value:.2f}\n\n🕒 **Ժամ՝** {time}\n\n🔗 **TXID՝** {txid}",
        "btn_add_address": "➕ Ավելացնել",
        "btn_my_addresses": "📚 Իմ հասցեներ",
        "btn_check_now": "🔄 Ստուգել",
        "btn_language": "🌐 Լեզու",
        "btn_back": "⬅️ Հետ",
        "btn_remove": "❌ Հեռացնել #{id}",
    },
}


def _(user_id: int, key: str, **kwargs) -> str:
    lang = get_user_language(user_id)
    text = TRANSLATIONS.get(lang, TRANSLATIONS["en"]).get(key) or TRANSLATIONS["en"].get(key, key)
    return text.format(**kwargs) if kwargs else text


# ============ HELPERS ============
def validate_dash_address(address: str) -> bool:
    return bool(address) and address[0] in ("X", "7") and 33 <= len(address) <= 35 and address.isalnum()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_CHAT_IDS


def build_main_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_(user_id, "btn_add_address"), callback_data="add")],
        [InlineKeyboardButton(_(user_id, "btn_my_addresses"), callback_data="list")],
        [InlineKeyboardButton(_(user_id, "btn_check_now"), callback_data="check_now")],
        [InlineKeyboardButton(_(user_id, "btn_language"), callback_data="language")],
    ])


def build_language_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")],
        [InlineKeyboardButton("🇦🇲 Հայերեն", callback_data="lang_hy")],
        [InlineKeyboardButton(_(user_id, "btn_back"), callback_data="back_main")],
    ])


# ============ SCANNER ============
async def seed_address(user_id: int, address: str) -> bool:
    """Помечает все текущие tx адреса как виденные, чтобы не спамить старыми депозитами."""
    data = await asyncio.to_thread(insight_get, f"/addr/{address}")
    if data is None:
        return False
    txids = data.get("transactions", [])
    seed_in_db(user_id, address, txids)
    logger.info(f"🛡 Seeded {len(txids)} tx для {address[:16]}... (user {user_id})")
    return True


async def scan(bot, user_ids: List[int]) -> Dict[int, int]:
    """
    Один проход по всем адресам указанных пользователей.
    - каждый уникальный адрес запрашивается один раз
    - каждая tx запрашивается один раз (кэш на проход)
    - при ошибке API tx НЕ помечается как seen -> повтор в следующем цикле
    """
    found: Dict[int, int] = defaultdict(int)
    async with scan_lock:
        started = time.time()
        subs = get_subscriptions(user_ids)
        by_addr: Dict[str, List[sqlite3.Row]] = defaultdict(list)
        for r in subs:
            by_addr[r["address"]].append(r)

        sem = asyncio.Semaphore(MAX_CONCURRENCY)
        tx_cache: Dict[str, Optional[dict]] = {}
        price_holder = {"value": None}
        price_lock = asyncio.Lock()

        async def fetch(path: str) -> Optional[dict]:
            async with sem:
                return await asyncio.to_thread(insight_get, path)

        async def get_tx(txid: str) -> Optional[dict]:
            if txid not in tx_cache:
                tx_cache[txid] = await fetch(f"/tx/{txid}")
            return tx_cache[txid]

        async def get_price() -> float:
            async with price_lock:
                if price_holder["value"] is None:
                    price_holder["value"] = await asyncio.to_thread(get_dash_price_usd)
                return price_holder["value"]

        async def process(address: str, rows: List[sqlite3.Row]):
            try:
                data = await fetch(f"/addr/{address}")
                if data is None:
                    logger.warning(f"Нет ответа по {address[:16]}..., повтор в следующем цикле")
                    return
                txids = data.get("transactions", [])

                ready = []
                for r in rows:
                    if r["seeded"]:
                        ready.append(r["user_id"])
                    else:
                        seed_in_db(r["user_id"], address, txids)
                if not ready or not txids:
                    return

                # unseen для каждого пользователя (самые старые первыми)
                unseen: Dict[int, List[str]] = {}
                for uid in ready:
                    seen = get_seen_set(uid, address)
                    unseen[uid] = [t for t in reversed(txids[:MAX_TX_PER_CHECK]) if t not in seen]

                for txid in dict.fromkeys(t for lst in unseen.values() for t in lst):
                    tx = await get_tx(txid)
                    if tx is None:
                        continue  # НЕ помечаем — попробуем позже
                    info = parse_tx(tx, address)
                    is_deposit = info["amount"] > 0 and not info["outgoing"]

                    for uid in ready:
                        if txid not in unseen[uid]:
                            continue
                        if not is_deposit:
                            mark_tx_seen(uid, txid, address)
                            continue
                        price = await get_price()
                        num = increment_receipt_counter(uid)
                        msg = _(
                            uid, "deposit_notification",
                            num=num, address=address, amount=info["amount"],
                            usd_value=info["amount"] * price if price > 0 else 0,
                            time=info["time_str"], senders=info["senders"],
                            txid=txid,
                        )
                        try:
                            await bot.send_message(chat_id=uid, text=msg, parse_mode="Markdown",
                                                   disable_web_page_preview=True)
                            mark_tx_seen(uid, txid, address)  # только после успешной отправки
                            found[uid] += 1
                            logger.info(f"💰 {info['amount']:.8f} DASH -> {address[:16]}... (user {uid})")
                        except Exception as e:
                            logger.error(f"Не удалось отправить уведомление user {uid}: {e}")
            except Exception:
                logger.exception(f"Ошибка обработки {address[:16]}...")

        await asyncio.gather(*(process(a, rows) for a, rows in by_addr.items()))

        elapsed = time.time() - started
        level = logging.WARNING if elapsed > CHECK_INTERVAL_SECONDS else logging.INFO
        logger.log(level, f"Скан: {len(by_addr)} адр., {elapsed:.1f}с (интервал {CHECK_INTERVAL_SECONDS}с)")
    return found


async def periodic_check(context: ContextTypes.DEFAULT_TYPE):
    await scan(context.bot, ADMIN_CHAT_IDS)


# ============ HANDLERS ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("❌ Unauthorized")
        return
    text = _(user.id, "start", interval=CHECK_INTERVAL_SECONDS, count=len(get_addresses(user.id)))
    await update.message.reply_text(text, reply_markup=build_main_menu(user.id), parse_mode="Markdown")


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if not user or not is_admin(user.id):
        await query.edit_message_text("❌ Unauthorized")
        return ConversationHandler.END
    await query.edit_message_text(_(user.id, "add_prompt"), parse_mode="Markdown")
    return ADD_ADDRESS


async def panel_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if not user or not is_admin(user.id):
        await query.edit_message_text("❌ Unauthorized")
        return

    user_id = user.id
    data = query.data

    if data == "list":
        rows = get_addresses(user_id)
        if not rows:
            await query.edit_message_text(_(user_id, "no_addresses"), reply_markup=build_main_menu(user_id))
            return
        lines = [_(user_id, "address_list_header")]
        keyboard = []
        for row in rows:
            label = f" — {row['label']}" if row["label"] else ""
            lines.append(f"`{row['id']}. {row['address']}`{label}")
            keyboard.append([InlineKeyboardButton(_(user_id, "btn_remove", id=row["id"]),
                                                  callback_data=f"remove:{row['id']}")])
        keyboard.append([InlineKeyboardButton(_(user_id, "btn_back"), callback_data="back_main")])
        await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard),
                                      parse_mode="Markdown")
        return

    if data.startswith("remove:"):
        ok = remove_address_from_db(user_id, int(data.split(":")[1]))
        await query.edit_message_text(_(user_id, "remove_success") if ok else "❌ Failed",
                                      reply_markup=build_main_menu(user_id))
        return

    if data == "check_now":
        await query.edit_message_text(_(user_id, "checking"))
        found = await scan(context.bot, [user_id])
        await context.bot.send_message(chat_id=user_id,
                                       text=_(user_id, "scan_complete", count=found.get(user_id, 0)),
                                       reply_markup=build_main_menu(user_id))
        return

    if data == "language":
        await query.edit_message_text(_(user_id, "main_menu"), reply_markup=build_language_menu(user_id))
        return

    if data.startswith("lang_"):
        set_user_language(user_id, data.split("_")[1])
        await query.edit_message_text("🌐 Language changed", reply_markup=build_main_menu(user_id))
        return

    if data == "back_main":
        await query.edit_message_text(_(user_id, "main_menu"), reply_markup=build_main_menu(user_id))
        return


async def add_address_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not user or not is_admin(user.id):
        return ConversationHandler.END

    text = update.message.text.strip()
    address, _sep, label = text.partition("|")
    address, label = address.strip(), label.strip()

    if not validate_dash_address(address):
        await update.message.reply_text(_(user.id, "invalid_address"))
        return ADD_ADDRESS

    if not add_address_to_db(user.id, address, label):
        await update.message.reply_text(_(user.id, "address_exists"), reply_markup=build_main_menu(user.id))
        return ConversationHandler.END

    # Сразу засеваем старые tx, чтобы не потерять депозит между добавлением и первым циклом
    await seed_address(user.id, address)
    await update.message.reply_text(_(user.id, "address_added"), reply_markup=build_main_menu(user.id))
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if user and is_admin(user.id):
        await update.message.reply_text("Cancelled", reply_markup=build_main_menu(user.id))
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error", exc_info=context.error)


# ============ MAIN ============
if __name__ == "__main__":
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN missing")
    if not ADMIN_CHAT_IDS:
        raise ValueError("ADMIN_CHAT_IDS missing")

    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_start, pattern="^add$")],
        states={ADD_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_address_received)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(panel_click))
    app.add_error_handler(error_handler)

    if app.job_queue:
        app.job_queue.run_repeating(
            periodic_check,
            interval=CHECK_INTERVAL_SECONDS,
            first=5,
            job_kwargs={"max_instances": 1, "coalesce": True},
        )
    else:
        logger.error("JobQueue недоступен: pip install 'python-telegram-bot[job-queue]'")

    logger.info("🚀 Bot started | interval=%ss | concurrency=%s | servers=%s",
                CHECK_INTERVAL_SECONDS, MAX_CONCURRENCY, INSIGHT_BASES)
    app.run_polling()
