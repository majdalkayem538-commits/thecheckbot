import csv
import io
import json
import logging
import sqlite3
import time
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
from typing import Tuple, Dict, Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================================
# إعدادات من Environment Variables
# =========================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
API_SYRIA_KEY = os.getenv("API_SYRIA_KEY", "")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "your_support_username")

USE_ALLOWLIST = os.getenv("USE_ALLOWLIST", "false").lower() == "true"

ALLOWED_USER_IDS = {
    int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
}

ANTI_SPAM_SECONDS = int(os.getenv("ANTI_SPAM_SECONDS", "4"))

SYRIATEL_GSMS = [
    x.strip() for x in os.getenv("SYRIATEL_GSMS", "").split(",")
    if x.strip()
]

SHAMCASH_ACCOUNTS = [
    x.strip() for x in os.getenv("SHAMCASH_ACCOUNTS", "").split(",")
    if x.strip()
]

DB_NAME = os.getenv("DB_NAME", "payments_pro.db")
BASE_URL = os.getenv("BASE_URL", "https://apisyria.com/api/v1")

STATE_NONE = "none"
STATE_WAIT_BALANCE_CODE = "wait_balance_code"
STATE_WAIT_ADMIN_SEARCH = "wait_admin_search"
STATE_WAIT_SHAMCASH_ACCOUNT = "wait_shamcash_account"

# =========================================
# اللوج
# =========================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# =========================================
# Session مع retry
# =========================================

def build_session() -> requests.Session:
    session = requests.Session()

    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )

    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update({
        "X-Api-Key": API_SYRIA_KEY,
        "Accept": "application/json",
    })
    return session

http = build_session()

# =========================================
# أدوات مساعدة عامة
# =========================================

user_last_action_time: Dict[int, float] = {}

def normalize_digits(text: str) -> str:
    arabic_to_english = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    return text.translate(arabic_to_english).strip()

def tx_input_is_valid(tx_number: str) -> bool:
    return tx_number.isdigit() and 3 <= len(tx_number) <= 30

def safe_json_dump(data: Any) -> str:
    try:
        return json.dumps(data, ensure_ascii=False)
    except Exception:
        return "{}"

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def is_user_allowed(user_id: int) -> bool:
    if not USE_ALLOWLIST:
        return True
    return user_id in ALLOWED_USER_IDS or user_id == ADMIN_ID

def is_spamming(user_id: int) -> Tuple[bool, int]:
    current = time.time()
    last = user_last_action_time.get(user_id, 0)
    diff = current - last
    if diff < ANTI_SPAM_SECONDS:
        wait_for = max(1, int(ANTI_SPAM_SECONDS - diff))
        return True, wait_for
    user_last_action_time[user_id] = current
    return False, 0

# =========================================
# قاعدة البيانات
# =========================================

def db_connect():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        created_at TEXT,
        last_seen_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT,
        tx_number TEXT NOT NULL,
        matched_gsm TEXT,
        matched_cash_code TEXT,
        matched_account TEXT,
        amount TEXT,
        currency TEXT,
        tx_status_text TEXT,
        tx_date TEXT,
        tx_from_number TEXT,
        tx_to_number TEXT,
        note TEXT,
        telegram_user_id INTEGER,
        telegram_username TEXT,
        status TEXT NOT NULL,
        raw_response TEXT,
        created_at TEXT,
        UNIQUE(provider, tx_number)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS balance_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT,
        telegram_user_id INTEGER,
        telegram_username TEXT,
        input_code TEXT,
        gsm TEXT,
        cash_code TEXT,
        account_address TEXT,
        balance TEXT,
        currency TEXT,
        status TEXT,
        raw_response TEXT,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS duplicate_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT,
        tx_number TEXT,
        telegram_user_id INTEGER,
        telegram_username TEXT,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS error_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT,
        telegram_user_id INTEGER,
        telegram_username TEXT,
        details TEXT,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    conn.commit()
    conn.close()

    if get_setting("maintenance_mode") is None:
        set_setting("maintenance_mode", "off")

def upsert_user(user_id: int, username: str, first_name: str):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (user_id, username, first_name, created_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_seen_at=excluded.last_seen_at
    """, (user_id, username, first_name, now_str(), now_str()))
    conn.commit()
    conn.close()

def get_setting(key: str) -> Optional[str]:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else None

def set_setting(key: str, value: str):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO settings (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, value))
    conn.commit()
    conn.close()

def maintenance_mode() -> bool:
    return get_setting("maintenance_mode") == "on"

def log_error(scope: str, telegram_user_id: int, telegram_username: str, details: str):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO error_logs (scope, telegram_user_id, telegram_username, details, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (scope, telegram_user_id, telegram_username, details, now_str()))
    conn.commit()
    conn.close()

def is_tx_already_used(provider: str, tx_number: str) -> bool:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM transactions WHERE provider = ? AND tx_number = ? LIMIT 1",
        (provider, tx_number)
    )
    row = cur.fetchone()
    conn.close()
    return row is not None

def save_duplicate_attempt(provider: str, tx_number: str, telegram_user_id: int, telegram_username: str):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO duplicate_attempts (provider, tx_number, telegram_user_id, telegram_username, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (provider, tx_number, telegram_user_id, telegram_username, now_str()))
    conn.commit()
    conn.close()

def save_transaction(
    provider: str,
    tx_number: str,
    matched_gsm: str,
    matched_cash_code: str,
    matched_account: str,
    amount: str,
    currency: str,
    tx_status_text: str,
    tx_date: str,
    tx_from_number: str,
    tx_to_number: str,
    note: str,
    telegram_user_id: int,
    telegram_username: str,
    status: str,
    raw_response: str = ""
):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO transactions (
            provider,
            tx_number,
            matched_gsm,
            matched_cash_code,
            matched_account,
            amount,
            currency,
            tx_status_text,
            tx_date,
            tx_from_number,
            tx_to_number,
            note,
            telegram_user_id,
            telegram_username,
            status,
            raw_response,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        provider,
        tx_number,
        matched_gsm,
        matched_cash_code,
        matched_account,
        amount,
        currency,
        tx_status_text,
        tx_date,
        tx_from_number,
        tx_to_number,
        note,
        telegram_user_id,
        telegram_username,
        status,
        raw_response,
        now_str()
    ))
    conn.commit()
    conn.close()

def save_balance_request(
    provider: str,
    telegram_user_id: int,
    telegram_username: str,
    input_code: str,
    gsm: str,
    cash_code: str,
    account_address: str,
    balance: str,
    currency: str,
    status: str,
    raw_response: str = ""
):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO balance_requests (
            provider,
            telegram_user_id,
            telegram_username,
            input_code,
            gsm,
            cash_code,
            account_address,
            balance,
            currency,
            status,
            raw_response,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        provider,
        telegram_user_id,
        telegram_username,
        input_code,
        gsm,
        cash_code,
        account_address,
        balance,
        currency,
        status,
        raw_response,
        now_str()
    ))
    conn.commit()
    conn.close()

def get_user_last_transactions(user_id: int, limit: int = 5):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT provider, tx_number, amount, currency, tx_status_text, tx_date, status, created_at
        FROM transactions
        WHERE telegram_user_id = ?
        ORDER BY id DESC
        LIMIT ?
    """, (user_id, limit))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_last_transactions(limit: int = 10):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT provider, tx_number, matched_gsm, matched_cash_code, matched_account, amount, currency,
               tx_status_text, telegram_username, status, created_at
        FROM transactions
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows

def stats_summary():
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) AS c FROM users")
    users_count = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM transactions")
    tx_count = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM transactions WHERE status='approved'")
    approved_count = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM transactions WHERE status='fake'")
    fake_count = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM duplicate_attempts")
    duplicate_count = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM balance_requests")
    balance_count = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM error_logs")
    error_count = cur.fetchone()["c"]

    conn.close()
    return {
        "users": users_count,
        "transactions": tx_count,
        "approved": approved_count,
        "fake": fake_count,
        "duplicates": duplicate_count,
        "balance_requests": balance_count,
        "errors": error_count,
    }

def today_summary():
    today = datetime.now().strftime("%Y-%m-%d")
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) AS c FROM transactions WHERE created_at LIKE ?", (f"{today}%",))
    tx_count = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM transactions WHERE created_at LIKE ? AND status='approved'", (f"{today}%",))
    approved_count = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM transactions WHERE created_at LIKE ? AND status='fake'", (f"{today}%",))
    fake_count = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM balance_requests WHERE created_at LIKE ?", (f"{today}%",))
    balance_count = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM duplicate_attempts WHERE created_at LIKE ?", (f"{today}%",))
    duplicate_count = cur.fetchone()["c"]

    conn.close()
    return {
        "today": today,
        "transactions": tx_count,
        "approved": approved_count,
        "fake": fake_count,
        "balance_requests": balance_count,
        "duplicates": duplicate_count,
    }

def get_last_duplicate_attempts(limit: int = 10):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT provider, tx_number, telegram_username, created_at
        FROM duplicate_attempts
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows

def search_transactions(keyword: str, limit: int = 10):
    conn = db_connect()
    cur = conn.cursor()
    like_q = f"%{keyword}%"
    cur.execute("""
        SELECT provider, tx_number, matched_gsm, matched_cash_code, matched_account, amount, currency,
               tx_status_text, tx_date, tx_from_number, tx_to_number, note, telegram_username, status, created_at
        FROM transactions
        WHERE tx_number LIKE ?
           OR matched_gsm LIKE ?
           OR matched_cash_code LIKE ?
           OR matched_account LIKE ?
           OR tx_from_number LIKE ?
           OR tx_to_number LIKE ?
        ORDER BY id DESC
        LIMIT ?
    """, (like_q, like_q, like_q, like_q, like_q, like_q, limit))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_last_errors(limit: int = 10):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT scope, telegram_username, details, created_at
        FROM error_logs
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows

def export_transactions_csv() -> io.BytesIO:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT provider, tx_number, matched_gsm, matched_cash_code, matched_account, amount, currency,
               tx_status_text, tx_date, tx_from_number, tx_to_number, note, telegram_user_id,
               telegram_username, status, created_at
        FROM transactions
        ORDER BY id DESC
    """)
    rows = cur.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "provider", "tx_number", "matched_gsm", "matched_cash_code", "matched_account",
        "amount", "currency", "tx_status_text", "tx_date", "tx_from_number", "tx_to_number",
        "note", "telegram_user_id", "telegram_username", "status", "created_at"
    ])

    for row in rows:
        writer.writerow([
            row["provider"], row["tx_number"], row["matched_gsm"], row["matched_cash_code"],
            row["matched_account"], row["amount"], row["currency"], row["tx_status_text"],
            row["tx_date"], row["tx_from_number"], row["tx_to_number"], row["note"],
            row["telegram_user_id"], row["telegram_username"], row["status"], row["created_at"]
        ])

    mem = io.BytesIO()
    mem.write(output.getvalue().encode("utf-8-sig"))
    mem.seek(0)
    return mem

# =========================================
# واجهات الأزرار
# =========================================

def home_keyboard(user_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("✅ التحقق من عملية سيريتل", callback_data="new_check_syriatel")],
        [InlineKeyboardButton("✅ التحقق من عملية شام كاش", callback_data="new_check_shamcash")],
        [InlineKeyboardButton("💰 رصيد سيريتل عبر الكود", callback_data="check_balance_syriatel")],
        [InlineKeyboardButton("💰 رصيد شام كاش", callback_data="check_balance_shamcash")],
        [InlineKeyboardButton("📂 آخر عملياتي", callback_data="my_last_ops")],
        [InlineKeyboardButton("☎️ الدعم", callback_data="support")],
    ]

    if is_admin(user_id):
        keyboard.append([InlineKeyboardButton("🛠 لوحة الأدمن", callback_data="admin_panel")])

    return InlineKeyboardMarkup(keyboard)

def action_keyboard(user_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("✅ عملية سيريتل جديدة", callback_data="new_check_syriatel")],
        [InlineKeyboardButton("✅ عملية شام كاش جديدة", callback_data="new_check_shamcash")],
        [InlineKeyboardButton("💰 رصيد سيريتل", callback_data="check_balance_syriatel")],
        [InlineKeyboardButton("💰 رصيد شام كاش", callback_data="check_balance_shamcash")],
        [InlineKeyboardButton("📂 آخر عملياتي", callback_data="my_last_ops")],
        [InlineKeyboardButton("🏠 الصفحة الرئيسية", callback_data="home")],
        [InlineKeyboardButton("☎️ الدعم", callback_data="support")],
    ]

    if is_admin(user_id):
        keyboard.append([InlineKeyboardButton("🛠 لوحة الأدمن", callback_data="admin_panel")])

    return InlineKeyboardMarkup(keyboard)

def admin_panel_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("📄 آخر العمليات", callback_data="admin_last")],
        [InlineKeyboardButton("📊 الإحصائيات", callback_data="admin_stats")],
        [InlineKeyboardButton("📅 عمليات اليوم", callback_data="admin_today")],
        [InlineKeyboardButton("🔁 محاولات التكرار", callback_data="admin_duplicates")],
        [InlineKeyboardButton("🔎 البحث عن عملية", callback_data="admin_search")],
        [InlineKeyboardButton("⚠️ آخر الأخطاء", callback_data="admin_errors")],
        [InlineKeyboardButton("📤 تصدير CSV", callback_data="admin_export")],
        [InlineKeyboardButton("🟢 تشغيل الصيانة", callback_data="admin_maint_on"),
         InlineKeyboardButton("🔴 إيقاف الصيانة", callback_data="admin_maint_off")],
        [InlineKeyboardButton("🏠 الصفحة الرئيسية", callback_data="home")],
    ]
    return InlineKeyboardMarkup(keyboard)

# =========================================
# API - Syriatel
# =========================================

def check_syriatel_tx_multi(tx_number: str) -> Tuple[bool, Dict[str, Any]]:
    all_attempts = []

    for gsm in SYRIATEL_GSMS:
        try:
            params = {
                "resource": "syriatel",
                "action": "find_tx",
                "tx": tx_number,
                "gsm": gsm,
                "period": "all"
            }

            response = http.get(BASE_URL, params=params, timeout=25)
            response.raise_for_status()
            data = response.json()

            all_attempts.append({"gsm": gsm, "response": data})

            if not data.get("success"):
                continue

            payload = data.get("data", {})
            found = payload.get("found", False)
            if not found:
                continue

            transaction = payload.get("transaction", {})
            account = payload.get("account", {})

            tx_to = str(transaction.get("to", "")).strip()
            account_gsm = str(account.get("gsm", "")).strip()

            if account_gsm == gsm or tx_to == gsm:
                return True, {
                    "matched_gsm": gsm,
                    "transaction": {
                        "transaction_no": str(transaction.get("transaction_no", tx_number)).strip(),
                        "amount": str(transaction.get("amount", "غير معروف")).strip(),
                        "date": str(transaction.get("date", "غير معروف")).strip(),
                        "from": str(transaction.get("from", "غير معروف")).strip(),
                        "to": str(transaction.get("to", "غير معروف")).strip(),
                    },
                    "status_text": "ناجحة",
                    "provider": "syriatel",
                    "all_attempts": all_attempts
                }

        except Exception as e:
            all_attempts.append({"gsm": gsm, "error": str(e)})
            continue

    return False, {
        "status_text": "غير موجودة أو غير ناجحة",
        "provider": "syriatel",
        "all_attempts": all_attempts
    }

def check_syriatel_balance_by_code(code: str) -> Tuple[bool, Dict[str, Any]]:
    try:
        params = {
            "resource": "syriatel",
            "action": "balance",
            "gsm": code
        }

        response = http.get(BASE_URL, params=params, timeout=25)
        response.raise_for_status()
        data = response.json()

        if not data.get("success"):
            return False, data

        payload = data.get("data", {})
        gsm = str(payload.get("gsm", "")).strip()
        cash_code = str(payload.get("cash_code", "")).strip()
        balance = str(payload.get("balance", "")).strip()

        if not gsm and not cash_code:
            return False, data

        return True, {
            "gsm": gsm,
            "cash_code": cash_code,
            "balance": balance,
            "response": data
        }

    except Exception as e:
        return False, {"error": str(e)}

def get_cash_code_from_number(gsm: str) -> str:
    try:
        params = {
            "resource": "syriatel",
            "action": "balance",
            "gsm": gsm
        }

        response = http.get(BASE_URL, params=params, timeout=25)
        response.raise_for_status()
        data = response.json()

        if not data.get("success"):
            return "غير متوفر"

        payload = data.get("data", {})
        cash_code = payload.get("cash_code", "")
        return str(cash_code).strip() if cash_code else "غير متوفر"

    except Exception:
        return "غير متوفر"

# =========================================
# API - ShamCash
# =========================================

def check_shamcash_tx_multi(tx_number: str) -> Tuple[bool, Dict[str, Any]]:
    all_attempts = []

    for account_address in SHAMCASH_ACCOUNTS:
        try:
            params = {
                "resource": "shamcash",
                "action": "logs",
                "account_address": account_address
            }

            response = http.get(BASE_URL, params=params, timeout=25)
            response.raise_for_status()
            data = response.json()

            all_attempts.append({"account_address": account_address, "response": data})

            if not data.get("success"):
                continue

            payload = data.get("data", {})
            items = payload.get("items", [])

            for item in items:
                tran_id = str(item.get("tran_id", "")).strip()

                if tran_id == tx_number:
                    return True, {
                        "matched_account": str(item.get("account", account_address)).strip(),
                        "transaction": {
                            "transaction_no": tran_id,
                            "amount": str(item.get("amount", "غير معروف")).strip(),
                            "date": str(item.get("datetime", "غير معروف")).strip(),
                            "from": str(item.get("from_name", "غير معروف")).strip(),
                            "to": str(item.get("to_name", "غير معروف")).strip(),
                            "currency": str(item.get("currency", "SYP")).strip(),
                            "note": str(item.get("note", "")).strip(),
                        },
                        "status_text": "ناجحة",
                        "provider": "shamcash",
                        "all_attempts": all_attempts
                    }

        except Exception as e:
            all_attempts.append({"account_address": account_address, "error": str(e)})
            continue

    return False, {
        "status_text": "غير موجودة أو غير ناجحة",
        "provider": "shamcash",
        "all_attempts": all_attempts
    }

def check_shamcash_balance(account_address: str) -> Tuple[bool, Dict[str, Any]]:
    try:
        params = {
            "resource": "shamcash",
            "action": "balance",
            "account_address": account_address
        }

        response = http.get(BASE_URL, params=params, timeout=25)
        response.raise_for_status()
        data = response.json()

        if not data.get("success"):
            return False, data

        payload = data.get("data", {})
        balances = payload.get("balances", payload.get("items", payload))

        return True, {
            "account_address": account_address,
            "balances": balances,
            "response": data
        }

    except Exception as e:
        return False, {"error": str(e)}

# =========================================
# إشعارات الأدمن
# =========================================

async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str):
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=text)
    except Exception as e:
        logger.error("Failed to notify admin: %s", e)

# =========================================
# حمايات مشتركة
# =========================================

async def guard_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    username = user.username if user.username else "بدون_يوزر"
    first_name = user.first_name if user.first_name else ""

    upsert_user(user.id, username, first_name)

    if not is_user_allowed(user.id):
        target = update.message if update.message else update.callback_query.message
        await target.reply_text("هذا البوت غير متاح لك حاليًا.")
        return False

    if maintenance_mode() and not is_admin(user.id):
        target = update.message if update.message else update.callback_query.message
        await target.reply_text("البوت تحت الصيانة حاليًا، حاول لاحقًا.")
        return False

    spam, wait_for = is_spamming(user.id)
    if spam and not is_admin(user.id):
        target = update.message if update.message else update.callback_query.message
        await target.reply_text(f"⏳ انتظر {wait_for} ثانية قبل المحاولة التالية.")
        return False

    return True

# =========================================
# أوامر عامة
# =========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_request(update, context):
        return

    context.user_data["state"] = STATE_NONE
    await update.message.reply_text(
        "أهلاً وسهلاً فيكم اخواتي\nاختارو شو بدكن البوت يعمل\nتذكرو دائماً أنكن شركاء النجاح بكلشي حلو ❤️\nM B T ❤️",
        reply_markup=home_keyboard(update.effective_user.id)
    )

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_request(update, context):
        return

    context.user_data["state"] = STATE_NONE
    await update.message.reply_text(
        "تمت إعادة الضبط.",
        reply_markup=home_keyboard(update.effective_user.id)
    )

# =========================================
# أزرار عامة
# =========================================

async def home_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await guard_request(update, context):
        return

    context.user_data["state"] = STATE_NONE
    await query.message.reply_text(
        "🏠 الصفحة الرئيسية",
        reply_markup=home_keyboard(query.from_user.id)
    )

async def new_check_syriatel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await guard_request(update, context):
        return

    context.user_data["state"] = STATE_NONE
    context.user_data["tx_provider"] = "syriatel"
    await query.message.reply_text("أرسل رقم عملية سيريتل الآن:")

async def new_check_shamcash_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await guard_request(update, context):
        return

    context.user_data["state"] = STATE_NONE
    context.user_data["tx_provider"] = "shamcash"
    await query.message.reply_text("أرسل رقم عملية شام كاش الآن:")

async def check_balance_syriatel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await guard_request(update, context):
        return

    context.user_data["state"] = STATE_WAIT_BALANCE_CODE
    await query.message.reply_text("أرسل الكود الخاص برقم سيريتل الآن:")

async def check_balance_shamcash_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await guard_request(update, context):
        return

    context.user_data["state"] = STATE_WAIT_SHAMCASH_ACCOUNT
    await query.message.reply_text("أرسل عنوان حساب شام كاش الآن:")

async def support_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await guard_request(update, context):
        return

    await query.message.reply_text(
        f"☎️ للدعم تواصل مع: @{SUPPORT_USERNAME}",
        reply_markup=action_keyboard(query.from_user.id)
    )

async def my_last_ops_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await guard_request(update, context):
        return

    rows = get_user_last_transactions(query.from_user.id, limit=5)
    if not rows:
        await query.message.reply_text("لا توجد عمليات سابقة لك.", reply_markup=action_keyboard(query.from_user.id))
        return

    text = "📂 آخر عملياتك:\n\n"
    for row in rows:
        provider_label = "سيريتل كاش" if row["provider"] == "syriatel" else "شام كاش"
        currency = row["currency"] or "ل.س"
        text += (
            f"🏷 النوع: {provider_label}\n"
            f"🧾 رقم العملية: {row['tx_number']}\n"
            f"💰 المبلغ: {row['amount'] or '-'} {currency}\n"
            f"📌 الحالة: {row['tx_status_text'] or '-'}\n"
            f"📅 التاريخ: {row['tx_date'] or row['created_at']}\n"
            "--------------------\n"
        )

    await query.message.reply_text(text, reply_markup=action_keyboard(query.from_user.id))

# =========================================
# لوحة الأدمن
# =========================================

async def admin_panel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    await query.message.reply_text("🛠 لوحة الأدمن", reply_markup=admin_panel_keyboard())

async def admin_last_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    rows = get_last_transactions(limit=10)
    if not rows:
        await query.message.reply_text("لا توجد عمليات بعد.", reply_markup=admin_panel_keyboard())
        return

    text = "📄 آخر 10 عمليات:\n\n"
    for row in rows:
        provider_label = "سيريتل كاش" if row["provider"] == "syriatel" else "شام كاش"
        text += (
            f"النوع: {provider_label}\n"
            f"رقم العملية: {row['tx_number']}\n"
            f"الرقم المطابق: {row['matched_gsm'] or '-'}\n"
            f"كود الرقم: {row['matched_cash_code'] or '-'}\n"
            f"الحساب المطابق: {row['matched_account'] or '-'}\n"
            f"المبلغ: {row['amount'] or '-'} {row['currency'] or ''}\n"
            f"حالة العملية: {row['tx_status_text'] or '-'}\n"
            f"المستخدم: @{row['telegram_username']}\n"
            f"الحالة النهائية: {row['status']}\n"
            f"الوقت: {row['created_at']}\n"
            "--------------------\n"
        )
    await query.message.reply_text(text, reply_markup=admin_panel_keyboard())

async def admin_stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    s = stats_summary()
    await query.message.reply_text(
        "📊 إحصائيات البوت\n\n"
        f"👥 المستخدمون: {s['users']}\n"
        f"🧾 كل العمليات: {s['transactions']}\n"
        f"✅ الناجحة: {s['approved']}\n"
        f"❌ المرفوضة: {s['fake']}\n"
        f"🔁 التكرار: {s['duplicates']}\n"
        f"💰 طلبات الرصيد: {s['balance_requests']}\n"
        f"⚠️ الأخطاء: {s['errors']}",
        reply_markup=admin_panel_keyboard()
    )

async def admin_today_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    s = today_summary()
    await query.message.reply_text(
        f"📅 ملخص اليوم {s['today']}\n\n"
        f"🧾 العمليات: {s['transactions']}\n"
        f"✅ الناجحة: {s['approved']}\n"
        f"❌ المرفوضة: {s['fake']}\n"
        f"💰 طلبات الرصيد: {s['balance_requests']}\n"
        f"🔁 التكرار: {s['duplicates']}",
        reply_markup=admin_panel_keyboard()
    )

async def admin_duplicates_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    rows = get_last_duplicate_attempts(limit=10)
    if not rows:
        await query.message.reply_text("لا توجد محاولات تكرار.", reply_markup=admin_panel_keyboard())
        return

    text = "🔁 آخر محاولات التكرار:\n\n"
    for row in rows:
        provider_label = "سيريتل كاش" if row["provider"] == "syriatel" else "شام كاش"
        text += (
            f"النوع: {provider_label}\n"
            f"رقم العملية: {row['tx_number']}\n"
            f"المستخدم: @{row['telegram_username']}\n"
            f"الوقت: {row['created_at']}\n"
            "--------------------\n"
        )
    await query.message.reply_text(text, reply_markup=admin_panel_keyboard())

async def admin_search_prompt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    context.user_data["state"] = STATE_WAIT_ADMIN_SEARCH
    await query.message.reply_text("أرسل رقم العملية أو الرقم أو كود الكاش أو حساب شام كاش للبحث:")

async def admin_errors_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    rows = get_last_errors(limit=10)
    if not rows:
        await query.message.reply_text("لا توجد أخطاء مسجلة.", reply_markup=admin_panel_keyboard())
        return

    text = "⚠️ آخر الأخطاء:\n\n"
    for row in rows:
        text += (
            f"النطاق: {row['scope']}\n"
            f"المستخدم: @{row['telegram_username']}\n"
            f"التفاصيل: {row['details'][:250]}\n"
            f"الوقت: {row['created_at']}\n"
            "--------------------\n"
        )
    await query.message.reply_text(text, reply_markup=admin_panel_keyboard())

async def admin_export_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    csv_file = export_transactions_csv()
    await context.bot.send_document(
        chat_id=query.message.chat_id,
        document=csv_file,
        filename=f"transactions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        caption="ملف تصدير العمليات"
    )

async def admin_maint_on_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    set_setting("maintenance_mode", "on")
    await query.message.reply_text("تم تشغيل وضع الصيانة.", reply_markup=admin_panel_keyboard())

async def admin_maint_off_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    set_setting("maintenance_mode", "off")
    await query.message.reply_text("تم إيقاف وضع الصيانة.", reply_markup=admin_panel_keyboard())

# =========================================
# المعالجة الرئيسية للرسائل
# =========================================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_request(update, context):
        return

    user = update.effective_user
    username = user.username if user.username else "بدون_يوزر"
    text = normalize_digits(update.message.text or "")
    state = context.user_data.get("state", STATE_NONE)

    # بحث الأدمن
    if state == STATE_WAIT_ADMIN_SEARCH and is_admin(user.id):
        rows = search_transactions(text, limit=10)
        context.user_data["state"] = STATE_NONE

        if not rows:
            await update.message.reply_text("لا توجد نتائج.", reply_markup=admin_panel_keyboard())
            return

        msg = "🔎 نتائج البحث:\n\n"
        for row in rows:
            provider_label = "سيريتل كاش" if row["provider"] == "syriatel" else "شام كاش"
            msg += (
                f"النوع: {provider_label}\n"
                f"رقم العملية: {row['tx_number']}\n"
                f"الرقم المطابق: {row['matched_gsm'] or '-'}\n"
                f"كود الرقم: {row['matched_cash_code'] or '-'}\n"
                f"الحساب المطابق: {row['matched_account'] or '-'}\n"
                f"المبلغ: {row['amount'] or '-'} {row['currency'] or ''}\n"
                f"حالة العملية: {row['tx_status_text'] or '-'}\n"
                f"التاريخ: {row['tx_date'] or '-'}\n"
                f"من: {row['tx_from_number'] or '-'}\n"
                f"إلى: {row['tx_to_number'] or '-'}\n"
                f"الملاحظة: {row['note'] or '-'}\n"
                f"المستخدم: @{row['telegram_username']}\n"
                f"الحالة النهائية: {row['status']}\n"
                "--------------------\n"
            )

        await update.message.reply_text(msg, reply_markup=admin_panel_keyboard())
        return

    # رصيد سيريتل
    if state == STATE_WAIT_BALANCE_CODE:
        if not text:
            await update.message.reply_text(
                "أرسل الكود الخاص بالرقم بشكل صحيح.",
                reply_markup=action_keyboard(user.id)
            )
            return

        await update.message.reply_text("جارٍ التحقق من الرصيد...")

        ok, balance_data = check_syriatel_balance_by_code(text)
        raw_json = safe_json_dump(balance_data)

        if ok:
            gsm = balance_data.get("gsm", "غير معروف")
            cash_code = balance_data.get("cash_code", "غير معروف")
            balance = balance_data.get("balance", "غير معروف")

            save_balance_request(
                provider="syriatel",
                telegram_user_id=user.id,
                telegram_username=username,
                input_code=text,
                gsm=gsm,
                cash_code=cash_code,
                account_address="",
                balance=balance,
                currency="SYP",
                status="success",
                raw_response=raw_json
            )

            await update.message.reply_text(
                "💳 تم جلب الرصيد بنجاح\n\n"
                f"🔐 كود الكاش: {cash_code}\n"
                f"💰 الرصيد: {balance} ل.س",
                reply_markup=action_keyboard(user.id)
            )

            await notify_admin(
                context,
                "تم طلب رصيد سيريتل\n"
                f"المستخدم: @{username}\n"
                f"الكود المدخل: {text}\n"
                f"الرقم: {gsm}\n"
                f"كود الكاش: {cash_code}\n"
                f"الرصيد: {balance}"
            )
        else:
            save_balance_request(
                provider="syriatel",
                telegram_user_id=user.id,
                telegram_username=username,
                input_code=text,
                gsm="",
                cash_code="",
                account_address="",
                balance="",
                currency="",
                status="failed",
                raw_response=raw_json
            )

            await update.message.reply_text(
                "تعذر جلب الرصيد لهذا الكود ❌",
                reply_markup=action_keyboard(user.id)
            )

        context.user_data["state"] = STATE_NONE
        return

    # رصيد شام كاش
    if state == STATE_WAIT_SHAMCASH_ACCOUNT:
        account_address = update.message.text.strip()

        if not account_address:
            await update.message.reply_text(
                "أرسل عنوان حساب شام كاش بشكل صحيح.",
                reply_markup=action_keyboard(user.id)
            )
            return

        await update.message.reply_text("جارٍ التحقق من رصيد شام كاش...")

        ok, balance_data = check_shamcash_balance(account_address)
        raw_json = safe_json_dump(balance_data)

        if ok:
            balances = balance_data.get("balances", {})
            if isinstance(balances, dict):
                lines = [f"💰 {cur}: {bal}" for cur, bal in balances.items()]
                balances_text = "\n".join(lines) if lines else "لا توجد أرصدة"
            elif isinstance(balances, list):
                lines = []
                for item in balances:
                    if isinstance(item, dict):
                        c = item.get("currency", "CUR")
                        b = item.get("balance", "0")
                        lines.append(f"💰 {c}: {b}")
                balances_text = "\n".join(lines) if lines else "لا توجد أرصدة"
            else:
                balances_text = str(balances)

            save_balance_request(
                provider="shamcash",
                telegram_user_id=user.id,
                telegram_username=username,
                input_code=account_address,
                gsm="",
                cash_code="",
                account_address=account_address,
                balance=balances_text,
                currency="",
                status="success",
                raw_response=raw_json
            )

            await update.message.reply_text(
                "💳 تم جلب رصيد شام كاش بنجاح\n\n"
                f"🪪 الحساب: {account_address}\n"
                f"{balances_text}",
                reply_markup=action_keyboard(user.id)
            )

            await notify_admin(
                context,
                "تم طلب رصيد شام كاش\n"
                f"المستخدم: @{username}\n"
                f"الحساب: {account_address}\n"
                f"الأرصدة:\n{balances_text}"
            )
        else:
            save_balance_request(
                provider="shamcash",
                telegram_user_id=user.id,
                telegram_username=username,
                input_code=account_address,
                gsm="",
                cash_code="",
                account_address=account_address,
                balance="",
                currency="",
                status="failed",
                raw_response=raw_json
            )

            await update.message.reply_text(
                "تعذر جلب رصيد شام كاش لهذا الحساب ❌",
                reply_markup=action_keyboard(user.id)
            )

        context.user_data["state"] = STATE_NONE
        return

    # التحقق من العمليات
    tx_number = text
    provider = context.user_data.get("tx_provider", "syriatel")

    if not tx_input_is_valid(tx_number):
        await update.message.reply_text(
            "أرسل رقم العملية فقط بشكل صحيح، أو استخدم الأزرار بالأسفل.",
            reply_markup=action_keyboard(user.id)
        )
        return

    if is_tx_already_used(provider, tx_number):
        save_duplicate_attempt(provider, tx_number, user.id, username)

        await update.message.reply_text(
            "الإشعار مزور ❌",
            reply_markup=action_keyboard(user.id)
        )

        await notify_admin(
            context,
            "محاولة استخدام رقم عملية مكرر\n"
            f"المستخدم: @{username}\n"
            f"النوع: {'سيريتل كاش' if provider == 'syriatel' else 'شام كاش'}\n"
            f"رقم العملية: {tx_number}"
        )
        return

    await update.message.reply_text("جارٍ التحقق من العملية...")

    ok = False
    raw_data: Dict[str, Any] = {}
    matched_gsm = ""
    matched_account = ""

    try:
        if provider == "syriatel":
            ok, raw_data = check_syriatel_tx_multi(tx_number)
            matched_gsm = raw_data.get("matched_gsm", "")
        elif provider == "shamcash":
            ok, raw_data = check_shamcash_tx_multi(tx_number)
            matched_account = raw_data.get("matched_account", "")
        else:
            raw_data = {"error": "Unknown provider"}
    except requests.HTTPError as e:
        raw_data = {"error": f"HTTPError: {str(e)}"}
        log_error("check_tx_http", user.id, username, raw_data["error"])
    except requests.RequestException as e:
        raw_data = {"error": f"RequestException: {str(e)}"}
        log_error("check_tx_request", user.id, username, raw_data["error"])
    except Exception as e:
        raw_data = {"error": f"UnexpectedError: {str(e)}"}
        log_error("check_tx_unexpected", user.id, username, raw_data["error"])

    raw_json = safe_json_dump(raw_data)

    if ok:
        transaction = raw_data.get("transaction", {})
        amount = transaction.get("amount", "غير معروف")
        tx_date = transaction.get("date", "غير معروف")
        tx_from = transaction.get("from", "غير معروف")
        tx_to = transaction.get("to", "غير معروف")
        status_text = raw_data.get("status_text", "ناجحة")

        if provider == "syriatel":
            cash_code = get_cash_code_from_number(matched_gsm)

            save_transaction(
                provider="syriatel",
                tx_number=tx_number,
                matched_gsm=matched_gsm,
                matched_cash_code=cash_code,
                matched_account="",
                amount=amount,
                currency="ل.س",
                tx_status_text=status_text,
                tx_date=tx_date,
                tx_from_number=tx_from,
                tx_to_number=tx_to,
                note="",
                telegram_user_id=user.id,
                telegram_username=username,
                status="approved",
                raw_response=raw_json
            )

            await update.message.reply_text(
                "✅ تم الاستقبال بنجاح\n\n"
                f"🏷 النوع: سيريتل كاش\n"
                f"🧾 رقم العملية: {tx_number}\n"
                f"💰 المبلغ: {amount} ل.س\n"
                f"📌 حالة العملية: {status_text}\n"
                f"📅 التاريخ: {tx_date}\n"
                f"📤 من: {tx_from}\n"
                f"🔐 كود الكاش: {cash_code}",
                reply_markup=action_keyboard(user.id)
            )

            await notify_admin(
                context,
                "تم قبول عملية سيريتل\n"
                f"المستخدم: @{username}\n"
                f"رقم العملية: {tx_number}\n"
                f"المبلغ: {amount} ل.س\n"
                f"حالة العملية: {status_text}\n"
                f"التاريخ: {tx_date}\n"
                f"من: {tx_from}\n"
                f"إلى: {tx_to}\n"
                f"الرقم المطابق: {matched_gsm}\n"
                f"كود الرقم: {cash_code}"
            )

        elif provider == "shamcash":
            currency = transaction.get("currency", "SYP")
            note = transaction.get("note", "")

            save_transaction(
                provider="shamcash",
                tx_number=tx_number,
                matched_gsm="",
                matched_cash_code="",
                matched_account=matched_account,
                amount=amount,
                currency=currency,
                tx_status_text=status_text,
                tx_date=tx_date,
                tx_from_number=tx_from,
                tx_to_number=tx_to,
                note=note,
                telegram_user_id=user.id,
                telegram_username=username,
                status="approved",
                raw_response=raw_json
            )

            await update.message.reply_text(
                "✅ تم الاستقبال بنجاح\n\n"
                f"🏷 النوع: شام كاش\n"
                f"🧾 رقم العملية: {tx_number}\n"
                f"💰 المبلغ: {amount} {currency}\n"
                f"📌 حالة العملية: {status_text}\n"
                f"📅 التاريخ: {tx_date}\n"
                f"📤 من: {tx_from}\n"
                f"📥 إلى: {tx_to}\n"
                f"🪪 الحساب المطابق: {matched_account}\n"
                f"📝 الملاحظة: {note or '-'}",
                reply_markup=action_keyboard(user.id)
            )

            await notify_admin(
                context,
                "تم قبول عملية شام كاش\n"
                f"المستخدم: @{username}\n"
                f"رقم العملية: {tx_number}\n"
                f"المبلغ: {amount} {currency}\n"
                f"حالة العملية: {status_text}\n"
                f"التاريخ: {tx_date}\n"
                f"من: {tx_from}\n"
                f"إلى: {tx_to}\n"
                f"الحساب المطابق: {matched_account}\n"
                f"الملاحظة: {note or '-'}"
            )

    else:
        status_text = raw_data.get("status_text", "غير موجودة أو غير ناجحة")
        provider_label = "سيريتل كاش" if provider == "syriatel" else "شام كاش"

        save_transaction(
            provider=provider,
            tx_number=tx_number,
            matched_gsm="",
            matched_cash_code="",
            matched_account="",
            amount="",
            currency="",
            tx_status_text=status_text,
            tx_date="",
            tx_from_number="",
            tx_to_number="",
            note="",
            telegram_user_id=user.id,
            telegram_username=username,
            status="fake",
            raw_response=raw_json
        )

        await update.message.reply_text(
            "الإشعار مزور ❌\n"
            f"🏷 النوع: {provider_label}\n"
            f"حالة العملية: {status_text}",
            reply_markup=action_keyboard(user.id)
        )

        await notify_admin(
            context,
            "تم رفض عملية\n"
            f"المستخدم: @{username}\n"
            f"النوع: {provider_label}\n"
            f"رقم العملية: {tx_number}\n"
            f"حالة العملية: {status_text}"
        )

    context.user_data["state"] = STATE_NONE

# =========================================
# health server لـ Render
# =========================================

def run_health_server():
    port = int(os.getenv("PORT", "10000"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/health", "/ping"):
                self.send_response(200)
                self.send_header("Content-type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Bot is running")
            else:
                self.send_response(404)
                self.send_header("Content-type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Not found")

        def log_message(self, format, *args):
            return

    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        logger.info("Health server started on port %s", port)
        server.serve_forever()
    except Exception as e:
        logger.exception("Health server failed: %s", e)
        raise

# =========================================
# التشغيل
# =========================================

def main():
    print("STEP 1: main started", flush=True)

    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is missing")
    print("STEP 2: BOT_TOKEN OK", flush=True)

    if not API_SYRIA_KEY:
        raise ValueError("API_SYRIA_KEY is missing")
    print("STEP 3: API_SYRIA_KEY OK", flush=True)

    if ADMIN_ID == 0:
        raise ValueError("ADMIN_ID is missing or invalid")
    print("STEP 4: ADMIN_ID OK", flush=True)

    print("STEP 5: SYRIATEL_GSMS =", SYRIATEL_GSMS, flush=True)
    print("STEP 6: SHAMCASH_ACCOUNTS =", SHAMCASH_ACCOUNTS, flush=True)

    init_db()
    print("STEP 7: DB initialized", flush=True)

    threading.Thread(target=run_health_server, daemon=True).start()
    print("STEP 8: health server started", flush=True)

    app = Application.builder().token(BOT_TOKEN).build()
    print("STEP 9: telegram app built", flush=True)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))

    app.add_handler(CallbackQueryHandler(home_handler, pattern=r"^home$"))
    app.add_handler(CallbackQueryHandler(new_check_syriatel_handler, pattern=r"^new_check_syriatel$"))
    app.add_handler(CallbackQueryHandler(new_check_shamcash_handler, pattern=r"^new_check_shamcash$"))
    app.add_handler(CallbackQueryHandler(check_balance_syriatel_handler, pattern=r"^check_balance_syriatel$"))
    app.add_handler(CallbackQueryHandler(check_balance_shamcash_handler, pattern=r"^check_balance_shamcash$"))
    app.add_handler(CallbackQueryHandler(my_last_ops_handler, pattern=r"^my_last_ops$"))
    app.add_handler(CallbackQueryHandler(support_handler, pattern=r"^support$"))

    app.add_handler(CallbackQueryHandler(admin_panel_handler, pattern=r"^admin_panel$"))
    app.add_handler(CallbackQueryHandler(admin_last_handler, pattern=r"^admin_last$"))
    app.add_handler(CallbackQueryHandler(admin_stats_handler, pattern=r"^admin_stats$"))
    app.add_handler(CallbackQueryHandler(admin_today_handler, pattern=r"^admin_today$"))
    app.add_handler(CallbackQueryHandler(admin_duplicates_handler, pattern=r"^admin_duplicates$"))
    app.add_handler(CallbackQueryHandler(admin_search_prompt_handler, pattern=r"^admin_search$"))
    app.add_handler(CallbackQueryHandler(admin_errors_handler, pattern=r"^admin_errors$"))
    app.add_handler(CallbackQueryHandler(admin_export_handler, pattern=r"^admin_export$"))
    app.add_handler(CallbackQueryHandler(admin_maint_on_handler, pattern=r"^admin_maint_on$"))
    app.add_handler(CallbackQueryHandler(admin_maint_off_handler, pattern=r"^admin_maint_off$"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    print("STEP 10: handlers added", flush=True)

    logger.info("Bot started on Render Web Service...")
    print("STEP 11: before polling", flush=True)

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
