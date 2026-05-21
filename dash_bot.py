import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import aiohttp
import asyncpg
from dotenv import load_dotenv
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

load_dotenv()

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
ADMIN_CHAT_IDS = [int(x.strip()) for x in os.getenv("ADMIN_CHAT_IDS", "").split(",") if x.strip().isdigit()]

CHECK_INTERVAL_SECONDS = 60  # Every minute
TZ_OFFSET_HOURS = 4

# Insight API (official Dash explorer)
INSIGHT_ADDR_API = "https://insight.dash.org/insight-api/addr/{address}"
INSIGHT_TX_API = "https://insight.dash.org/insight-api/tx/{txid}"
EXPLORER_TX_URL = "https://insight.dash.org/insight/tx/{txid}"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

ADD_ADDRESS = 1
AUTHORIZE_USER = 2

# Global pool
_pool: Optional[asyncpg.Pool] = None

# User scan locks
USER_SCAN_LOCKS = {}

def get_user_scan_lock(user_id: int) -> asyncio.Lock:
    if user_id not in USER_SCAN_LOCKS:
        USER_SCAN_LOCKS[user_id] = asyncio.Lock()
    return USER_SCAN_LOCKS[user_id]

# -------------------------
# Rate limiter
# -------------------------
class RateLimiter:
    def __init__(self, max_calls_per_minute=30):
        self.max_calls_per_minute = max_calls_per_minute
        self.calls = []
        self.backoff_time = 0

    async def wait(self):
        now = time.time()
        self.calls = [t for t in self.calls if now - t < 60]
        if self.backoff_time > now:
            wait_time = self.backoff_time - now
            logger.warning(f"Rate limit backoff: waiting {wait_time:.1f}s")
            await asyncio.sleep(wait_time)
            return await self.wait()
        if len(self.calls) >= self.max_calls_per_minute:
            oldest = min(self.calls)
            wait_time = 60 - (now - oldest) + 2
            logger.warning(f"Rate limit reached, waiting {wait_time:.1f}s")
            self.backoff_time = now + wait_time
            await asyncio.sleep(wait_time)
            return await self.wait()
        self.calls.append(now)
        return True

rate_limiter = RateLimiter(max_calls_per_minute=30)

# -------------------------
# Database
# -------------------------
async def init_db():
    global _pool
    try:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        async with _pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS watched_addresses (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    address TEXT NOT NULL,
                    label TEXT,
                    added_at BIGINT NOT NULL,
                    UNIQUE(user_id, address)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_transactions (
                    txid TEXT NOT NULL,
                    address TEXT NOT NULL,
                    user_id BIGINT NOT NULL,
                    PRIMARY KEY (txid, address, user_id)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id BIGINT PRIMARY KEY,
                    language TEXT DEFAULT 'en'
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS authorized_users (
                    user_id BIGINT PRIMARY KEY,
                    added_by BIGINT,
                    created_at BIGINT NOT NULL
                )
            """)
        logger.info("✅ Database ready")
    except Exception as e:
        logger.error(f"Database error: {e}")
        raise

async def get_user_language(user_id: int) -> str:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT language FROM user_settings WHERE user_id = $1", user_id)
        return row['language'] if row else 'en'

async def set_user_language(user_id: int, lang: str):
    async with _pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_settings(user_id, language) VALUES ($1, $2) ON CONFLICT(user_id) DO UPDATE SET language=$2",
            user_id, lang
        )

async def add_address_to_db(user_id: int, address: str, label: str = "") -> Tuple[bool, str]:
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO watched_addresses(user_id, address, label, added_at) VALUES ($1, $2, $3, $4)",
                user_id, address.strip(), label.strip(), int(time.time())
            )
        return True, "✅ Address added"
    except asyncpg.UniqueViolationError:
        return False, "❌ Address already in your watchlist"

async def remove_address_from_db(user_id: int, address_id: int) -> bool:
    async with _pool.acquire() as conn:
        result = await conn.execute("DELETE FROM watched_addresses WHERE id = $1 AND user_id = $2", address_id, user_id)
        return result != "DELETE 0"

async def get_addresses(user_id: int) -> List:
    async with _pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, address, label FROM watched_addresses WHERE user_id = $1 ORDER BY id DESC", user_id)
        return rows

async def is_tx_seen(user_id: int, txid: str, address: str) -> bool:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM seen_transactions WHERE user_id = $1 AND txid = $2 AND address = $3", user_id, txid, address)
        return row is not None

async def has_seen_transactions_for_address(user_id: int, address: str) -> bool:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM seen_transactions WHERE user_id = $1 AND address = $2 LIMIT 1", user_id, address)
        return row is not None

async def mark_tx_seen(user_id: int, txid: str, address: str):
    async with _pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO seen_transactions(user_id, txid, address) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            user_id, txid, address
        )

async def add_authorized_user_to_db(user_id: int, added_by: int) -> bool:
    if is_super_admin_id(user_id):
        return False
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO authorized_users(user_id, added_by, created_at) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                user_id, added_by, int(time.time())
            )
        return True
    except:
        return False

async def remove_authorized_user_from_db(user_id: int) -> bool:
    async with _pool.acquire() as conn:
        result = await conn.execute("DELETE FROM authorized_users WHERE user_id = $1", user_id)
        return result != "DELETE 0"

async def is_db_authorized_user(user_id: int) -> bool:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM authorized_users WHERE user_id = $1", user_id)
        return row is not None

async def get_authorized_users() -> List:
    async with _pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, added_by FROM authorized_users ORDER BY created_at DESC")
        return rows

async def get_all_active_user_ids() -> List[int]:
    users = set(ADMIN_CHAT_IDS)
    async with _pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM authorized_users")
        for row in rows:
            users.add(int(row['user_id']))
    return sorted(users)

# -------------------------
# Translations
# -------------------------
TRANSLATIONS = {
    "en": {
        "unauthorized": "❌ Unauthorized",
        "start": "🤖 **Dash Deposit Bot Active**\n\n⏱ Scan: every {interval}s\n📊 Watching: {count} address(es)\n⚡ Insight API\n\nUse menu below 👇",
        "main_menu": "Main Menu:",
        "add_prompt": "➕ **Add DASH Address**\n\nSend address (starts with X or 7, 34 chars)\n\nOptional label:\n`Xaddress... | My Wallet`",
        "invalid_address": "❌ Invalid DASH address\nMust start with X or 7, 34 chars",
        "address_added": "✅ Address added",
        "already_exists": "❌ Address already in watchlist",
        "no_addresses": "📭 No addresses",
        "address_list_header": "📚 **Watched Addresses:**\n",
        "remove_success": "✅ Removed",
        "remove_fail": "❌ Not found",
        "checking": "🔄 Scanning...",
        "scan_complete": "✅ Scan complete!\n💰 Found: {count} new deposit(s)",
        "cancel": "Cancelled",
        "deposit_notification": "💰 **NEW DASH DEPOSIT!** 💰\n\n📥 **Address:** `{address}`{label}\n💵 **Amount:** `{amount:.8f}` DASH\n🕒 **Time:** {time}\n🔗 **TX:** `{txid_short}...`\n🌐 [View]({explorer})",
        "language_changed": "🌐 Language changed",
        "choose_language": "🌐 Choose language:",
        "btn_add_address": "➕ Add Address",
        "btn_my_addresses": "📚 My Addresses",
        "btn_check_now": "🔄 Check Now",
        "btn_language": "🌐 Language",
        "btn_back": "⬅️ Back",
        "btn_remove": "❌ Remove #{id}",
        "btn_authorization": "🛡 Authorization",
        "btn_authorize_user": "✅ Authorize User",
        "btn_authorized_users": "👥 Authorized Users",
        "authorization_menu": "🛡 Authorization Menu:",
        "authorize_prompt": "Send the user ID to authorize:",
        "authorize_success": "✅ User {target_id} authorized",
        "authorize_exists": "❌ User {target_id} already authorized",
        "authorized_users_empty": "📭 No authorized users",
        "authorized_users_header": "👥 **Authorized Users:**\n",
        "btn_remove_user": "❌ Remove {target_id}",
        "remove_user_success": "✅ User removed",
        "remove_user_fail": "❌ User not found",
        "super_admin_only": "❌ Only main admins",
        "invalid_user_id": "❌ Invalid user ID",
    },
    "hy": {
        "unauthorized": "❌ Մուտքը արգելված է",
        "start": "🤖 **Dash Deposit Bot-ը ակտիվ է**\n\n⏱ Ստուգում ամեն {interval}վ\n📊 Դիտարկվածներ՝ {count}\n⚡ Insight API\n\nՕգտագործեք մենյուն 👇",
        "main_menu": "Գլխավոր մենյու:",
        "add_prompt": "➕ **Ավելացնել DASH հասցե**\n\nՀասցե (սկսվում է X կամ 7, 34 նիշ)",
        "invalid_address": "❌ Սխալ DASH հասցե",
        "address_added": "✅ Ավելացված է",
        "already_exists": "❌ Հասցեն արդեն կա",
        "no_addresses": "📭 Հասցեներ չկան",
        "address_list_header": "📚 **Դիտարկվող հասցեներ:**\n",
        "remove_success": "✅ Հեռացված է",
        "remove_fail": "❌ Չի գտնվել",
        "checking": "🔄 Ստուգում...",
        "scan_complete": "✅ Ստուգումն ավարտվել է!\n💰 Գտնված՝ {count}",
        "cancel": "Չեղարկված",
        "deposit_notification": "💰 **ՆՈՐ DASH ՄՈՒՏՔ!** 💰\n\n📥 **Հասցե՝** `{address}`{label}\n💵 **Գումար՝** `{amount:.8f}` DASH\n🕒 **Ժամ՝** {time}\n🔗 **TX՝** `{txid_short}...`",
        "language_changed": "🌐 Լեզուն փոխված է",
        "choose_language": "🌐 Ընտրեք լեզուն:",
        "btn_add_address": "➕ Ավելացնել",
        "btn_my_addresses": "📚 Իմ հասցեներ",
        "btn_check_now": "🔄 Ստուգել",
        "btn_language": "🌐 Լեզու",
        "btn_back": "⬅️ Հետ",
        "btn_remove": "❌ Հեռացնել #{id}",
        "btn_authorization": "🛡 Թույլտվություն",
        "btn_authorize_user": "✅ Թույլատրել",
        "btn_authorized_users": "👥 Թույլատրված",
        "authorization_menu": "🛡 Թույլտվություն:",
        "authorize_prompt": "User ID-ն ուղարկեք:",
        "authorize_success": "✅ Թույլատրված է {target_id}",
        "authorize_exists": "❌ {target_id} արդեն թույլատրված է",
        "authorized_users_empty": "📭 Չկան",
        "authorized_users_header": "👥 **Թույլատրված:**\n",
        "btn_remove_user": "❌ Հեռացնել {target_id}",
        "remove_user_success": "✅ Հեռացված է",
        "remove_user_fail": "❌ Չի գտնվել",
        "super_admin_only": "❌ Միայն ադմիններ",
        "invalid_user_id": "❌ Սխալ ID",
    }
}

async def _(user_id: int, key: str, **kwargs) -> str:
    lang = await get_user_language(user_id)
    text = TRANSLATIONS.get(lang, TRANSLATIONS["en"]).get(key, key)
    if kwargs:
        text = text.format(**kwargs)
    return text

# -------------------------
# API helpers
# -------------------------
async def fetch_json(url: str) -> Optional[dict]:
    await rate_limiter.wait()
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15), headers=headers) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    logger.debug(f"API {resp.status}: {url[:50]}")
                    return None
    except Exception as e:
        logger.error(f"Request error: {e}")
        return None

async def check_address_for_deposits(user_id: int, address: str) -> List[dict]:
    deposits = []
    url = INSIGHT_ADDR_API.format(address=address)
    data = await fetch_json(url)
    if not data:
        return deposits

    confirmed_txs = data.get('transactions', [])
    for txid in confirmed_txs[:20]:
        if await is_tx_seen(user_id, txid, address):
            continue

        tx_url = INSIGHT_TX_API.format(txid=txid)
        tx_data = await fetch_json(tx_url)
        if not tx_data:
            await mark_tx_seen(user_id, txid, address)
            continue

        received_value = 0.0
        for vout in tx_data.get('vout', []):
            addresses = vout.get('scriptPubKey', {}).get('addresses', [])
            if address in addresses:
                value = vout.get('value')
                if value is not None:
                    try:
                        received_value += float(value)
                    except (ValueError, TypeError):
                        pass

        if received_value > 0:
            tx_time = tx_data.get('time', 0)
            if tx_time:
                dt = datetime.fromtimestamp(tx_time, tz=timezone.utc)
                local_dt = dt + timedelta(hours=TZ_OFFSET_HOURS)
                time_str = local_dt.strftime('%Y-%m-%d %H:%M:%S')
            else:
                time_str = "Just now"
                tx_time = int(time.time())
            
            deposits.append({
                'txid': txid,
                'amount': received_value,
                'time_str': time_str,
                'timestamp': tx_time
            })
            logger.info(f"💰 Found deposit: {received_value:.8f} DASH to {address[:16]}...")
        else:
            await mark_tx_seen(user_id, txid, address)
    
    return deposits

# -------------------------
# Helper functions
# -------------------------
def validate_dash_address(address: str) -> bool:
    return address and (address.startswith('X') or address.startswith('7')) and 33 <= len(address) <= 35

def is_super_admin_id(user_id: int) -> bool:
    return user_id in ADMIN_CHAT_IDS

async def is_admin(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    return is_super_admin_id(user.id) or await is_db_authorized_user(user.id)

def parse_add_input(text: str) -> Tuple[str, str]:
    text = text.strip()
    if "|" in text:
        address, label = text.split("|", 1)
        return address.strip(), label.strip()
    return text, ""

def parse_user_id_input(text: str) -> Optional[int]:
    text = (text or "").strip()
    if text.isdigit():
        return int(text)
    return None

async def build_main_menu(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(await _(user_id, "btn_add_address"), callback_data="add")],
        [InlineKeyboardButton(await _(user_id, "btn_my_addresses"), callback_data="list")],
        [InlineKeyboardButton(await _(user_id, "btn_check_now"), callback_data="check_now")],
        [InlineKeyboardButton(await _(user_id, "btn_language"), callback_data="language")],
    ]
    if is_super_admin_id(user_id):
        rows.append([InlineKeyboardButton(await _(user_id, "btn_authorization"), callback_data="auth_panel")])
    return InlineKeyboardMarkup(rows)

async def build_auth_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(await _(user_id, "btn_authorize_user"), callback_data="auth_add")],
        [InlineKeyboardButton(await _(user_id, "btn_authorized_users"), callback_data="auth_list")],
        [InlineKeyboardButton(await _(user_id, "btn_back"), callback_data="back_main")],
    ])

async def build_language_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")],
        [InlineKeyboardButton("🇦🇲 Հայերեն", callback_data="lang_hy")],
        [InlineKeyboardButton(await _(user_id, "btn_back"), callback_data="back_main")],
    ])

# -------------------------
# Telegram Handlers
# -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not await is_admin(update):
        await update.message.reply_text("❌ Unauthorized")
        return
    
    user_id = user.id
    addresses = await get_addresses(user_id)
    count = len(addresses)
    text = await _(user_id, "start", interval=CHECK_INTERVAL_SECONDS, count=count)
    await update.message.reply_text(text, reply_markup=await build_main_menu(user_id), parse_mode='Markdown')

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not await is_admin(update):
        await update.message.reply_text("❌ Unauthorized")
        return
    
    text = await _(user.id, "main_menu")
    await update.message.reply_text(text, reply_markup=await build_main_menu(user.id))

async def panel_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if not user or not await is_admin(update):
        await query.edit_message_text("❌ Unauthorized")
        return
    
    user_id = user.id
    data = query.data

    if data == "add":
        text = await _(user_id, "add_prompt")
        await query.edit_message_text(text, parse_mode='Markdown')
        return ADD_ADDRESS

    if data == "list":
        rows = await get_addresses(user_id)
        if not rows:
            text = await _(user_id, "no_addresses")
            await query.edit_message_text(text, reply_markup=await build_main_menu(user_id))
            return
        
        header = await _(user_id, "address_list_header")
        lines = [header]
        keyboard = []
        for row in rows:
            label = f" - {row['label']}" if row['label'] else ""
            lines.append(f"`{row['id']}. {row['address']}`{label}")
            remove_text = await _(user_id, "btn_remove", id=row['id'])
            keyboard.append([InlineKeyboardButton(remove_text, callback_data=f"remove:{row['id']}")])
        
        back_text = await _(user_id, "btn_back")
        keyboard.append([InlineKeyboardButton(back_text, callback_data="back_main")])
        await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return

    if data.startswith("remove:"):
        addr_id = int(data.split(":", 1)[1])
        success = await remove_address_from_db(user_id, addr_id)
        msg_key = "remove_success" if success else "remove_fail"
        text = await _(user_id, msg_key)
        await query.edit_message_text(text, reply_markup=await build_main_menu(user_id))
        return

    if data == "check_now":
        checking_text = await _(user_id, "checking")
        await query.edit_message_text(checking_text)
        count = await check_all_addresses_for_user(user_id, context)
        complete_text = await _(user_id, "scan_complete", count=count)
        await context.bot.send_message(
            chat_id=user_id,
            text=complete_text,
            reply_markup=await build_main_menu(user_id)
        )
        return

    if data == "language":
        text = await _(user_id, "choose_language")
        await query.edit_message_text(text, reply_markup=await build_language_menu(user_id))
        return

    if data.startswith("lang_"):
        lang = data.split("_")[1]
        await set_user_language(user_id, lang)
        text = await _(user_id, "language_changed")
        await query.edit_message_text(text, reply_markup=await build_main_menu(user_id))
        return

    if data == "auth_panel":
        if not is_super_admin_id(user_id):
            text = await _(user_id, "super_admin_only")
            await query.edit_message_text(text, reply_markup=await build_main_menu(user_id))
            return
        text = await _(user_id, "authorization_menu")
        await query.edit_message_text(text, reply_markup=await build_auth_menu(user_id))
        return

    if data == "auth_add":
        if not is_super_admin_id(user_id):
            text = await _(user_id, "super_admin_only")
            await query.edit_message_text(text, reply_markup=await build_main_menu(user_id))
            return
        text = await _(user_id, "authorize_prompt")
        await query.edit_message_text(text)
        return AUTHORIZE_USER

    if data == "auth_list":
        if not is_super_admin_id(user_id):
            text = await _(user_id, "super_admin_only")
            await query.edit_message_text(text, reply_markup=await build_main_menu(user_id))
            return
        
        rows = await get_authorized_users()
        if not rows:
            text = await _(user_id, "authorized_users_empty")
            await query.edit_message_text(text, reply_markup=await build_auth_menu(user_id))
            return
        
        header = await _(user_id, "authorized_users_header")
        lines = [header]
        keyboard = []
        for row in rows:
            lines.append(f"`{row['user_id']}`")
            remove_text = await _(user_id, "btn_remove_user", target_id=row['user_id'])
            keyboard.append([InlineKeyboardButton(remove_text, callback_data=f"auth_remove:{row['user_id']}")])
        
        back_text = await _(user_id, "btn_back")
        keyboard.append([InlineKeyboardButton(back_text, callback_data="auth_panel")])
        await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return

    if data.startswith("auth_remove:"):
        if not is_super_admin_id(user_id):
            text = await _(user_id, "super_admin_only")
            await query.edit_message_text(text, reply_markup=await build_main_menu(user_id))
            return
        
        target_user_id = int(data.split(":", 1)[1])
        success = await remove_authorized_user_from_db(target_user_id)
        msg_key = "remove_user_success" if success else "remove_user_fail"
        text = await _(user_id, msg_key)
        await query.edit_message_text(text, reply_markup=await build_auth_menu(user_id))
        return

    if data == "back_main":
        text = await _(user_id, "main_menu")
        await query.edit_message_text(text, reply_markup=await build_main_menu(user_id))
        return

async def add_address_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not user or not await is_admin(update):
        await update.message.reply_text("❌ Unauthorized")
        return ConversationHandler.END
    
    user_id = user.id
    text = update.message.text.strip()
    address, label = parse_add_input(text)
    
    if not address or not validate_dash_address(address):
        invalid_text = await _(user_id, "invalid_address")
        await update.message.reply_text(invalid_text)
        return ADD_ADDRESS
    
    success, msg = await add_address_to_db(user_id, address, label)
    reply_msg = msg
    await update.message.reply_text(reply_msg, reply_markup=await build_main_menu(user_id))
    return ConversationHandler.END

async def authorize_user_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not user or not is_super_admin_id(user.id):
        await update.message.reply_text("❌ Unauthorized")
        return ConversationHandler.END
    
    user_id = user.id
    text = update.message.text.strip()
    target_user_id = parse_user_id_input(text)
    
    if not target_user_id:
        invalid_text = await _(user_id, "invalid_user_id")
        await update.message.reply_text(invalid_text, reply_markup=await build_auth_menu(user_id))
        return AUTHORIZE_USER
    
    added = await add_authorized_user_to_db(target_user_id, user_id)
    key = "authorize_success" if added else "authorize_exists"
    msg_text = await _(user_id, key, target_id=target_user_id)
    await update.message.reply_text(msg_text, reply_markup=await build_auth_menu(user_id), parse_mode="Markdown")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if user and await is_admin(update):
        cancel_text = await _(user.id, "cancel")
        await update.message.reply_text(cancel_text, reply_markup=await build_main_menu(user.id))
    else:
        await update.message.reply_text("Cancelled")
    return ConversationHandler.END

async def notify_deposit(user_id: int, context: ContextTypes.DEFAULT_TYPE, address: str, label: str, txid: str, amount: float, time_str: str):
    label_text = f"\n🏷 **Label:** {label}" if label else ""
    explorer_link = EXPLORER_TX_URL.format(txid=txid)
    txid_short = txid[:16]
    
    msg = await _(user_id, "deposit_notification",
                  address=address,
                  label=label_text,
                  amount=amount,
                  time=time_str,
                  txid_short=txid_short,
                  explorer=explorer_link)
    
    await context.bot.send_message(chat_id=user_id, text=msg, parse_mode='Markdown', disable_web_page_preview=True)

async def check_single_address_for_user(user_id: int, address: str, label: str, context: ContextTypes.DEFAULT_TYPE) -> int:
    deposits = await check_address_for_deposits(user_id, address)
    for d in deposits:
        await mark_tx_seen(user_id, d['txid'], address)
        await notify_deposit(user_id, context, address, label, d['txid'], d['amount'], d['time_str'])
    return len(deposits)

async def check_all_addresses_for_user(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
    lock = get_user_scan_lock(user_id)

    if lock.locked():
        logger.info(f"⏭ Skipping scan for user {user_id}, previous scan still running")
        return 0

    async with lock:
        rows = await get_addresses(user_id)
        if not rows:
            return 0
        
        logger.info(f"🔍 Checking {len(rows)} addresses for user {user_id}")
        total = 0
        for i, row in enumerate(rows):
            address = row['address']
            label = row['label'] or ""

            logger.info(f"📌 [{i+1}/{len(rows)}] {address[:16]}...")

            if not await has_seen_transactions_for_address(user_id, address):
                logger.info(f"🛡 Seeding old transactions for {address[:16]}...")
                # Just mark first 20 as seen
                url = INSIGHT_ADDR_API.format(address=address)
                data = await fetch_json(url)
                if data:
                    for txid in data.get('transactions', [])[:20]:
                        await mark_tx_seen(user_id, txid, address)
                if i < len(rows)-1:
                    await asyncio.sleep(1)
                continue

            found = await check_single_address_for_user(user_id, address, label, context)
            total += found
            if found:
                logger.info(f"✨ Found {found} deposit(s) for user {user_id}")
            if i < len(rows)-1:
                await asyncio.sleep(3)
        
        return total

async def periodic_check(context: ContextTypes.DEFAULT_TYPE):
    for admin_id in await get_all_active_user_ids():
        try:
            logger.info(f"🔍 Running scheduled scan for user {admin_id}")
            start = time.time()
            count = await check_all_addresses_for_user(admin_id, context)
            logger.info(f"✅ Scan for {admin_id}: {count} deposit(s) found ({time.time()-start:.1f}s)")
        except Exception as e:
            logger.error(f"Scan error for user {admin_id}: {e}", exc_info=True)

# -------------------------
# MAIN
# -------------------------
async def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN missing")
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL missing")
    if not ADMIN_CHAT_IDS:
        raise ValueError("ADMIN_CHAT_IDS missing")

    await init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(panel_click, pattern="^add$"),
            CallbackQueryHandler(panel_click, pattern="^auth_add$"),
        ],
        states={
            ADD_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_address_received)],
            AUTHORIZE_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, authorize_user_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(panel_click))

    if app.job_queue:
        app.job_queue.run_repeating(periodic_check, interval=CHECK_INTERVAL_SECONDS, first=3)

    logger.info("=" * 50)
    logger.info("🚀 Bot Started (Insight API)")
    logger.info(f"📊 Admins: {ADMIN_CHAT_IDS}")
    logger.info(f"⏱ Scan every {CHECK_INTERVAL_SECONDS}s")
    logger.info("=" * 50)

    async with app:
        await app.start()
        await app.updater.start_polling()
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            logger.info("Shutdown signal received")
        finally:
            if _pool:
                await _pool.close()
                logger.info("Database pool closed")

if __name__ == "__main__":
    asyncio.run(main())
