import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# Config
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_IDS = [int(x.strip()) for x in os.getenv("ADMIN_CHAT_IDS", "").split(",") if x.strip().isdigit()]
DB_PATH = "dash_watch.db"
CHECK_INTERVAL_SECONDS = 20
TZ_OFFSET_HOURS = 4

# Insight API
INSIGHT_ADDR_API = "https://insight.dash.org/insight-api/addr/{address}"
INSIGHT_TX_API = "https://insight.dash.org/insight-api/tx/{txid}"
EXPLORER_TX_URL = "https://insight.dash.org/insight/tx/{txid}"

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

ADD_ADDRESS = 1

# Price cache
_price_cache = {"price": 0.0, "time": 0}
PRICE_CACHE_TTL = 300  # Cache for 5 minutes

# ===== DATABASE =====
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watched_addresses (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                address TEXT NOT NULL,
                label TEXT,
                created_at INTEGER NOT NULL,
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
        conn.commit()
    logger.info("✅ Database ready")

def get_user_language(user_id: int) -> str:
    with db() as conn:
        row = conn.execute("SELECT language FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
        return row['language'] if row else 'en'

def set_user_language(user_id: int, lang: str):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO user_settings(user_id, language) VALUES (?, ?)", (user_id, lang))
        conn.commit()

def add_address_to_db(user_id: int, address: str, label: str = "") -> Tuple[bool, str]:
    try:
        with db() as conn:
            conn.execute("INSERT INTO watched_addresses(user_id, address, label, created_at) VALUES (?, ?, ?, ?)",
                        (user_id, address.strip(), label.strip(), int(time.time())))
            conn.commit()
        return True, "✅ Address added"
    except sqlite3.IntegrityError:
        return False, "❌ Already exists"

def remove_address_from_db(user_id: int, address_id: int) -> bool:
    with db() as conn:
        cur = conn.execute("DELETE FROM watched_addresses WHERE id = ? AND user_id = ?", (address_id, user_id))
        conn.commit()
        return cur.rowcount > 0

def get_addresses(user_id: int) -> List[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT id, address, label FROM watched_addresses WHERE user_id = ? ORDER BY id DESC", (user_id,)).fetchall()

def is_tx_seen(user_id: int, txid: str, address: str) -> bool:
    with db() as conn:
        return conn.execute("SELECT 1 FROM seen_transactions WHERE user_id = ? AND txid = ? AND address = ?", (user_id, txid, address)).fetchone() is not None

def mark_tx_seen(user_id: int, txid: str, address: str):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO seen_transactions(user_id, txid, address) VALUES (?, ?, ?)", (user_id, txid, address))
        conn.commit()

def has_seen_transactions_for_address(user_id: int, address: str) -> bool:
    with db() as conn:
        return conn.execute("SELECT 1 FROM seen_transactions WHERE user_id = ? AND address = ? LIMIT 1", (user_id, address)).fetchone() is not None

def get_dash_price_usd() -> float:
    global _price_cache
    now = time.time()
    
    # Return cached price if still valid
    if _price_cache["price"] > 0 and (now - _price_cache["time"]) < PRICE_CACHE_TTL:
        return _price_cache["price"]
    
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=dash&vs_currencies=usd"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            price = float(data.get('dash', {}).get('usd', 0))
            if price > 0:
                _price_cache = {"price": price, "time": now}
                logger.info(f"💰 DASH/USD: ${price}")
                return price
    except Exception as e:
        logger.warning(f"Price fetch error: {e}")
    
    # Return last cached price if available
    return _price_cache.get("price", 0.0)

def get_receipt_counter(user_id: int) -> int:
    with db() as conn:
        row = conn.execute("SELECT counter FROM user_receipt_counter WHERE user_id = ?", (user_id,)).fetchone()
        return row['counter'] if row else 0

def increment_receipt_counter(user_id: int) -> int:
    with db() as conn:
        current = get_receipt_counter(user_id)
        new_count = current + 1
        conn.execute("INSERT OR REPLACE INTO user_receipt_counter(user_id, counter) VALUES (?, ?)", (user_id, new_count))
        conn.commit()
        return new_count

# ===== TRANSLATIONS =====
TRANSLATIONS = {
    "en": {
        "start": "🤖 **Dash Deposit Bot Active**\n\n⏱ Scan: every {interval}s\n📊 Watching: {count} address(es)\n⚡ Insight API\n\nUse menu below 👇",
        "main_menu": "Main Menu:",
        "add_prompt": "➕ **Add DASH Address**\n\nSend address (starts with X or 7, 34 chars)",
        "invalid_address": "❌ Invalid DASH address",
        "address_added": "✅ Address added",
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
        "add_prompt": "➕ **Ավելացնել DASH հասցե**\n\nՀասցե (սկսվում է X կամ 7, 34 նիշ)",
        "invalid_address": "❌ Սխալ DASH հասցե",
        "address_added": "✅ Ավելացված է",
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
    }
}

def _(user_id: int, key: str, **kwargs) -> str:
    lang = get_user_language(user_id)
    text = TRANSLATIONS.get(lang, TRANSLATIONS["en"]).get(key, key)
    if kwargs:
        text = text.format(**kwargs)
    return text

# ===== HELPERS =====
def validate_dash_address(address: str) -> bool:
    return address and (address.startswith('X') or address.startswith('7')) and 33 <= len(address) <= 35

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_CHAT_IDS

async def build_main_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_(user_id, "btn_add_address"), callback_data="add")],
        [InlineKeyboardButton(_(user_id, "btn_my_addresses"), callback_data="list")],
        [InlineKeyboardButton(_(user_id, "btn_check_now"), callback_data="check_now")],
        [InlineKeyboardButton(_(user_id, "btn_language"), callback_data="language")],
    ])

async def build_language_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")],
        [InlineKeyboardButton("🇦🇲 Հայերեն", callback_data="lang_hy")],
        [InlineKeyboardButton(_(user_id, "btn_back"), callback_data="back_main")],
    ])

def fetch_json(url: str) -> Optional[dict]:
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, timeout=15, headers=headers)
        if response.status_code == 200:
            return response.json()
    except:
        pass
    return None

def check_address_for_deposits(user_id: int, address: str) -> List[dict]:
    deposits = []
    url = INSIGHT_ADDR_API.format(address=address)
    data = fetch_json(url)
    if not data:
        return deposits

    for txid in data.get('transactions', [])[:20]:
        if is_tx_seen(user_id, txid, address):
            continue

        tx_url = INSIGHT_TX_API.format(txid=txid)
        tx_data = fetch_json(tx_url)
        if not tx_data:
            mark_tx_seen(user_id, txid, address)
            continue

        received_value = 0.0
        for vout in tx_data.get('vout', []):
            addresses = vout.get('scriptPubKey', {}).get('addresses', [])
            if address in addresses:
                value = vout.get('value')
                if value:
                    received_value += float(value)

        if received_value > 0:
            # Get sender addresses from inputs
            senders = []
            for vin in tx_data.get('vin', []):
                addr = vin.get('addr')
                if addr and addr != address:
                    senders.append(addr)  # Full address, not truncated
            
            senders_str = ", ".join(senders) if senders else "Unknown"
            
            tx_time = tx_data.get('time', 0)
            if tx_time:
                dt = datetime.fromtimestamp(tx_time, tz=timezone.utc)
                local_dt = dt + timedelta(hours=TZ_OFFSET_HOURS)
                time_str = local_dt.strftime('%Y-%m-%d %H:%M:%S')
            else:
                time_str = "Just now"
            
            deposits.append({'txid': txid, 'amount': received_value, 'time_str': time_str, 'senders': senders_str})
            logger.info(f"💰 Found: {received_value:.8f} DASH to {address[:16]}...")
        else:
            mark_tx_seen(user_id, txid, address)
    
    return deposits

# ===== HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("❌ Unauthorized")
        return
    
    addresses = get_addresses(user.id)
    text = _(user.id, "start", interval=CHECK_INTERVAL_SECONDS, count=len(addresses))
    await update.message.reply_text(text, reply_markup=await build_main_menu(user.id), parse_mode='Markdown')

async def panel_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if not user or not is_admin(user.id):
        await query.edit_message_text("❌ Unauthorized")
        return
    
    user_id = user.id
    data = query.data

    if data == "add":
        await query.edit_message_text(_(user_id, "add_prompt"), parse_mode='Markdown')
        return ADD_ADDRESS

    if data == "list":
        rows = get_addresses(user_id)
        if not rows:
            await query.edit_message_text(_(user_id, "no_addresses"), reply_markup=await build_main_menu(user_id))
            return
        
        lines = [_(user_id, "address_list_header")]
        keyboard = []
        for row in rows:
            lines.append(f"`{row['id']}. {row['address']}`")
            keyboard.append([InlineKeyboardButton(_(user_id, "btn_remove", id=row['id']), callback_data=f"remove:{row['id']}")])
        keyboard.append([InlineKeyboardButton(_(user_id, "btn_back"), callback_data="back_main")])
        await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return

    if data.startswith("remove:"):
        addr_id = int(data.split(":")[1])
        success = remove_address_from_db(user_id, addr_id)
        await query.edit_message_text(_(user_id, "remove_success") if success else "❌ Failed", reply_markup=await build_main_menu(user_id))
        return

    if data == "check_now":
        await query.edit_message_text(_(user_id, "checking"))
        price = get_dash_price_usd()
        count = 0
        for row in get_addresses(user_id):
            deposits = check_address_for_deposits(user_id, row['address'])
            for d in deposits:
                mark_tx_seen(user_id, d['txid'], row['address'])
                receipt_num = increment_receipt_counter(user_id)
                usd_value = d['amount'] * price if price > 0 else 0
                msg = _(user_id, "deposit_notification", num=receipt_num, address=row['address'], amount=d['amount'], usd_value=usd_value, time=d['time_str'], senders=d['senders'], txid=d['txid'])
                await context.bot.send_message(chat_id=user_id, text=msg, parse_mode='Markdown', disable_web_page_preview=True)
                count += 1
            time.sleep(2)
        
        await context.bot.send_message(chat_id=user_id, text=_(user_id, "scan_complete", count=count), reply_markup=await build_main_menu(user_id))
        return

    if data == "language":
        await query.edit_message_text(_(user_id, "main_menu"), reply_markup=await build_language_menu(user_id))
        return

    if data.startswith("lang_"):
        lang = data.split("_")[1]
        set_user_language(user_id, lang)
        await query.edit_message_text("🌐 Language changed", reply_markup=await build_main_menu(user_id))
        return

    if data == "back_main":
        await query.edit_message_text(_(user_id, "main_menu"), reply_markup=await build_main_menu(user_id))
        return

async def add_address_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not user or not is_admin(user.id):
        return ConversationHandler.END
    
    text = update.message.text.strip()
    address = text.split("|")[0].strip() if "|" in text else text
    
    if not validate_dash_address(address):
        await update.message.reply_text(_(user.id, "invalid_address"))
        return ADD_ADDRESS
    
    success, msg = add_address_to_db(user.id, address)
    await update.message.reply_text(msg, reply_markup=await build_main_menu(user.id))
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if user and is_admin(user.id):
        await update.message.reply_text("Cancelled", reply_markup=await build_main_menu(user.id))
    return ConversationHandler.END

async def periodic_check(context: ContextTypes.DEFAULT_TYPE):
    price = get_dash_price_usd()
    for admin_id in ADMIN_CHAT_IDS:
        try:
            count = 0
            for row in get_addresses(admin_id):
                # Seed old transactions on first scan - mark ALL as seen
                if not has_seen_transactions_for_address(admin_id, row['address']):
                    logger.info(f"🛡 Seeding old txs for {row['address'][:16]}...")
                    url = INSIGHT_ADDR_API.format(address=row['address'])
                    data = fetch_json(url)
                    if data:
                        for txid in data.get('transactions', [])[:50]:  # Mark first 50 as seen
                            mark_tx_seen(admin_id, txid, row['address'])
                        logger.info(f"✅ Marked {len(data.get('transactions', [])[:50])} old txs as seen")
                    continue  # Skip to next address
                
                deposits = check_address_for_deposits(admin_id, row['address'])
                for d in deposits:
                    mark_tx_seen(admin_id, d['txid'], row['address'])
                    receipt_num = increment_receipt_counter(admin_id)
                    usd_value = d['amount'] * price if price > 0 else 0
                    msg = _(admin_id, "deposit_notification", num=receipt_num, address=row['address'], amount=d['amount'], usd_value=usd_value, time=d['time_str'], senders=d['senders'], txid=d['txid'])
                    await context.bot.send_message(chat_id=admin_id, text=msg, parse_mode='Markdown', disable_web_page_preview=True)
                    count += 1
                time.sleep(2)
            if count > 0:
                logger.info(f"✨ Found {count} deposits for user {admin_id}")
        except Exception as e:
            logger.error(f"Scan error: {e}")

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN missing")
    if not ADMIN_CHAT_IDS:
        raise ValueError("ADMIN_CHAT_IDS missing")
    
    init_db()
    
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(panel_click, pattern="^add$")],
        states={ADD_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_address_received)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(panel_click))

    if app.job_queue:
        app.job_queue.run_repeating(periodic_check, interval=CHECK_INTERVAL_SECONDS, first=3)

    logger.info("=" * 50)
    logger.info("🚀 Bot Started (SQLite + Insight API)")
    logger.info(f"📊 Admins: {ADMIN_CHAT_IDS}")
    logger.info("=" * 50)

    app.run_polling()
