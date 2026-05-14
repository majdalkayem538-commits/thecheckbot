import csv
import io
import json
import logging
import threading
import asyncio
import sqlite3
import time
import os
from flask import Flask
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

BOT_TOKEN         = os.getenv("BOT_TOKEN", "")
API_SYRIA_KEY     = os.getenv("API_SYRIA_KEY", "")
ADMIN_ID          = int(os.getenv("ADMIN_ID", "0"))
SUPPORT_USERNAME  = os.getenv("SUPPORT_USERNAME", "your_support_username")
USE_ALLOWLIST     = os.getenv("USE_ALLOWLIST", "false").lower() == "true"
ANTI_SPAM_SECONDS = int(os.getenv("ANTI_SPAM_SECONDS", "4"))
DB_NAME           = os.getenv("DB_NAME", "payments_pro.db")
BASE_URL          = os.getenv("BASE_URL", "https://apisyria.com/api/v1")

ALLOWED_USER_IDS: set[int] = {
    int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
}

SYRIATEL_GSMS: list[str] = [
    x.strip() for x in os.getenv("SYRIATEL_GSMS", "").split(",") if x.strip()
]

SHAMCASH_ACCOUNTS: list[str] = [
    x.strip() for x in os.getenv("SHAMCASH_ACCOUNTS", "").split(",") if x.strip()
]

# =========================================
# الحالات
# =========================================

STATE_NONE                = "none"
STATE_WAIT_BALANCE_CODE   = "wait_balance_code"
STATE_WAIT_ADMIN_SEARCH   = "wait_admin_search"
STATE_WAIT_SHAMCASH_ACCT  = "wait_shamcash_account"
STATE_WAIT_TX             = "wait_tx"          # انتظار رقم عملية (سيريتل أو شام كاش)

# =========================================
# اللوج
# =========================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# =========================================
# HTTP Session مع Retry
# =========================================

def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://",  adapter)
    session.headers.update({
        "X-Api-Key": API_SYRIA_KEY,
        "Accept":    "application/json",
    })
    return session

http = build_session()

# =========================================
# أدوات مساعدة
# =========================================

user_last_action: Dict[int, float] = {}

def normalize_digits(text: str) -> str:
    return text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")).strip()

def tx_input_is_valid(tx: str) -> bool:
    return tx.isdigit() and 3 <= len(tx) <= 30

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
    return True if not USE_ALLOWLIST else (user_id in ALLOWED_USER_IDS or user_id == ADMIN_ID)

def is_spamming(user_id: int) -> Tuple[bool, int]:
    now = time.time()
    diff = now - user_last_action.get(user_id, 0)
    if diff < ANTI_SPAM_SECONDS:
        return True, max(1, int(ANTI_SPAM_SECONDS - diff))
    user_last_action[user_id] = now
    return False, 0

def split_message(text: str, limit: int = 4000) -> list[str]:
    """تقسيم رسائل طويلة لتجنب حد تيليغرام 4096 حرف."""
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut == -1:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    parts.append(text)
    return parts

# =========================================
# قاعدة البيانات (Thread-Safe)
# =========================================

_db_lock = threading.Lock()

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # أفضل أداء مع multi-thread
    return conn

def init_db():
    with _db_lock:
        conn = db_connect()
        cur = conn.cursor()

        cur.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id      INTEGER PRIMARY KEY,
            username     TEXT,
            first_name   TEXT,
            created_at   TEXT,
            last_seen_at TEXT
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            provider          TEXT,
            tx_number         TEXT NOT NULL,
            matched_gsm       TEXT,
            matched_cash_code TEXT,
            matched_account   TEXT,
            amount            TEXT,
            currency          TEXT,
            tx_status_text    TEXT,
            tx_date           TEXT,
            tx_from_number    TEXT,
            tx_to_number      TEXT,
            note              TEXT,
            telegram_user_id  INTEGER,
            telegram_username TEXT,
            status            TEXT NOT NULL,
            raw_response      TEXT,
            created_at        TEXT,
            UNIQUE(provider, tx_number)
        );

        CREATE TABLE IF NOT EXISTS balance_requests (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            provider         TEXT,
            telegram_user_id INTEGER,
            telegram_username TEXT,
            input_code       TEXT,
            gsm              TEXT,
            cash_code        TEXT,
            account_address  TEXT,
            balance          TEXT,
            currency         TEXT,
            status           TEXT,
            raw_response     TEXT,
            created_at       TEXT
        );

        CREATE TABLE IF NOT EXISTS duplicate_attempts (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            provider          TEXT,
            tx_number         TEXT,
            telegram_user_id  INTEGER,
            telegram_username TEXT,
            created_at        TEXT
        );

        CREATE TABLE IF NOT EXISTS error_logs (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            scope             TEXT,
            telegram_user_id  INTEGER,
            telegram_username TEXT,
            details           TEXT,
            created_at        TEXT
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """)

        conn.commit()
        conn.close()

    if get_setting("maintenance_mode") is None:
        set_setting("maintenance_mode", "off")

# --- Settings ---

def get_setting(key: str) -> Optional[str]:
    with _db_lock:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cur.fetchone()
        conn.close()
        return row["value"] if row else None

def set_setting(key: str, value: str):
    with _db_lock:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO settings (key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )
        conn.commit()
        conn.close()

def maintenance_mode() -> bool:
    return get_setting("maintenance_mode") == "on"

# --- Users ---

def upsert_user(user_id: int, username: str, first_name: str):
    with _db_lock:
        conn = db_connect()
        conn.execute("""
            INSERT INTO users (user_id, username, first_name, created_at, last_seen_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_seen_at=excluded.last_seen_at
        """, (user_id, username, first_name, now_str(), now_str()))
        conn.commit()
        conn.close()

# --- Error Log ---

def log_error(scope: str, telegram_user_id: int, telegram_username: str, details: str):
    with _db_lock:
        conn = db_connect()
        conn.execute(
            "INSERT INTO error_logs (scope,telegram_user_id,telegram_username,details,created_at) VALUES(?,?,?,?,?)",
            (scope, telegram_user_id, telegram_username, details, now_str())
        )
        conn.commit()
        conn.close()

# --- Transactions ---

def is_tx_already_used(provider: str, tx_number: str) -> bool:
    with _db_lock:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM transactions WHERE provider=? AND tx_number=? LIMIT 1",
            (provider, tx_number)
        )
        row = cur.fetchone()
        conn.close()
        return row is not None

def save_duplicate_attempt(provider: str, tx_number: str, user_id: int, username: str):
    with _db_lock:
        conn = db_connect()
        conn.execute(
            "INSERT INTO duplicate_attempts (provider,tx_number,telegram_user_id,telegram_username,created_at) VALUES(?,?,?,?,?)",
            (provider, tx_number, user_id, username, now_str())
        )
        conn.commit()
        conn.close()

def save_transaction(
    provider: str, tx_number: str,
    matched_gsm: str, matched_cash_code: str, matched_account: str,
    amount: str, currency: str, tx_status_text: str,
    tx_date: str, tx_from_number: str, tx_to_number: str, note: str,
    telegram_user_id: int, telegram_username: str,
    status: str, raw_response: str = ""
):
    with _db_lock:
        conn = db_connect()
        conn.execute("""
            INSERT OR IGNORE INTO transactions
            (provider,tx_number,matched_gsm,matched_cash_code,matched_account,
             amount,currency,tx_status_text,tx_date,tx_from_number,tx_to_number,note,
             telegram_user_id,telegram_username,status,raw_response,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            provider, tx_number, matched_gsm, matched_cash_code, matched_account,
            amount, currency, tx_status_text, tx_date, tx_from_number, tx_to_number, note,
            telegram_user_id, telegram_username, status, raw_response, now_str()
        ))
        conn.commit()
        conn.close()

def save_balance_request(
    provider: str, telegram_user_id: int, telegram_username: str,
    input_code: str, gsm: str, cash_code: str, account_address: str,
    balance: str, currency: str, status: str, raw_response: str = ""
):
    with _db_lock:
        conn = db_connect()
        conn.execute("""
            INSERT INTO balance_requests
            (provider,telegram_user_id,telegram_username,input_code,gsm,cash_code,
             account_address,balance,currency,status,raw_response,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            provider, telegram_user_id, telegram_username,
            input_code, gsm, cash_code, account_address,
            balance, currency, status, raw_response, now_str()
        ))
        conn.commit()
        conn.close()

# --- Queries ---

def get_user_last_transactions(user_id: int, limit: int = 5):
    with _db_lock:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("""
            SELECT provider,tx_number,amount,currency,tx_status_text,tx_date,status,created_at
            FROM transactions WHERE telegram_user_id=?
            ORDER BY id DESC LIMIT ?
        """, (user_id, limit))
        rows = cur.fetchall()
        conn.close()
        return rows

def get_last_transactions(limit: int = 10):
    with _db_lock:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("""
            SELECT provider,tx_number,matched_gsm,matched_cash_code,matched_account,
                   amount,currency,tx_status_text,telegram_username,status,created_at
            FROM transactions ORDER BY id DESC LIMIT ?
        """, (limit,))
        rows = cur.fetchall()
        conn.close()
        return rows

def stats_summary() -> dict:
    with _db_lock:
        conn = db_connect()
        cur = conn.cursor()
        def q(sql, *args):
            cur.execute(sql, args)
            return cur.fetchone()[0]
        data = {
            "users":            q("SELECT COUNT(*) FROM users"),
            "transactions":     q("SELECT COUNT(*) FROM transactions"),
            "approved":         q("SELECT COUNT(*) FROM transactions WHERE status='approved'"),
            "fake":             q("SELECT COUNT(*) FROM transactions WHERE status='fake'"),
            "duplicates":       q("SELECT COUNT(*) FROM duplicate_attempts"),
            "balance_requests": q("SELECT COUNT(*) FROM balance_requests"),
            "errors":           q("SELECT COUNT(*) FROM error_logs"),
        }
        conn.close()
        return data

def today_summary() -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    like  = f"{today}%"
    with _db_lock:
        conn = db_connect()
        cur = conn.cursor()
        def q(sql):
            cur.execute(sql, (like,))
            return cur.fetchone()[0]
        data = {
            "today":            today,
            "transactions":     q("SELECT COUNT(*) FROM transactions WHERE created_at LIKE ?"),
            "approved":         q("SELECT COUNT(*) FROM transactions WHERE created_at LIKE ? AND status='approved'".replace("LIKE ?","LIKE ?")),
            "fake":             q("SELECT COUNT(*) FROM transactions WHERE created_at LIKE ? AND status='fake'".replace("LIKE ?","LIKE ?")),
            "balance_requests": q("SELECT COUNT(*) FROM balance_requests WHERE created_at LIKE ?"),
            "duplicates":       q("SELECT COUNT(*) FROM duplicate_attempts WHERE created_at LIKE ?"),
        }
        conn.close()
        # إصلاح approved/fake (يحتاج param إضافي)
        with db_connect() as c2:
            data["approved"] = c2.execute(
                "SELECT COUNT(*) FROM transactions WHERE created_at LIKE ? AND status='approved'", (like,)
            ).fetchone()[0]
            data["fake"] = c2.execute(
                "SELECT COUNT(*) FROM transactions WHERE created_at LIKE ? AND status='fake'", (like,)
            ).fetchone()[0]
        return data

def get_last_duplicate_attempts(limit: int = 10):
    with _db_lock:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("""
            SELECT provider,tx_number,telegram_username,created_at
            FROM duplicate_attempts ORDER BY id DESC LIMIT ?
        """, (limit,))
        rows = cur.fetchall()
        conn.close()
        return rows

def search_transactions(keyword: str, limit: int = 10):
    like = f"%{keyword}%"
    with _db_lock:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("""
            SELECT provider,tx_number,matched_gsm,matched_cash_code,matched_account,
                   amount,currency,tx_status_text,tx_date,tx_from_number,tx_to_number,
                   note,telegram_username,status,created_at
            FROM transactions
            WHERE tx_number LIKE ? OR matched_gsm LIKE ? OR matched_cash_code LIKE ?
               OR matched_account LIKE ? OR tx_from_number LIKE ? OR tx_to_number LIKE ?
               OR telegram_username LIKE ?
            ORDER BY id DESC LIMIT ?
        """, (like, like, like, like, like, like, like, limit))
        rows = cur.fetchall()
        conn.close()
        return rows

def get_last_errors(limit: int = 10):
    with _db_lock:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("""
            SELECT scope,telegram_username,details,created_at
            FROM error_logs ORDER BY id DESC LIMIT ?
        """, (limit,))
        rows = cur.fetchall()
        conn.close()
        return rows

def export_transactions_csv() -> io.BytesIO:
    with _db_lock:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("""
            SELECT provider,tx_number,matched_gsm,matched_cash_code,matched_account,
                   amount,currency,tx_status_text,tx_date,tx_from_number,tx_to_number,
                   note,telegram_user_id,telegram_username,status,created_at
            FROM transactions ORDER BY id DESC
        """)
        rows = cur.fetchall()
        conn.close()

    out = io.StringIO()
    w   = csv.writer(out)
    w.writerow([
        "provider","tx_number","matched_gsm","matched_cash_code","matched_account",
        "amount","currency","tx_status_text","tx_date","tx_from_number","tx_to_number",
        "note","telegram_user_id","telegram_username","status","created_at"
    ])
    for r in rows:
        w.writerow([r[k] for k in r.keys()])

    mem = io.BytesIO()
    mem.write(out.getvalue().encode("utf-8-sig"))
    mem.seek(0)
    return mem

# =========================================
# API - Syriatel
# =========================================

def normalize_gsm_digits(gsm: str) -> str:
    return "".join(ch for ch in str(gsm).strip() if ch.isdigit())

def generate_gsm_variants(gsm: str) -> list[str]:
    """ينتج كل صيغ الرقم الممكنة (09 / 963) بدون تكرار."""
    gsm = normalize_gsm_digits(gsm)
    variants = []

    if gsm.startswith("09") and len(gsm) == 10:
        variants.append(gsm)               # 09XXXXXXXX
        variants.append("963" + gsm[1:])   # 9639XXXXXXXX

    elif gsm.startswith("9639") and len(gsm) == 12:
        variants.append(gsm)               # 9639XXXXXXXX
        variants.append("0" + gsm[3:])     # 09XXXXXXXX

    elif gsm.startswith("963") and len(gsm) == 12:
        variants.append(gsm)
        variants.append("0" + gsm[3:])

    else:
        variants.append(gsm)

    # إزالة التكرار مع الحفاظ على الترتيب
    seen, unique = set(), []
    for v in variants:
        if v not in seen:
            seen.add(v)
            unique.append(v)
    return unique

def check_syriatel_tx_multi(tx_number: str) -> Tuple[bool, Dict[str, Any]]:
    all_attempts = []

    for gsm in SYRIATEL_GSMS:
        for gsm_used in generate_gsm_variants(gsm):   # ✅ استخدام الدالة الصحيحة
            try:
                params = {
                    "resource": "syriatel",
                    "action":   "find_tx",
                    "tx":       tx_number,
                    "gsm":      gsm_used,
                    "period":   "all"
                }
                resp = http.get(BASE_URL, params=params, timeout=25)
                resp.raise_for_status()
                data = resp.json()

                payload     = data.get("data", {})
                found       = payload.get("found", False)
                transaction = payload.get("transaction", {})

                all_attempts.append({
                    "original_gsm": gsm,
                    "gsm_used":     gsm_used,
                    "success":      data.get("success"),
                    "found":        found,
                    "account_gsm":  payload.get("gsm"),
                    "tx_to":        transaction.get("to"),
                })

                if data.get("success") and transaction:
                    # نجلب cash_code من نفس الـ payload فوراً إن وُجد
                    cash_code_direct = str(payload.get("cash_code", "")).strip()
                    return True, {
                        "matched_gsm":      gsm,
                        "gsm_used":         gsm_used,
                        "cash_code_direct": cash_code_direct,
                        "transaction": {
                            "transaction_no": str(transaction.get("transaction_no", tx_number)).strip(),
                            "amount":         str(transaction.get("amount", "غير معروف")).strip(),
                            "date":           str(transaction.get("date",   "غير معروف")).strip(),
                            "from":           str(transaction.get("from",   "غير معروف")).strip(),
                            "to":             str(transaction.get("to",     "غير معروف")).strip(),
                        },
                        "status_text": "ناجحة",
                        "provider":    "syriatel",
                        "all_attempts": all_attempts,
                    }

            except Exception as e:
                all_attempts.append({"gsm": gsm, "gsm_used": gsm_used, "error": str(e)})
                continue

    return False, {
        "status_text":  "غير موجودة أو غير ناجحة",
        "provider":     "syriatel",
        "all_attempts": all_attempts,
    }

def check_syriatel_balance_by_code(code: str) -> Tuple[bool, Dict[str, Any]]:
    try:
        params = {"resource": "syriatel", "action": "balance", "gsm": code}
        resp   = http.get(BASE_URL, params=params, timeout=25)
        resp.raise_for_status()
        data   = resp.json()

        if not data.get("success"):
            return False, data

        payload   = data.get("data", {})
        gsm       = str(payload.get("gsm",       "")).strip()
        cash_code = str(payload.get("cash_code", "")).strip()
        balance   = str(payload.get("balance",   "")).strip()

        if not gsm and not cash_code:
            return False, data

        return True, {"gsm": gsm, "cash_code": cash_code, "balance": balance}

    except Exception as e:
        return False, {"error": str(e)}

def get_cash_code_from_number(gsm: str) -> str:
    """يُستخدم فقط كـ fallback لو cash_code مش موجود بنتيجة التحقق."""
    try:
        params = {"resource": "syriatel", "action": "balance", "gsm": gsm}
        resp   = http.get(BASE_URL, params=params, timeout=25)
        resp.raise_for_status()
        data   = resp.json()

        if not data.get("success"):
            return "غير متوفر"

        cash_code = data.get("data", {}).get("cash_code", "")
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
                "resource":       "shamcash",
                "action":         "logs",
                "account_address": account_address
            }
            resp = http.get(BASE_URL, params=params, timeout=25)
            resp.raise_for_status()
            data = resp.json()

            all_attempts.append({"account_address": account_address, "success": data.get("success")})

            if not data.get("success"):
                continue

            for item in data.get("data", {}).get("items", []):
                if str(item.get("tran_id", "")).strip() == tx_number:
                    return True, {
                        "matched_account": str(item.get("account", account_address)).strip(),
                        "transaction": {
                            "transaction_no": str(item.get("tran_id",   "")).strip(),
                            "amount":         str(item.get("amount",    "غير معروف")).strip(),
                            "date":           str(item.get("datetime",  "غير معروف")).strip(),
                            "from":           str(item.get("from_name", "غير معروف")).strip(),
                            "to":             str(item.get("to_name",   "غير معروف")).strip(),
                            "currency":       str(item.get("currency",  "SYP")).strip(),
                            "note":           str(item.get("note",      "")).strip(),
                        },
                        "status_text":  "ناجحة",
                        "provider":     "shamcash",
                        "all_attempts": all_attempts,
                    }

        except Exception as e:
            all_attempts.append({"account_address": account_address, "error": str(e)})
            continue

    return False, {
        "status_text":  "غير موجودة أو غير ناجحة",
        "provider":     "shamcash",
        "all_attempts": all_attempts,
    }

def check_shamcash_balance(account_address: str) -> Tuple[bool, Dict[str, Any]]:
    try:
        params = {"resource": "shamcash", "action": "balance", "account_address": account_address}
        resp   = http.get(BASE_URL, params=params, timeout=25)
        resp.raise_for_status()
        data   = resp.json()

        if not data.get("success"):
            return False, data

        payload  = data.get("data", {})
        balances = payload.get("balances", payload.get("items", payload))
        return True, {"account_address": account_address, "balances": balances}

    except Exception as e:
        return False, {"error": str(e)}

# =========================================
# إشعارات الأدمن
# =========================================

async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str):
    for part in split_message(text):
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=part)
        except Exception as e:
            logger.error("notify_admin error: %s", e)

# =========================================
# واجهات الأزرار
# =========================================

def home_keyboard(user_id: int) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("✅ تحقق سيريتل كاش",  callback_data="new_check_syriatel")],
        [InlineKeyboardButton("✅ تحقق شام كاش",     callback_data="new_check_shamcash")],
        [InlineKeyboardButton("💰 رصيد سيريتل",      callback_data="check_balance_syriatel")],
        [InlineKeyboardButton("💰 رصيد شام كاش",     callback_data="check_balance_shamcash")],
        [InlineKeyboardButton("📂 آخر عملياتي",      callback_data="my_last_ops")],
        [InlineKeyboardButton("☎️ الدعم",            callback_data="support")],
    ]
    if is_admin(user_id):
        kb.append([InlineKeyboardButton("🛠 لوحة الأدمن", callback_data="admin_panel")])
    return InlineKeyboardMarkup(kb)

def action_keyboard(user_id: int) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("✅ سيريتل كاش جديدة",  callback_data="new_check_syriatel"),
         InlineKeyboardButton("✅ شام كاش جديدة",      callback_data="new_check_shamcash")],
        [InlineKeyboardButton("💰 رصيد سيريتل",       callback_data="check_balance_syriatel"),
         InlineKeyboardButton("💰 رصيد شام كاش",      callback_data="check_balance_shamcash")],
        [InlineKeyboardButton("📂 آخر عملياتي",       callback_data="my_last_ops"),
         InlineKeyboardButton("🏠 الرئيسية",          callback_data="home")],
        [InlineKeyboardButton("☎️ الدعم",             callback_data="support")],
    ]
    if is_admin(user_id):
        kb.append([InlineKeyboardButton("🛠 لوحة الأدمن", callback_data="admin_panel")])
    return InlineKeyboardMarkup(kb)

def admin_panel_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("📄 آخر العمليات",       callback_data="admin_last"),
         InlineKeyboardButton("📊 الإحصائيات",         callback_data="admin_stats")],
        [InlineKeyboardButton("📅 عمليات اليوم",       callback_data="admin_today"),
         InlineKeyboardButton("🔁 محاولات التكرار",    callback_data="admin_duplicates")],
        [InlineKeyboardButton("🔎 بحث عن عملية",       callback_data="admin_search"),
         InlineKeyboardButton("⚠️ آخر الأخطاء",       callback_data="admin_errors")],
        [InlineKeyboardButton("📤 تصدير CSV",          callback_data="admin_export")],
        [InlineKeyboardButton("🟢 تشغيل الصيانة",     callback_data="admin_maint_on"),
         InlineKeyboardButton("🔴 إيقاف الصيانة",     callback_data="admin_maint_off")],
        [InlineKeyboardButton("🏠 الرئيسية",           callback_data="home")],
    ]
    return InlineKeyboardMarkup(kb)

def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="cancel")]])

# =========================================
# الحراسة المشتركة (Guard)
# =========================================

async def guard_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user     = update.effective_user
    username = user.username or "بدون_يوزر"
    upsert_user(user.id, username, user.first_name or "")

    target = update.message or (update.callback_query and update.callback_query.message)

    if not is_user_allowed(user.id):
        if target:
            await target.reply_text("هذا البوت غير متاح لك حاليًا.")
        return False

    if maintenance_mode() and not is_admin(user.id):
        if target:
            await target.reply_text("🔧 البوت تحت الصيانة حاليًا، حاول لاحقًا.")
        return False

    spam, wait = is_spamming(user.id)
    if spam and not is_admin(user.id):
        if target:
            await target.reply_text(f"⏳ انتظر {wait} ثانية قبل المحاولة التالية.")
        return False

    return True

# =========================================
# أوامر عامة
# =========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_request(update, context):
        return
    context.user_data.clear()
    await update.message.reply_text(
        "أهلاً وسهلاً فيكم اخواتي 👋\n"
        "اختارو شو بدكن البوت يعمل\n"
        "تذكرو دائماً أنكن شركاء النجاح بكلشي حلو ❤️\n"
        "M B T ❤️",
        reply_markup=home_keyboard(update.effective_user.id)
    )

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_request(update, context):
        return
    context.user_data.clear()
    await update.message.reply_text(
        "✅ تمت إعادة الضبط.",
        reply_markup=home_keyboard(update.effective_user.id)
    )

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر /cancel أو زر الإلغاء."""
    if not await guard_request(update, context):
        return
    context.user_data.clear()
    if update.message:
        await update.message.reply_text("❌ تم الإلغاء.", reply_markup=home_keyboard(update.effective_user.id))
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text("❌ تم الإلغاء.", reply_markup=home_keyboard(update.effective_user.id))

# =========================================
# أزرار عامة
# =========================================

async def home_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await guard_request(update, context):
        return
    context.user_data.clear()
    await query.message.reply_text("🏠 الصفحة الرئيسية", reply_markup=home_keyboard(query.from_user.id))

async def new_check_syriatel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await guard_request(update, context):
        return
    context.user_data["state"]       = STATE_WAIT_TX
    context.user_data["tx_provider"] = "syriatel"
    await query.message.reply_text(
        "📨 أرسل رقم عملية سيريتل كاش الآن:\n\n_أرسل /cancel للإلغاء_",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard()
    )

async def new_check_shamcash_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await guard_request(update, context):
        return
    context.user_data["state"]       = STATE_WAIT_TX
    context.user_data["tx_provider"] = "shamcash"
    await query.message.reply_text(
        "📨 أرسل رقم عملية شام كاش الآن:\n\n_أرسل /cancel للإلغاء_",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard()
    )

async def check_balance_syriatel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await guard_request(update, context):
        return
    context.user_data["state"] = STATE_WAIT_BALANCE_CODE
    await query.message.reply_text(
        "🔐 أرسل الكود الخاص برقم سيريتل:\n\n_أرسل /cancel للإلغاء_",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard()
    )

async def check_balance_shamcash_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await guard_request(update, context):
        return
    context.user_data["state"] = STATE_WAIT_SHAMCASH_ACCT
    await query.message.reply_text(
        "🪪 أرسل عنوان حساب شام كاش:\n\n_أرسل /cancel للإلغاء_",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard()
    )

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
        await query.message.reply_text("لا توجد عمليات سابقة.", reply_markup=action_keyboard(query.from_user.id))
        return

    text = "📂 آخر عملياتك:\n\n"
    for r in rows:
        label    = "سيريتل كاش" if r["provider"] == "syriatel" else "شام كاش"
        currency = r["currency"] or "ل.س"
        status_icon = "✅" if r["status"] == "approved" else "❌"
        text += (
            f"{status_icon} {label}\n"
            f"🧾 رقم العملية: {r['tx_number']}\n"
            f"💰 المبلغ: {r['amount'] or '-'} {currency}\n"
            f"📅 التاريخ: {r['tx_date'] or r['created_at']}\n"
            "──────────────\n"
        )

    for part in split_message(text):
        await query.message.reply_text(part, reply_markup=action_keyboard(query.from_user.id))

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
        await query.message.reply_text("لا توجد عمليات.", reply_markup=admin_panel_keyboard())
        return

    text = "📄 آخر 10 عمليات:\n\n"
    for r in rows:
        label = "سيريتل كاش" if r["provider"] == "syriatel" else "شام كاش"
        icon  = "✅" if r["status"] == "approved" else "❌"
        text += (
            f"{icon} {label}\n"
            f"رقم العملية: {r['tx_number']}\n"
            f"المبلغ: {r['amount'] or '-'} {r['currency'] or ''}\n"
            f"المستخدم: @{r['telegram_username']}\n"
            f"الوقت: {r['created_at']}\n"
            "──────────────\n"
        )

    for part in split_message(text):
        await query.message.reply_text(part, reply_markup=admin_panel_keyboard())

async def admin_stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    s = stats_summary()
    await query.message.reply_text(
        "📊 إحصائيات البوت\n\n"
        f"👥 المستخدمون:      {s['users']}\n"
        f"🧾 كل العمليات:     {s['transactions']}\n"
        f"✅ الناجحة:          {s['approved']}\n"
        f"❌ المرفوضة:         {s['fake']}\n"
        f"🔁 التكرار:          {s['duplicates']}\n"
        f"💰 طلبات الرصيد:    {s['balance_requests']}\n"
        f"⚠️ الأخطاء:          {s['errors']}",
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
        f"🧾 العمليات:        {s['transactions']}\n"
        f"✅ الناجحة:          {s['approved']}\n"
        f"❌ المرفوضة:         {s['fake']}\n"
        f"💰 طلبات الرصيد:    {s['balance_requests']}\n"
        f"🔁 التكرار:          {s['duplicates']}",
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
    for r in rows:
        label = "سيريتل كاش" if r["provider"] == "syriatel" else "شام كاش"
        text += (
            f"النوع: {label}\n"
            f"رقم العملية: {r['tx_number']}\n"
            f"المستخدم: @{r['telegram_username']}\n"
            f"الوقت: {r['created_at']}\n"
            "──────────────\n"
        )

    for part in split_message(text):
        await query.message.reply_text(part, reply_markup=admin_panel_keyboard())

async def admin_search_prompt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    context.user_data["state"] = STATE_WAIT_ADMIN_SEARCH
    await query.message.reply_text(
        "🔎 أرسل رقم العملية أو الرقم أو كود الكاش أو اسم المستخدم للبحث:\n\n_أرسل /cancel للإلغاء_",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard()
    )

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
    for r in rows:
        text += (
            f"النطاق: {r['scope']}\n"
            f"المستخدم: @{r['telegram_username']}\n"
            f"التفاصيل: {r['details'][:200]}\n"
            f"الوقت: {r['created_at']}\n"
            "──────────────\n"
        )

    for part in split_message(text):
        await query.message.reply_text(part, reply_markup=admin_panel_keyboard())

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
        caption="📤 ملف تصدير العمليات"
    )

async def admin_maint_on_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    set_setting("maintenance_mode", "on")
    await query.message.reply_text("🟢 تم تشغيل وضع الصيانة.", reply_markup=admin_panel_keyboard())

async def admin_maint_off_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    set_setting("maintenance_mode", "off")
    await query.message.reply_text("🔴 تم إيقاف وضع الصيانة.", reply_markup=admin_panel_keyboard())

# =========================================
# معالجة النصوص الرئيسية
# =========================================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_request(update, context):
        return

    user     = update.effective_user
    username = user.username or "بدون_يوزر"
    text     = normalize_digits(update.message.text or "").strip()
    state    = context.user_data.get("state", STATE_NONE)

    # ── بحث الأدمن ───────────────────────────────────────────────
    if state == STATE_WAIT_ADMIN_SEARCH and is_admin(user.id):
        context.user_data["state"] = STATE_NONE
        rows = search_transactions(text, limit=10)

        if not rows:
            await update.message.reply_text("لا توجد نتائج.", reply_markup=admin_panel_keyboard())
            return

        msg = "🔎 نتائج البحث:\n\n"
        for r in rows:
            label = "سيريتل كاش" if r["provider"] == "syriatel" else "شام كاش"
            icon  = "✅" if r["status"] == "approved" else "❌"
            msg += (
                f"{icon} {label}\n"
                f"رقم العملية: {r['tx_number']}\n"
                f"الرقم المطابق: {r['matched_gsm'] or '-'}\n"
                f"كود الكاش: {r['matched_cash_code'] or '-'}\n"
                f"الحساب: {r['matched_account'] or '-'}\n"
                f"المبلغ: {r['amount'] or '-'} {r['currency'] or ''}\n"
                f"من: {r['tx_from_number'] or '-'} → إلى: {r['tx_to_number'] or '-'}\n"
                f"المستخدم: @{r['telegram_username']}\n"
                f"الوقت: {r['created_at']}\n"
                "──────────────\n"
            )

        for part in split_message(msg):
            await update.message.reply_text(part, reply_markup=admin_panel_keyboard())
        return

    # ── رصيد سيريتل ─────────────────────────────────────────────
    if state == STATE_WAIT_BALANCE_CODE:
        if not text:
            await update.message.reply_text("⚠️ أرسل الكود بشكل صحيح.", reply_markup=action_keyboard(user.id))
            return

        context.user_data["state"] = STATE_NONE
        await update.message.reply_text("⏳ جارٍ التحقق من الرصيد...")

        ok, data = check_syriatel_balance_by_code(text)
        raw_json = safe_json_dump(data)

        if ok:
            gsm       = data.get("gsm",       "غير معروف")
            cash_code = data.get("cash_code", "غير معروف")
            balance   = data.get("balance",   "غير معروف")

            save_balance_request(
                provider="syriatel", telegram_user_id=user.id, telegram_username=username,
                input_code=text, gsm=gsm, cash_code=cash_code, account_address="",
                balance=balance, currency="SYP", status="success", raw_response=raw_json
            )
            await update.message.reply_text(
                "💳 تم جلب الرصيد بنجاح\n\n"
                f"🔐 كود الكاش: {cash_code}\n"
                f"💰 الرصيد: {balance} ل.س",
                reply_markup=action_keyboard(user.id)
            )
            await notify_admin(context,
                f"💰 طلب رصيد سيريتل\nالمستخدم: @{username}\nكود: {text}\nرقم: {gsm}\nكود الكاش: {cash_code}\nالرصيد: {balance}"
            )
        else:
            save_balance_request(
                provider="syriatel", telegram_user_id=user.id, telegram_username=username,
                input_code=text, gsm="", cash_code="", account_address="",
                balance="", currency="", status="failed", raw_response=raw_json
            )
            await update.message.reply_text("❌ تعذر جلب الرصيد لهذا الكود.", reply_markup=action_keyboard(user.id))
        return

    # ── رصيد شام كاش ────────────────────────────────────────────
    if state == STATE_WAIT_SHAMCASH_ACCT:
        account_address = update.message.text.strip()
        if not account_address:
            await update.message.reply_text("⚠️ أرسل عنوان الحساب بشكل صحيح.", reply_markup=action_keyboard(user.id))
            return

        context.user_data["state"] = STATE_NONE
        await update.message.reply_text("⏳ جارٍ التحقق من رصيد شام كاش...")

        ok, data = check_shamcash_balance(account_address)
        raw_json = safe_json_dump(data)

        if ok:
            balances = data.get("balances", {})
            if isinstance(balances, dict):
                lines = [f"💰 {c}: {b}" for c, b in balances.items()]
            elif isinstance(balances, list):
                lines = [
                    f"💰 {item.get('currency','?')}: {item.get('balance','?')}"
                    for item in balances if isinstance(item, dict)
                ]
            else:
                lines = [str(balances)]
            balances_text = "\n".join(lines) if lines else "لا توجد أرصدة"

            save_balance_request(
                provider="shamcash", telegram_user_id=user.id, telegram_username=username,
                input_code=account_address, gsm="", cash_code="",
                account_address=account_address, balance=balances_text,
                currency="", status="success", raw_response=raw_json
            )
            await update.message.reply_text(
                f"💳 رصيد شام كاش\n🪪 الحساب: {account_address}\n\n{balances_text}",
                reply_markup=action_keyboard(user.id)
            )
            await notify_admin(context,
                f"💰 طلب رصيد شام كاش\nالمستخدم: @{username}\nالحساب: {account_address}\n{balances_text}"
            )
        else:
            save_balance_request(
                provider="shamcash", telegram_user_id=user.id, telegram_username=username,
                input_code=account_address, gsm="", cash_code="",
                account_address=account_address, balance="", currency="",
                status="failed", raw_response=raw_json
            )
            await update.message.reply_text("❌ تعذر جلب رصيد شام كاش لهذا الحساب.", reply_markup=action_keyboard(user.id))
        return

    # ── التحقق من عملية ─────────────────────────────────────────
    if state != STATE_WAIT_TX:
        # المستخدم أرسل نصاً بدون ضغط زر → أعطه القائمة
        await update.message.reply_text(
            "اختار ماذا تريد من القائمة:",
            reply_markup=home_keyboard(user.id)
        )
        return

    tx_number = text
    provider  = context.user_data.get("tx_provider", "syriatel")

    if not tx_input_is_valid(tx_number):
        await update.message.reply_text(
            "⚠️ أرسل رقم العملية بالأرقام فقط (3-30 خانة).",
            reply_markup=action_keyboard(user.id)
        )
        return

    # تحقق من التكرار
    if is_tx_already_used(provider, tx_number):
        save_duplicate_attempt(provider, tx_number, user.id, username)
        provider_label = "سيريتل كاش" if provider == "syriatel" else "شام كاش"
        await update.message.reply_text("الإشعار مزور ❌", reply_markup=action_keyboard(user.id))
        await notify_admin(context,
            f"🔁 محاولة تكرار\nالمستخدم: @{username}\nالنوع: {provider_label}\nرقم العملية: {tx_number}"
        )
        context.user_data["state"] = STATE_NONE
        return

    await update.message.reply_text("⏳ جارٍ التحقق من العملية...")

    ok       = False
    raw_data: Dict[str, Any] = {}
    matched_gsm     = ""
    matched_account = ""

    try:
        if provider == "syriatel":
            ok, raw_data    = check_syriatel_tx_multi(tx_number)
            matched_gsm     = raw_data.get("matched_gsm", "")
        elif provider == "shamcash":
            ok, raw_data    = check_shamcash_tx_multi(tx_number)
            matched_account = raw_data.get("matched_account", "")
        else:
            raw_data = {"error": "unknown provider"}
    except requests.HTTPError as e:
        raw_data = {"error": f"HTTPError: {e}"}
        log_error("check_tx_http", user.id, username, raw_data["error"])
    except requests.RequestException as e:
        raw_data = {"error": f"RequestException: {e}"}
        log_error("check_tx_request", user.id, username, raw_data["error"])
    except Exception as e:
        raw_data = {"error": f"UnexpectedError: {e}"}
        log_error("check_tx_unexpected", user.id, username, raw_data["error"])

    raw_json    = safe_json_dump(raw_data)
    transaction = raw_data.get("transaction", {})
    amount      = transaction.get("amount", "غير معروف")
    tx_date     = transaction.get("date",   "غير معروف")
    tx_from     = transaction.get("from",   "غير معروف")
    tx_to       = transaction.get("to",     "غير معروف")
    status_text = raw_data.get("status_text", "ناجحة" if ok else "غير موجودة أو غير ناجحة")

    context.user_data["state"] = STATE_NONE

    if ok:
        if provider == "syriatel":
            # ✅ نستخدم cash_code من نتيجة التحقق مباشرة، وإلا نطلبه كـ fallback
            cash_code = raw_data.get("cash_code_direct") or get_cash_code_from_number(matched_gsm)
            gsm_used  = raw_data.get("gsm_used", matched_gsm)

            save_transaction(
                provider="syriatel", tx_number=tx_number,
                matched_gsm=matched_gsm, matched_cash_code=cash_code, matched_account="",
                amount=amount, currency="ل.س", tx_status_text=status_text,
                tx_date=tx_date, tx_from_number=tx_from, tx_to_number=tx_to, note="",
                telegram_user_id=user.id, telegram_username=username,
                status="approved", raw_response=raw_json
            )
            await update.message.reply_text(
                "✅ تم الاستقبال بنجاح\n\n"
                f"🏷 النوع: سيريتل كاش\n"
                f"🧾 رقم العملية: {tx_number}\n"
                f"💰 المبلغ: {amount} ل.س\n"
                f"📌 حالة العملية: {status_text}\n"
                f"📅 التاريخ: {tx_date}\n"
                f"📤 من: {tx_from}\n"
                f"📥 إلى: {tx_to}\n"
                f"🔐 كود الكاش: {cash_code}",
                reply_markup=action_keyboard(user.id)
            )
            await notify_admin(context,
                f"✅ تم قبول عملية سيريتل\n"
                f"المستخدم: @{username}\n"
                f"رقم العملية: {tx_number}\n"
                f"المبلغ: {amount} ل.س\n"
                f"التاريخ: {tx_date}\n"
                f"من: {tx_from} → إلى: {tx_to}\n"
                f"الرقم المطابق: {matched_gsm}\n"
                f"الصيغة: {gsm_used}\n"
                f"كود الكاش: {cash_code}"
            )

        elif provider == "shamcash":
            currency = transaction.get("currency", "SYP")
            note     = transaction.get("note", "")

            save_transaction(
                provider="shamcash", tx_number=tx_number,
                matched_gsm="", matched_cash_code="", matched_account=matched_account,
                amount=amount, currency=currency, tx_status_text=status_text,
                tx_date=tx_date, tx_from_number=tx_from, tx_to_number=tx_to, note=note,
                telegram_user_id=user.id, telegram_username=username,
                status="approved", raw_response=raw_json
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
            await notify_admin(context,
                f"✅ تم قبول عملية شام كاش\n"
                f"المستخدم: @{username}\n"
                f"رقم العملية: {tx_number}\n"
                f"المبلغ: {amount} {currency}\n"
                f"التاريخ: {tx_date}\n"
                f"من: {tx_from} → إلى: {tx_to}\n"
                f"الحساب المطابق: {matched_account}\n"
                f"الملاحظة: {note or '-'}"
            )

    else:
        provider_label = "سيريتل كاش" if provider == "syriatel" else "شام كاش"

        if provider == "syriatel":
            attempts    = raw_data.get("all_attempts", [])
            debug_lines = []
            for item in attempts[:10]:
                debug_lines.append(
                    f"orig={item.get('original_gsm','-')} | used={item.get('gsm_used','-')}"
                    f" | success={item.get('success','-')} | found={item.get('found','-')}"
                )
            await notify_admin(context,
                f"🔍 DEBUG رفض سيريتل\nالمستخدم: @{username}\nرقم: {tx_number}\n" +
                "\n".join(debug_lines)
            )

        save_transaction(
            provider=provider, tx_number=tx_number,
            matched_gsm="", matched_cash_code="", matched_account="",
            amount="", currency="", tx_status_text=status_text,
            tx_date="", tx_from_number="", tx_to_number="", note="",
            telegram_user_id=user.id, telegram_username=username,
            status="fake", raw_response=raw_json
        )
        await update.message.reply_text(
            f"الإشعار مزور ❌\n🏷 النوع: {provider_label}\nحالة العملية: {status_text}",
            reply_markup=action_keyboard(user.id)
        )
        await notify_admin(context,
            f"❌ تم رفض عملية\n"
            f"المستخدم: @{username}\n"
            f"النوع: {provider_label}\n"
            f"رقم العملية: {tx_number}\n"
            f"الحالة: {status_text}"
        )

# =========================================
# Flask Health Server
# =========================================

web_app = Flask(__name__)

@web_app.get("/")
def home():
    return "OK", 200

@web_app.get("/health")
def health():
    return "OK", 200

# =========================================
# تشغيل البوت
# =========================================

def run_bot():
    print("STEP 1: bot thread started", flush=True)

    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is missing")
    if not API_SYRIA_KEY:
        raise ValueError("API_SYRIA_KEY is missing")
    if ADMIN_ID == 0:
        raise ValueError("ADMIN_ID is missing or invalid")

    print(f"STEP 2: SYRIATEL_GSMS={SYRIATEL_GSMS}", flush=True)
    print(f"STEP 3: SHAMCASH_ACCOUNTS={SHAMCASH_ACCOUNTS}", flush=True)

    init_db()
    print("STEP 4: DB initialized", flush=True)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def post_init(application):
        await application.bot.delete_webhook(drop_pending_updates=True)

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Handlers
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("reset",  reset))
    app.add_handler(CommandHandler("cancel", cancel_command))

    app.add_handler(CallbackQueryHandler(cancel_command,               pattern=r"^cancel$"))
    app.add_handler(CallbackQueryHandler(home_handler,                 pattern=r"^home$"))
    app.add_handler(CallbackQueryHandler(new_check_syriatel_handler,   pattern=r"^new_check_syriatel$"))
    app.add_handler(CallbackQueryHandler(new_check_shamcash_handler,   pattern=r"^new_check_shamcash$"))
    app.add_handler(CallbackQueryHandler(check_balance_syriatel_handler, pattern=r"^check_balance_syriatel$"))
    app.add_handler(CallbackQueryHandler(check_balance_shamcash_handler, pattern=r"^check_balance_shamcash$"))
    app.add_handler(CallbackQueryHandler(my_last_ops_handler,          pattern=r"^my_last_ops$"))
    app.add_handler(CallbackQueryHandler(support_handler,              pattern=r"^support$"))

    app.add_handler(CallbackQueryHandler(admin_panel_handler,          pattern=r"^admin_panel$"))
    app.add_handler(CallbackQueryHandler(admin_last_handler,           pattern=r"^admin_last$"))
    app.add_handler(CallbackQueryHandler(admin_stats_handler,          pattern=r"^admin_stats$"))
    app.add_handler(CallbackQueryHandler(admin_today_handler,          pattern=r"^admin_today$"))
    app.add_handler(CallbackQueryHandler(admin_duplicates_handler,     pattern=r"^admin_duplicates$"))
    app.add_handler(CallbackQueryHandler(admin_search_prompt_handler,  pattern=r"^admin_search$"))
    app.add_handler(CallbackQueryHandler(admin_errors_handler,         pattern=r"^admin_errors$"))
    app.add_handler(CallbackQueryHandler(admin_export_handler,         pattern=r"^admin_export$"))
    app.add_handler(CallbackQueryHandler(admin_maint_on_handler,       pattern=r"^admin_maint_on$"))
    app.add_handler(CallbackQueryHandler(admin_maint_off_handler,      pattern=r"^admin_maint_off$"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("STEP 5: handlers registered", flush=True)
    logger.info("Bot started...")

    app.run_polling(drop_pending_updates=True, close_loop=False, stop_signals=None)


# =========================================
# نقطة الدخول
# =========================================

def main():
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    port = int(os.getenv("PORT", "10000"))
    print(f"Flask running on port {port}", flush=True)
    web_app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
