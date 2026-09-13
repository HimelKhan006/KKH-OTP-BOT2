#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
⚡ OTP WAVE - 100% Fully Automatic All-in-One Standalone Telegram Bot
====================================================================
Single self-contained Python file. No restarts. Always online.
- Auto-installs dependencies
- SQLite deduplication (7-day retention)
- Forwards all incoming OTPs to Group + Admin private chat
- Crash-proof polling loop (never stops)
"""

import os
import sys
import subprocess

# ==========================================
# 1. Auto Dependency Installer
# ==========================================
def ensure_dependencies():
    required = [
        ("telegram", "python-telegram-bot>=21.0"),
        ("httpx",    "httpx>=0.27.0"),
        ("dotenv",   "python-dotenv>=1.0.0"),
    ]
    for module_name, package_spec in required:
        try:
            __import__(module_name)
        except ImportError:
            print(f"📦 Installing {package_spec}...")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--no-warn-script-location", package_spec],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )

ensure_dependencies()

# ==========================================
# 2. Imports
# ==========================================
import re
import json
import html
import sqlite3
import logging
import asyncio
import time
import argparse
import unicodedata
from typing import Set, Dict, Any, List, Optional, Tuple
from datetime import datetime, timezone

import httpx
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup, CopyTextButton
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, NetworkError, Conflict
from telegram.request import HTTPXRequest
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ==========================================
# 3. Configuration
# ==========================================
def load_environment():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("\"'").strip()
                    if key and key not in os.environ:
                        os.environ[key] = val
        except Exception:
            pass

load_environment()

TELEGRAM_BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
THIRDWAVE_API_KEY     = os.getenv("THIRDWAVE_API_KEY", "").strip()
_raw_base = os.getenv("THIRDWAVE_BASE_URL", "https://clients.thirdwave.im").strip().rstrip("/")
if _raw_base and not _raw_base.startswith(("http://", "https://")):
    _raw_base = f"https://{_raw_base}"
THIRDWAVE_BASE_URL    = _raw_base
TELEGRAM_GROUP_CHAT_ID = os.getenv("TELEGRAM_GROUP_CHAT_ID", "").strip()
SECONDARY_GROUP_CHAT_ID = os.getenv("SECONDARY_GROUP_CHAT_ID", os.getenv("TELEGRAM_SECONDARY_GROUP_CHAT_ID", "")).strip()
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "2.0"))
STARTUP_TYPE          = os.getenv("STARTUP_TYPE", "workflow_dispatch").strip().lower()

_admin_raw    = os.getenv("ADMIN_USER_IDS", "").strip()
ADMIN_USER_IDS: List[int] = [int(u.strip()) for u in _admin_raw.split(",") if u.strip().isdigit()]

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
DB_FILE   = os.getenv("DB_FILE", os.path.join(BASE_DIR, "bot1_database.db"))
DATA_FILE = os.path.join(BASE_DIR, "bot_data.json")

# ==========================================
# 4. Logging
# ==========================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("OTPWAVE")

# ==========================================
# 5. Persistent Storage (bot_data.json)
# ==========================================
_STORED_DATA_CACHE: Dict[str, Any] = {}

def load_stored_data() -> dict:
    global _STORED_DATA_CACHE
    if not _STORED_DATA_CACHE and os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    _STORED_DATA_CACHE = loaded
        except Exception:
            pass
    return dict(_STORED_DATA_CACHE)

def save_stored_data(data: dict):
    global _STORED_DATA_CACHE
    try:
        _STORED_DATA_CACHE = dict(data)
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning(f"Error saving stored data to {DATA_FILE}: {e}")

def get_target_group_chat_ids() -> List[int]:
    """Returns a list of all configured target group chat IDs (Primary + Secondary)."""
    groups: List[int] = []
    seen = set()

    # Check stored json overrides
    data = load_stored_data()
    for key in ["group_chat_id", "primary_group_chat_id", "secondary_group_chat_id"]:
        val = data.get(key)
        if val:
            for part in str(val).split(","):
                part = part.strip()
                if part:
                    try:
                        cid = int(part)
                        if cid not in seen:
                            seen.add(cid)
                            groups.append(cid)
                    except ValueError:
                        pass

    # Check primary env variable (supports comma-separated list as well)
    if TELEGRAM_GROUP_CHAT_ID:
        for part in TELEGRAM_GROUP_CHAT_ID.split(","):
            part = part.strip()
            if part:
                try:
                    cid = int(part)
                    if cid not in seen:
                        seen.add(cid)
                        groups.append(cid)
                except ValueError:
                    pass

    # Check secondary env variable
    if SECONDARY_GROUP_CHAT_ID:
        for part in SECONDARY_GROUP_CHAT_ID.split(","):
            part = part.strip()
            if part:
                try:
                    cid = int(part)
                    if cid not in seen:
                        seen.add(cid)
                        groups.append(cid)
                except ValueError:
                    pass

    return groups

def get_linked_group_chat_id() -> Optional[int]:
    """Returns the primary linked group chat ID."""
    targets = get_target_group_chat_ids()
    return targets[0] if targets else None

def is_user_authorized(user_id: int) -> bool:
    if not ADMIN_USER_IDS:
        return True
    return user_id in ADMIN_USER_IDS

# ==========================================
# 6. GitHub Gist Cloud Storage (28-Hour Retention)
# ==========================================
GIST_ID    = os.getenv("GIST_ID", os.getenv("GITHUB_GIST_ID", "")).strip()
GIST_TOKEN = os.getenv("GIST_TOKEN", os.getenv("GH_TOKEN", os.getenv("GITHUB_TOKEN", ""))).strip()
seen_message_ids: Set[str] = set()
seen_timestamps: Dict[str, float] = {}
_gist_dirty: bool = False
_is_handover: bool = os.getenv("IS_HANDOVER", "false").strip().lower() in ("true", "1", "yes")
_handover_epoch: float = 0.0
bot_process_start_time: float = time.time()

GIST_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

class GistStorage:
    def __init__(self, gist_id: str, token: str, filename: str = "thirdwave_seen_messages.json",
                 description: str = "OTP Wave Bot — 28h persistent storage"):
        self.gist_id = gist_id
        self.token = token
        self.filename = filename
        self.description = description
        self.bot_name = "THIRDWAVE_OTP_WAVE"
        self.enabled = bool(token)
        self.api_url = f"https://api.github.com/gists/{gist_id}" if gist_id else ""

    def _auth_headers(self) -> Dict[str, str]:
        return {**GIST_HEADERS, "Authorization": f"Bearer {self.token}"}

    async def ensure_gist(self) -> bool:
        """Finds existing Gist matching filename, deletes any duplicate Gists, or creates a new one."""
        if not self.token:
            return False
        try:
            async with httpx.AsyncClient(timeout=15.0) as http:
                # 1. Search existing Gists to reuse and delete duplicates
                res = await http.get("https://api.github.com/gists?per_page=100", headers=self._auth_headers())
                if res.is_success:
                    gists = res.json()
                    matching_gists = []
                    for g in gists:
                        files = g.get("files", {})
                        if self.filename in files:
                            matching_gists.append(g)

                    if matching_gists:
                        # Use the first matching Gist
                        primary = matching_gists[0]
                        self.gist_id = primary.get("id", "")
                        self.api_url = f"https://api.github.com/gists/{self.gist_id}"
                        logger.info(f"☁️ Reusing existing GitHub Gist: {self.gist_id}")

                        # Automatically DELETE any duplicate Gists from previous runs
                        for dup in matching_gists[1:]:
                            dup_id = dup.get("id")
                            if dup_id and dup_id != self.gist_id:
                                try:
                                    del_res = await http.delete(f"https://api.github.com/gists/{dup_id}", headers=self._auth_headers())
                                    if del_res.status_code in (204, 200):
                                        logger.info(f"🗑️ Deleted duplicate Gist: {dup_id}")
                                except Exception as e:
                                    logger.warning(f"Could not delete duplicate Gist {dup_id}: {e}")
                        return True

                # 2. If no matching Gist exists, create a new one
                res = await http.post(
                    "https://api.github.com/gists",
                    headers=self._auth_headers(),
                    json={
                        "description": self.description,
                        "public": False,
                        "files": {
                            self.filename: {
                                "content": json.dumps({"seen": {}, "bot": self.bot_name, "count": 0, "handover": False}, indent=2)
                            }
                        }
                    }
                )
                if res.is_success:
                    self.gist_id = res.json().get("id", "")
                    self.api_url = f"https://api.github.com/gists/{self.gist_id}"
                    logger.info(f"☁️ Created new GitHub Gist: {self.gist_id}")
                    return True
                else:
                    logger.warning(f"Gist create failed {res.status_code}: {res.text[:120]}")
        except Exception as e:
            logger.warning(f"Gist auto-management error: {e}")
        return False

    async def load_state(self) -> Dict[str, Any]:
        """Fetch 28h history and continuous handover state from GitHub Gist."""
        if not self.enabled:
            return {}
        if not self.api_url:
            await self.ensure_gist()
        if not self.api_url:
            return {}
        try:
            async with httpx.AsyncClient(timeout=15.0) as http:
                res = await http.get(self.api_url, headers=self._auth_headers())
                if res.is_success:
                    data = res.json()
                    files = data.get("files", {})
                    if self.filename in files:
                        content_str = files[self.filename].get("content", "{}")
                        parsed = json.loads(content_str)
                        if isinstance(parsed, dict):
                            seen_map = parsed.get("seen", {})
                            cutoff = datetime.now(timezone.utc).timestamp() - (28 * 3600)
                            valid_seen = {k: float(v) for k, v in seen_map.items() if float(v) >= cutoff}
                            
                        if "custom_languages" in parsed and isinstance(parsed["custom_languages"], list):
                            with get_db_connection() as conn:
                                init_and_seed_language_database(conn)
                                for c in parsed["custom_languages"]:
                                    conn.execute("""
                                        INSERT INTO custom_languages (lang_id, lang_name, iso_code, display_name, is_custom, is_enabled)
                                        VALUES (?, ?, ?, ?, ?, ?)
                                        ON CONFLICT(lang_id) DO UPDATE SET
                                            display_name = excluded.display_name,
                                            is_enabled = excluded.is_enabled;
                                    """, (c["lang_id"], c["name"], c["iso"], c["display_name"], 1 if c.get("is_custom") else 0, 1 if c.get("is_enabled") else 0))
                                    for phr in c.get("phrases", []):
                                        conn.execute("""
                                            INSERT INTO custom_language_phrases (lang_id, phrase)
                                            SELECT ?, ? WHERE NOT EXISTS (
                                                SELECT 1 FROM custom_language_phrases WHERE lang_id = ? AND phrase = ?
                                            );
                                        """, (c["lang_id"], phr, c["lang_id"], phr))
                                conn.commit()
                            load_languages_from_db()

                            logger.info(f"☁️ Restored {len(valid_seen)} seen messages from GitHub Gist ({self.gist_id[:8]}...).")
                            return {
                                "seen": valid_seen,
                                "handover": bool(parsed.get("handover", False)),
                                "handover_epoch": float(parsed.get("handover_epoch", 0.0)),
                                "total_forwarded": int(parsed.get("total_forwarded", 0)),
                                "country_counts": parsed.get("country_counts", {}),
                                "base_url": parsed.get("base_url", ""),
                                "bot_data": parsed.get("bot_data", {}),
                            }
                else:
                    logger.warning(f"Gist load status {res.status_code}: {res.text[:100]}")
        except Exception as e:
            logger.warning(f"Gist load error: {e}")
        return {}

    async def load_seen(self) -> Dict[str, float]:
        """Backwards compatibility wrapper for load_state."""
        state = await self.load_state()
        return state.get("seen", {})

    async def save_state(self, seen_dict: Dict[str, float], is_handover: bool = False,
                         total_forwarded: int = 0, country_counts: Optional[Dict[str, int]] = None,
                         base_url: str = "", **kwargs) -> bool:
        """Prune older than 28h and sync state & handover markers to GitHub Gist."""
        if not self.enabled:
            return False
        if not self.api_url:
            await self.ensure_gist()
        if not self.api_url:
            return False
        try:
            cutoff = datetime.now(timezone.utc).timestamp() - (28 * 3600)
            cleaned = {k: v for k, v in seen_dict.items() if v >= cutoff}
            
            cust_langs_data = []
            for lid, cinfo in DB_LANGUAGES_CACHE.items():
                cust_langs_data.append({
                    "lang_id": lid,
                    "name": cinfo.get("name", lid.title()),
                    "iso": cinfo.get("iso", "XX"),
                    "display_name": cinfo.get("display_name", lid.title()),
                    "is_enabled": cinfo.get("is_enabled", True),
                    "phrases": list(cinfo.get("phrases", {}).keys()) if isinstance(cinfo.get("phrases"), dict) else list(cinfo.get("phrases", []))
                })

            payload_data = {
                "custom_languages": cust_langs_data,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "bot": self.bot_name,
                "count": len(cleaned),
                "seen": cleaned,
                "handover": is_handover,
                "handover_epoch": datetime.now(timezone.utc).timestamp() if is_handover else 0.0,
                "total_forwarded": total_forwarded,
                "country_counts": country_counts or {},
                "base_url": base_url or "",
                "bot_data": load_stored_data(),
            }
            payload = {
                "description": self.description,
                "files": {
                    self.filename: {
                        "content": json.dumps(payload_data, indent=2)
                    }
                }
            }
            async with httpx.AsyncClient(timeout=15.0) as http:
                res = await http.patch(self.api_url, headers=self._auth_headers(), json=payload)
                if res.is_success:
                    logger.info(f"☁️ Synced {len(cleaned)} messages to GitHub Gist (handover={is_handover}).")
                    return True
                else:
                    logger.warning(f"Gist sync status {res.status_code}: {res.text[:100]}")
        except Exception as e:
            logger.warning(f"Gist sync error: {e}")
        return False

    async def save_seen(self, seen_dict: Dict[str, float]) -> bool:
        """Backwards compatibility wrapper for save_state."""
        global total_forwarded_count, country_forwarded_counts
        base = client.base_url if "client" in globals() else ""
        return await self.save_state(seen_dict, is_handover=False, total_forwarded=total_forwarded_count,
                                     country_counts=country_forwarded_counts, base_url=base)

gist_storage = GistStorage(
    GIST_ID, GIST_TOKEN,
    filename="thirdwave_seen_messages.json",
    description="OTP Wave Bot — 28h persistent storage (auto-managed)"
)

def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def init_db():
    with get_db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_otps (
                id           TEXT PRIMARY KEY,
                source       TEXT,
                country      TEXT,
                number       TEXT,
                otp_code     TEXT,
                raw_message  TEXT,
                rate         TEXT,
                message_time TEXT,
                chat_id      INTEGER,
                forwarded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_forwarded_at ON processed_otps(forwarded_at);")
        init_and_seed_language_database(conn)
        conn.commit()
    load_languages_from_db()
    logger.info("📦 SQLite database initialized at %s", DB_FILE)

def is_message_seen(message_id: str) -> bool:
    if not message_id:
        return False
    if message_id in seen_message_ids:
        return True
    try:
        with get_db_connection() as conn:
            cur = conn.execute("SELECT 1 FROM processed_otps WHERE id = ? LIMIT 1;", (message_id,))
            found = cur.fetchone() is not None
            if found:
                seen_message_ids.add(message_id)
            return found
    except Exception as e:
        logger.error(f"DB check error: {e}")
        return False

def save_processed_message(item: Dict[str, Any], chat_id: int,
                           country: str = "", masked_num: str = "", otp_code: str = "") -> bool:
    global _gist_dirty
    mid = str(item.get("id") or "").strip()
    if not mid:
        return False
    seen_message_ids.add(mid)
    seen_timestamps[mid] = datetime.now(timezone.utc).timestamp()
    _gist_dirty = True
    source      = str(item.get("sourceAddress") or item.get("source") or item.get("caller") or item.get("sender") or "")
    rate        = str(item.get("rate") or "")
    raw_message = str(item.get("messageBody") or item.get("message") or item.get("text") or item.get("body") or item.get("otp") or "")
    message_time = str(item.get("receivedAt") or item.get("messageTime") or item.get("createdAt") or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    try:
        with get_db_connection() as conn:
            cur = conn.execute("""
                INSERT OR IGNORE INTO processed_otps
                    (id, source, country, number, otp_code, raw_message, rate, message_time, chat_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (mid, source, country, masked_num, otp_code, raw_message, rate, message_time, chat_id))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"DB save error: {e}")
    return False

def get_total_processed_count() -> int:
    try:
        with get_db_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM processed_otps;")
            row = cur.fetchone()
            return row[0] if row else 0
    except Exception:
        return 0

def cleanup_old_messages(max_age_days: int = 7):
    try:
        with get_db_connection() as conn:
            conn.execute(
                "DELETE FROM processed_otps WHERE forwarded_at < datetime('now', ?);",
                (f"-{max_age_days} days",)
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"DB cleanup error: {e}")

# ==========================================
# 7. Thirdwave API Client
# ==========================================
class ThirdwaveClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 20.0):
        self.base_url  = base_url.rstrip("/")
        self.api_key   = api_key.strip()
        self.timeout   = timeout
        self._client: Optional[httpx.AsyncClient] = None

    def _get_http_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                limits=httpx.Limits(max_keepalive_connections=10, max_connections=20, keepalive_expiry=60.0),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type":  "application/json",
                    "Accept":        "application/json",
                },
            )
        return self._client

    async def fetch_incoming_messages(self) -> List[Dict[str, Any]]:
        try:
            client = self._get_http_client()
            res = await client.get(f"{self.base_url}/api/v1/traffic", params={"page": 1, "pageSize": 50})
            if res.is_success:
                data = res.json()
                rows = data.get("rows") if isinstance(data, dict) else (data if isinstance(data, list) else [])
                seen = set()
                out  = []
                for r in rows:
                    if isinstance(r, dict) and r.get("id"):
                        mid = str(r["id"])
                        if mid not in seen:
                            seen.add(mid)
                            out.append(r)
                return out
            else:
                logger.warning(f"API returned {res.status_code}: {res.text[:120]}")
        except Exception as e:
            logger.warning(f"Thirdwave API fetch error: {e}")
        return []

    def update_base_url(self, new_url: str) -> str:
        cleaned = new_url.strip().rstrip("/")
        if cleaned and not cleaned.startswith(("http://", "https://")):
            cleaned = f"https://{cleaned}"
        self.base_url = cleaned
        self._client = None
        return self.base_url

# ==========================================
# 8. Country ISO Alpha-2 Lookup
# ==========================================
COUNTRY_ISO_DATA: Dict[str, Tuple[str, str]] = {
    # Asia & Middle East
    "sri lanka": ("🇱🇰", "LK"), "lk": ("🇱🇰", "LK"),
    "indonesia": ("🇮🇩", "ID"), "id": ("🇮🇩", "ID"),
    "india":     ("🇮🇳", "IN"), "in": ("🇮🇳", "IN"),
    "bangladesh":("🇧🇩", "BD"), "bd": ("🇧🇩", "BD"),
    "pakistan":  ("🇵🇰", "PK"), "pk": ("🇵🇰", "PK"),
    "vietnam":   ("🇻🇳", "VN"), "vn": ("🇻🇳", "VN"),
    "philippines":("🇵🇭","PH"), "ph": ("🇵🇭", "PH"),
    "thailand":  ("🇹🇭", "TH"), "th": ("🇹🇭", "TH"),
    "malaysia":  ("🇲🇾", "MY"), "my": ("🇲🇾", "MY"),
    "cambodia":  ("🇰🇭", "KH"), "kh": ("🇰🇭", "KH"),
    "myanmar":   ("🇲🇲", "MM"), "mm": ("🇲🇲", "MM"),
    "nepal":     ("🇳🇵", "NP"), "np": ("🇳🇵", "NP"),
    "china":     ("🇨🇳", "CN"), "cn": ("🇨🇳", "CN"),
    "taiwan":    ("🇹🇼", "TW"), "tw": ("🇹🇼", "TW"),
    "japan":     ("🇯🇵", "JP"), "jp": ("🇯🇵", "JP"),
    "south korea":("🇰🇷","KR"), "kr": ("🇰🇷", "KR"),
    "singapore": ("🇸🇬", "SG"), "sg": ("🇸🇬", "SG"),
    "hong kong": ("🇭🇰", "HK"), "hk": ("🇭🇰", "HK"),
    "russia":    ("🇷🇺", "RU"), "ru": ("🇷🇺", "RU"),
    "ukraine":   ("🇺🇦", "UA"), "ua": ("🇺🇦", "UA"),
    "turkey":    ("🇹🇷", "TR"), "tr": ("🇹🇷", "TR"),
    "saudi arabia":("🇸🇦","SA"),"sa": ("🇸🇦", "SA"),
    "uae":       ("🇦🇪", "AE"), "ae": ("🇦🇪", "AE"),
    "united arab emirates":("🇦🇪","AE"),
    "iran":      ("🇮🇷", "IR"), "ir": ("🇮🇷", "IR"),
    "iraq":      ("🇮🇶", "IQ"), "iq": ("🇮🇶", "IQ"),
    "israel":    ("🇮🇱", "IL"), "il": ("🇮🇱", "IL"),
    "jordan":    ("🇯🇴", "JO"), "jo": ("🇯🇴", "JO"),
    "kuwait":    ("🇰🇼", "KW"), "kw": ("🇰🇼", "KW"),
    "lebanon":   ("🇱🇧", "LB"), "lb": ("🇱🇧", "LB"),
    "oman":      ("🇴🇲", "OM"), "om": ("🇴🇲", "OM"),
    "qatar":     ("🇶🇦", "QA"), "qa": ("🇶🇦", "QA"),
    "syria":     ("🇸🇾", "SY"), "sy": ("🇸🇾", "SY"),
    "yemen":     ("🇾🇪", "YE"), "ye": ("🇾🇪", "YE"),
    "bahrain":   ("🇧🇭", "BH"), "bh": ("🇧🇭", "BH"),
    "uzbekistan":("🇺🇿","UZ"), "uz": ("🇺🇿", "UZ"),
    "kazakhstan":("🇰🇿","KZ"), "kz": ("🇰🇿", "KZ"),
    "kyrgyzstan":("🇰🇬","KG"), "kg": ("🇰🇬", "KG"),
    "tajikistan":("🇹🇯","TJ"), "tj": ("🇹🇯", "TJ"),
    "turkmenistan":("🇹🇲","TM"), "tm": ("🇹🇲", "TM"),
    "afghanistan":("🇦🇫","AF"), "af": ("🇦🇫", "AF"),
    "azerbaijan":("🇦🇿","AZ"), "az": ("🇦🇿", "AZ"),
    "armenia":   ("🇦🇲", "AM"), "am": ("🇦🇲", "AM"),
    "georgia":   ("🇬🇪", "GE"), "ge": ("🇬🇪", "GE"),
    "laos":      ("🇱🇦", "LA"), "la": ("🇱🇦", "LA"),
    "bhutan":    ("🇧🇹", "BT"), "bt": ("🇧🇹", "BT"),
    "maldives":  ("🇲🇻", "MV"), "mv": ("🇲🇻", "MV"),

    # Africa
    "ivory coast":("🇨🇮","CI"), "cote d'ivoire":("🇨🇮","CI"), "ci":("🇨🇮","CI"),
    "egypt":     ("🇪🇬", "EG"), "eg": ("🇪🇬", "EG"),
    "nigeria":   ("🇳🇬", "NG"), "ng": ("🇳🇬", "NG"),
    "kenya":     ("🇰🇪", "KE"), "ke": ("🇰🇪", "KE"),
    "ghana":     ("🇬🇭", "GH"), "gh": ("🇬🇭", "GH"),
    "south africa":("🇿🇦","ZA"),"za": ("🇿🇦", "ZA"),
    "morocco":   ("🇲🇦", "MA"), "ma": ("🇲🇦", "MA"),
    "algeria":   ("🇩🇿", "DZ"), "dz": ("🇩🇿", "DZ"),
    "tunisia":   ("🇹🇳", "TN"), "tn": ("🇹🇳", "TN"),
    "libya":     ("🇱🇾", "LY"), "ly": ("🇱🇾", "LY"),
    "tanzania":  ("🇹🇿", "TZ"), "tz": ("🇹🇿", "TZ"),
    "uganda":    ("🇺🇬", "UG"), "ug": ("🇺🇬", "UG"),
    "ethiopia":  ("🇪🇹", "ET"), "et": ("🇪🇹", "ET"),
    "somalia":   ("🇸🇴", "SO"), "so": ("🇸🇴", "SO"),
    "sudan":     ("🇸🇩", "SD"), "sd": ("🇸🇩", "SD"),
    "senegal":   ("🇸🇳", "SN"), "sn": ("🇸🇳", "SN"),
    "cameroon":  ("🇨🇲", "CM"), "cm": ("🇨🇲", "CM"),
    "rwanda":    ("🇷🇼", "RW"), "rw": ("🇷🇼", "RW"),
    "zambia":    ("🇿🇲", "ZM"), "zm": ("🇿🇲", "ZM"),
    "zimbabwe":  ("🇿🇼", "ZW"), "zw": ("🇿🇼", "ZW"),
    "mozambique":("🇲🇿","MZ"), "mz": ("🇲🇿", "MZ"),
    "angola":    ("🇦🇴", "AO"), "ao": ("🇦🇴", "AO"),
    "mali":      ("🇲🇱", "ML"), "ml": ("🇲🇱", "ML"),
    "burkina faso":("🇧🇫","BF"),"bf": ("🇧🇫", "BF"),
    "guinea":    ("🇬🇳", "GN"), "gn": ("🇬🇳", "GN"),
    "benin":     ("🇧🇯", "BJ"), "bj": ("🇧🇯", "BJ"),
    "togo":      ("🇹🇬", "TG"), "tg": ("🇹🇬", "TG"),
    "madagascar":("🇲🇬","MG"), "mg": ("🇲🇬", "MG"),
    "mauritius": ("🇲🇺", "MU"), "mu": ("🇲🇺", "MU"),
    "botswana":  ("🇧🇼", "BW"), "bw": ("🇧🇼", "BW"),
    "namibia":   ("🇳🇦", "NA"), "na": ("🇳🇦", "NA"),

    # Europe
    "kosovo":    ("🇽🇰", "XK"), "xk": ("🇽🇰", "XK"),
    "united kingdom":("🇬🇧","GB"),"uk": ("🇬🇧", "GB"), "gb": ("🇬🇧", "GB"),
    "germany":   ("🇩🇪", "DE"), "de": ("🇩🇪", "DE"),
    "france":    ("🇫🇷", "FR"), "fr": ("🇫🇷", "FR"),
    "italy":     ("🇮🇹", "IT"), "it": ("🇮🇹", "IT"),
    "spain":     ("🇪🇸", "ES"), "es": ("🇪🇸", "ES"),
    "poland":    ("🇵🇱", "PL"), "pl": ("🇵🇱", "PL"),
    "netherlands":("🇳🇱","NL"), "nl": ("🇳🇱", "NL"),
    "portugal":  ("🇵🇹", "PT"), "pt": ("🇵🇹", "PT"),
    "sweden":    ("🇸🇪", "SE"), "se": ("🇸🇪", "SE"),
    "norway":    ("🇳🇴", "NO"), "no": ("🇳🇴", "NO"),
    "denmark":   ("🇩🇰", "DK"), "dk": ("🇩🇰", "DK"),
    "finland":   ("🇫🇮", "FI"), "fi": ("🇫🇮", "FI"),
    "switzerland":("🇨🇭","CH"), "ch": ("🇨🇭", "CH"),
    "austria":   ("🇦🇹", "AT"), "at": ("🇦🇹", "AT"),
    "belgium":   ("🇧🇪", "BE"), "be": ("🇧🇪", "BE"),
    "greece":    ("🇬🇷", "GR"), "gr": ("🇬🇷", "GR"),
    "ireland":   ("🇮🇪", "IE"), "ie": ("🇮🇪", "IE"),
    "czech":     ("🇨🇿", "CZ"), "cz": ("🇨🇿", "CZ"), "czech republic":("🇨🇿","CZ"),
    "romania":   ("🇷🇴", "RO"), "ro": ("🇷🇴", "RO"),
    "hungary":   ("🇭🇺", "HU"), "hu": ("🇭🇺", "HU"),
    "albania":   ("🇦🇱", "AL"), "al": ("🇦🇱", "AL"),
    "serbia":    ("🇷🇸", "RS"), "rs": ("🇷🇸", "RS"),
    "croatia":   ("🇭🇷", "HR"), "hr": ("🇭🇷", "HR"),
    "bulgaria":  ("🇧🇬", "BG"), "bg": ("🇧🇬", "BG"),
    "slovakia":  ("🇸🇰", "SK"), "sk": ("🇸🇰", "SK"),
    "slovenia":  ("🇸🇮", "SI"), "si": ("🇸🇮", "SI"),
    "estonia":   ("🇪🇪", "EE"), "ee": ("🇪🇪", "EE"),
    "latvia":    ("🇱🇻", "LV"), "lv": ("🇱🇻", "LV"),
    "lithuania": ("🇱🇹", "LT"), "lt": ("🇱🇹", "LT"),
    "belarus":   ("🇧🇾", "BY"), "by": ("🇧🇾", "BY"),
    "moldova":   ("🇲🇩", "MD"), "md": ("🇲🇩", "MD"),
    "bosnia":    ("🇧🇦", "BA"), "ba": ("🇧🇦", "BA"),
    "north macedonia":("🇲🇰","MK"),"mk": ("🇲🇰", "MK"),
    "cyprus":    ("🇨🇾", "CY"), "cy": ("🇨🇾", "CY"),
    "malta":     ("🇲🇹", "MT"), "mt": ("🇲🇹", "MT"),
    "iceland":   ("🇮🇸", "IS"), "is": ("🇮🇸", "IS"),
    "luxembourg":("🇱🇺","LU"), "lu": ("🇱🇺", "LU"),

    # Americas
    "united states":("🇺🇸","US"),"us": ("🇺🇸", "US"), "usa": ("🇺🇸", "US"),
    "canada":    ("🇨🇦", "CA"), "ca": ("🇨🇦", "CA"),
    "brazil":    ("🇧🇷", "BR"), "br": ("🇧🇷", "BR"),
    "mexico":    ("🇲🇽", "MX"), "mx": ("🇲🇽", "MX"),
    "colombia":  ("🇨🇴", "CO"), "co": ("🇨🇴", "CO"),
    "argentina": ("🇦🇷", "AR"), "ar": ("🇦🇷", "AR"),
    "peru":      ("🇵🇪", "PE"), "pe": ("🇵🇪", "PE"),
    "chile":     ("🇨🇱", "CL"), "cl": ("🇨🇱", "CL"),
    "venezuela": ("🇻🇪", "VE"), "ve": ("🇻🇪", "VE"),
    "ecuador":   ("🇪🇨", "EC"), "ec": ("🇪🇨", "EC"),
    "guatemala": ("🇬🇹", "GT"), "gt": ("🇬🇹", "GT"),
    "cuba":      ("🇨🇺", "CU"), "cu": ("🇨🇺", "CU"),
    "bolivia":   ("🇧🇴", "BO"), "bo": ("🇧🇴", "BO"),
    "dominican republic":("🇩🇴","DO"),"do": ("🇩🇴", "DO"),
    "honduras":  ("🇭🇳", "HN"), "hn": ("🇭🇳", "HN"),
    "paraguay":  ("🇵🇾", "PY"), "py": ("🇵🇾", "PY"),
    "el salvador":("🇸🇻","SV"),"sv": ("🇸🇻", "SV"),
    "nicaragua": ("🇳🇮", "NI"), "ni": ("🇳🇮", "NI"),
    "costa rica":("🇨🇷","CR"), "cr": ("🇨🇷", "CR"),
    "panama":    ("🇵🇦", "PA"), "pa": ("🇵🇦", "PA"),
    "uruguay":   ("🇺🇾", "UY"), "uy": ("🇺🇾", "UY"),
    "jamaica":   ("🇯🇲", "JM"), "jm": ("🇯🇲", "JM"),
    "haiti":     ("🇭🇹", "HT"), "ht": ("🇭🇹", "HT"),

    # Oceania
    "australia": ("🇦🇺", "AU"), "au": ("🇦🇺", "AU"),
    "new zealand":("🇳🇿","NZ"), "nz": ("🇳🇿", "NZ"),
    "fiji":      ("🇫🇯", "FJ"), "fj": ("🇫🇯", "FJ"),
    "papua new guinea":("🇵🇬","PG"),"pg": ("🇵🇬", "PG"),
    "celtel":    ("🇱🇰", "LK"),
}

PREFIX_ISO_MAP: Dict[str, Tuple[str, str]] = {
    # 1-digit
    "1":   ("🇺🇸", "US"), "7":   ("🇷🇺", "RU"),

    # 2-digit
    "20":  ("🇪🇬", "EG"), "27":  ("🇿🇦", "ZA"), "30":  ("🇬🇷", "GR"),
    "31":  ("🇳🇱", "NL"), "32":  ("🇧🇪", "BE"), "33":  ("🇫🇷", "FR"),
    "34":  ("🇪🇸", "ES"), "36":  ("🇭🇺", "HU"), "39":  ("🇮🇹", "IT"),
    "40":  ("🇷🇴", "RO"), "41":  ("🇨🇭", "CH"), "43":  ("🇦🇹", "AT"),
    "44":  ("🇬🇧", "GB"), "45":  ("🇩🇰", "DK"), "46":  ("🇸🇪", "SE"),
    "47":  ("🇳🇴", "NO"), "48":  ("🇵🇱", "PL"), "49":  ("🇩🇪", "DE"),
    "51":  ("🇵🇪", "PE"), "52":  ("🇲🇽", "MX"), "53":  ("🇨🇺", "CU"),
    "54":  ("🇦🇷", "AR"), "55":  ("🇧🇷", "BR"), "56":  ("🇨🇱", "CL"),
    "57":  ("🇨🇴", "CO"), "58":  ("🇻🇪", "VE"), "60":  ("🇲🇾", "MY"),
    "61":  ("🇦🇺", "AU"), "62":  ("🇮🇩", "ID"), "63":  ("🇵🇭", "PH"),
    "64":  ("🇳🇿", "NZ"), "65":  ("🇸🇬", "SG"), "66":  ("🇹🇭", "TH"),
    "81":  ("🇯🇵", "JP"), "82":  ("🇰🇷", "KR"), "84":  ("🇻🇳", "VN"),
    "86":  ("🇨🇳", "CN"), "90":  ("🇹🇷", "TR"), "91":  ("🇮🇳", "IN"),
    "92":  ("🇵🇰", "PK"), "93":  ("🇦🇫", "AF"), "94":  ("🇱🇰", "LK"),
    "95":  ("🇲🇲", "MM"), "98":  ("🇮🇷", "IR"),

    # 3-digit Africa
    "212": ("🇲🇦", "MA"), "213": ("🇩🇿", "DZ"), "216": ("🇹🇳", "TN"),
    "218": ("🇱🇾", "LY"), "220": ("🇬🇲", "GM"), "221": ("🇸🇳", "SN"),
    "222": ("🇲🇷", "MR"), "223": ("🇲🇱", "ML"), "224": ("🇬🇳", "GN"),
    "225": ("🇨🇮", "CI"), "226": ("🇧🇫", "BF"), "227": ("🇳🇪", "NE"),
    "228": ("🇹🇬", "TG"), "229": ("🇧🇯", "BJ"), "230": ("🇲🇺", "MU"),
    "231": ("🇱🇷", "LR"), "232": ("🇸🇱", "SL"), "233": ("🇬🇭", "GH"),
    "234": ("🇳🇬", "NG"), "235": ("🇹🇩", "TD"), "236": ("🇨🇫", "CF"),
    "237": ("🇨🇲", "CM"), "238": ("🇨🇻", "CV"), "239": ("🇸🇹", "ST"),
    "240": ("🇬🇶", "GQ"), "241": ("🇬🇦", "GA"), "242": ("🇨🇬", "CG"),
    "243": ("🇨🇩", "CD"), "244": ("🇦🇴", "AO"), "245": ("🇬🇼", "GW"),
    "248": ("🇸🇨", "SC"), "249": ("🇸🇩", "SD"), "250": ("🇷🇼", "RW"),
    "251": ("🇪🇹", "ET"), "252": ("🇸🇴", "SO"), "253": ("🇩🇯", "DJ"),
    "254": ("🇰🇪", "KE"), "255": ("🇹🇿", "TZ"), "256": ("🇺🇬", "UG"),
    "257": ("🇧🇮", "BI"), "258": ("🇲🇿", "MZ"), "260": ("🇿🇲", "ZM"),
    "261": ("🇲🇬", "MG"), "263": ("🇿🇼", "ZW"), "264": ("🇳🇦", "NA"),
    "265": ("🇲🇼", "MW"), "266": ("🇱🇸", "LS"), "267": ("🇧🇼", "BW"),
    "268": ("🇸🇿", "SZ"),

    # 3-digit Europe & Middle East
    "350": ("🇬🇮", "GI"), "351": ("🇵🇹", "PT"), "352": ("🇱🇺", "LU"),
    "353": ("🇮🇪", "IE"), "354": ("🇮🇸", "IS"), "355": ("🇦🇱", "AL"),
    "356": ("🇲🇹", "MT"), "357": ("🇨🇾", "CY"), "358": ("🇫🇮", "FI"),
    "359": ("🇧🇬", "BG"), "370": ("🇱🇹", "LT"), "371": ("🇱🇻", "LV"),
    "372": ("🇪🇪", "EE"), "373": ("🇲🇩", "MD"), "374": ("🇦🇲", "AM"),
    "375": ("🇧🇾", "BY"), "376": ("🇦🇩", "AD"), "377": ("🇲🇨", "MC"),
    "380": ("🇺🇦", "UA"), "381": ("🇷🇸", "RS"), "382": ("🇲🇪", "ME"),
    "383": ("🇽🇰", "XK"), "385": ("🇭🇷", "HR"), "386": ("🇸🇮", "SI"),
    "387": ("🇧🇦", "BA"), "389": ("🇲🇰", "MK"), "420": ("🇨🇿", "CZ"),
    "421": ("🇸🇰", "SK"), "852": ("🇭🇰", "HK"), "853": ("🇲🇴", "MO"),
    "855": ("🇰🇭", "KH"), "856": ("🇱🇦", "LA"), "880": ("🇧🇩", "BD"),
    "886": ("🇹🇼", "TW"), "960": ("🇲🇻", "MV"), "961": ("🇱🇧", "LB"),
    "962": ("🇯🇴", "JO"), "963": ("🇸🇾", "SY"), "964": ("🇮🇶", "IQ"),
    "965": ("🇰🇼", "KW"), "966": ("🇸🇦", "SA"), "967": ("🇾🇪", "YE"),
    "968": ("🇴🇲", "OM"), "970": ("🇵🇸", "PS"), "971": ("🇦🇪", "AE"),
    "972": ("🇮🇱", "IL"), "973": ("🇧🇭", "BH"), "974": ("🇶🇦", "QA"),
    "975": ("🇧🇹", "BT"), "976": ("🇲🇳", "MN"), "977": ("🇳🇵", "NP"),
    "992": ("🇹🇯", "TJ"), "993": ("🇹🇲", "TM"), "994": ("🇦🇿", "AZ"),
    "995": ("🇬🇪", "GE"), "996": ("🇰🇬", "KG"), "998": ("🇺🇿", "UZ"),

    # 3-digit Americas & Caribbean
    "501": ("🇧🇿", "BZ"), "502": ("🇬🇹", "GT"), "503": ("🇸🇻", "SV"),
    "504": ("🇭🇳", "HN"), "505": ("🇳🇮", "NI"), "506": ("🇨🇷", "CR"),
    "507": ("🇵🇦", "PA"), "509": ("🇭🇹", "HT"), "591": ("🇧🇴", "BO"),
    "592": ("🇬🇾", "GY"), "593": ("🇪🇨", "EC"), "595": ("🇵🇾", "PY"),
    "597": ("🇸🇷", "SR"), "598": ("🇺🇾", "UY"), "675": ("🇵🇬", "PG"),
    "679": ("🇫🇯", "FJ"),
}

def get_country_iso_display(item: Dict[str, Any]) -> str:
    # Try direct country code fields
    for field in ["countryCode", "iso", "iso2", "country"]:
        val = str(item.get(field) or "").strip().lower()
        if val in COUNTRY_ISO_DATA:
            flag, code = COUNTRY_ISO_DATA[val]
            return f"{flag} {code}"

    # Try rangeName / destinationName text
    raw_range = str(item.get("rangeName") or item.get("destinationName") or "").strip()
    if raw_range:
        parts = re.split(r"[-–—,:/]", raw_range)
        name_key = parts[0].strip().lower()
        if name_key in COUNTRY_ISO_DATA:
            flag, code = COUNTRY_ISO_DATA[name_key]
            return f"{flag} {code}"
        for country_name, (flag, code) in COUNTRY_ISO_DATA.items():
            if len(country_name) > 3 and country_name in raw_range.lower():
                return f"{flag} {code}"

    # Fallback: match phone number prefix
    raw_number = str(
        item.get("destinationNumber") or item.get("number") or
        item.get("rangeTemplate") or item.get("template") or
        item.get("dst") or ""
    ).strip()
    digits = re.sub(r"\D", "", raw_number)
    for p_len in [4, 3, 2, 1]:
        prefix = digits[:p_len]
        if prefix in PREFIX_ISO_MAP:
            flag, code = PREFIX_ISO_MAP[prefix]
            return f"{flag} {code}"

    return "🌐 XX"

def extract_country_name(range_str: str) -> str:
    if not range_str:
        return "Global"
    parts = re.split(r"[-–—,:/]", str(range_str))
    country = parts[0].strip()
    return country if country else str(range_str).strip()

def mask_phone_number(num_str: str) -> str:
    if not num_str:
        return ""
    clean = str(num_str).strip().lstrip("+")
    length = len(clean)
    if length <= 4:
        masked = clean
    elif length <= 6:
        masked = clean[:2] + "****" + clean[-2:]
    else:
        masked = f"{clean[:4]}****{clean[-3:]}"
    return f"+{masked}"

def extract_otp_code(text: str) -> str:
    if not text:
        return ""
    kw_match = re.search(
        r"(?:code|otp|pin|passcode|secret|verif\w*|kod\w*|c[oó]digo|clave|is)[:\s\-]+([A-Za-z0-9\-]{3,10})\b",
        text, re.IGNORECASE,
    )
    if kw_match:
        code = kw_match.group(1).strip()
        if any(c.isdigit() for c in code):
            return code
    hyphen_match = re.findall(r"\b\d{3}-\d{3}\b|\b\d{3}-\d{4}\b|\b\d{4}-\d{4}\b", text)
    if hyphen_match:
        return hyphen_match[0]
    digits_match = re.findall(r"\b[0-9]{4,8}\b", text)
    if digits_match:
        return digits_match[0]
    return ""

def parse_message_timestamp(time_str: str) -> float:
    if not time_str:
        return 0.0
    clean = str(time_str).strip()
    try:
        if "T" in clean:
            clean_iso = clean.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        dt = datetime.strptime(clean[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0

ISO_TO_COUNTRY_NAME: Dict[str, str] = {
    "AD": "Andorra", "AE": "United Arab Emirates", "AF": "Afghanistan", "AL": "Albania",
    "AM": "Armenia", "AO": "Angola", "AR": "Argentina", "AT": "Austria", "AU": "Australia",
    "AZ": "Azerbaijan", "BA": "Bosnia and Herzegovina", "BD": "Bangladesh", "BE": "Belgium",
    "BF": "Burkina Faso", "BG": "Bulgaria", "BH": "Bahrain", "BI": "Burundi", "BJ": "Benin",
    "BO": "Bolivia", "BR": "Brazil", "BT": "Bhutan", "BW": "Botswana", "BY": "Belarus",
    "BZ": "Belize", "CA": "Canada", "CD": "DR Congo", "CF": "Central African Republic",
    "CG": "Congo", "CH": "Switzerland", "CI": "Ivory Coast", "CL": "Chile", "CM": "Cameroon",
    "CN": "China", "CO": "Colombia", "CR": "Costa Rica", "CU": "Cuba", "CV": "Cape Verde",
    "CY": "Cyprus", "CZ": "Czech Republic", "DE": "Germany", "DJ": "Djibouti", "DK": "Denmark",
    "DO": "Dominican Republic", "DZ": "Algeria", "EC": "Ecuador", "EE": "Estonia", "EG": "Egypt",
    "ES": "Spain", "ET": "Ethiopia", "FI": "Finland", "FJ": "Fiji", "FR": "France",
    "GA": "Gabon", "GB": "United Kingdom", "GE": "Georgia", "GH": "Ghana", "GI": "Gibraltar",
    "GM": "Gambia", "GN": "Guinea", "GQ": "Equatorial Guinea", "GR": "Greece", "GT": "Guatemala",
    "GW": "Guinea-Bissau", "GY": "Guyana", "HK": "Hong Kong", "HN": "Honduras", "HR": "Croatia",
    "HT": "Haiti", "HU": "Hungary", "ID": "Indonesia", "IE": "Ireland", "IL": "Israel",
    "IN": "India", "IQ": "Iraq", "IR": "Iran", "IS": "Iceland", "IT": "Italy",
    "JM": "Jamaica", "JO": "Jordan", "JP": "Japan", "KE": "Kenya", "KG": "Kyrgyzstan",
    "KH": "Cambodia", "KR": "South Korea", "KW": "Kuwait", "KZ": "Kazakhstan", "LA": "Laos",
    "LB": "Lebanon", "LK": "Sri Lanka", "LR": "Liberia", "LS": "Lesotho", "LT": "Lithuania",
    "LU": "Luxembourg", "LV": "Latvia", "LY": "Libya", "MA": "Morocco", "MC": "Monaco",
    "MD": "Moldova", "ME": "Montenegro", "MG": "Madagascar", "MK": "North Macedonia", "ML": "Mali",
    "MM": "Myanmar", "MN": "Mongolia", "MO": "Macau", "MR": "Mauritania", "MT": "Malta",
    "MU": "Mauritius", "MV": "Maldives", "MW": "Malawi", "MX": "Mexico", "MY": "Malaysia",
    "MZ": "Mozambique", "NA": "Namibia", "NE": "Niger", "NG": "Nigeria", "NI": "Nicaragua",
    "NL": "Netherlands", "NO": "Norway", "NP": "Nepal", "NZ": "New Zealand", "OM": "Oman",
    "PA": "Panama", "PE": "Peru", "PG": "Papua New Guinea", "PH": "Philippines", "PK": "Pakistan",
    "PL": "Poland", "PS": "Palestine", "PT": "Portugal", "PY": "Paraguay", "QA": "Qatar",
    "RO": "Romania", "RS": "Serbia", "RU": "Russia", "RW": "Rwanda", "SA": "Saudi Arabia",
    "SC": "Seychelles", "SD": "Sudan", "SE": "Sweden", "SG": "Singapore", "SI": "Slovenia",
    "SK": "Slovakia", "SL": "Sierra Leone", "SN": "Senegal", "SO": "Somalia", "SR": "Suriname",
    "ST": "Sao Tome and Principe", "SV": "El Salvador", "SY": "Syria", "SZ": "Eswatini", "TD": "Chad",
    "TG": "Togo", "TH": "Thailand", "TJ": "Tajikistan", "TM": "Turkmenistan", "TN": "Tunisia",
    "TR": "Turkey", "TT": "Trinidad and Tobago", "TW": "Taiwan", "TZ": "Tanzania", "UA": "Ukraine",
    "UG": "Uganda", "US": "United States", "UY": "Uruguay", "UZ": "Uzbekistan", "VA": "Vatican City",
    "VE": "Venezuela", "VN": "Vietnam", "XK": "Kosovo", "YE": "Yemen", "ZA": "South Africa",
    "ZM": "Zambia", "ZW": "Zimbabwe"
}

def get_country_full_name(iso_code: str, item: Optional[Dict[str, Any]] = None) -> str:
    iso = (iso_code or "").strip().upper()
    if iso in ISO_TO_COUNTRY_NAME:
        return ISO_TO_COUNTRY_NAME[iso]
    if item:
        raw_range = str(item.get("rangeName") or item.get("destinationName") or item.get("country") or "").strip()
        if raw_range:
            parts = re.split(r"[-–—,:/]", raw_range)
            cand = parts[0].strip().title()
            if cand and len(cand) > 2:
                return cand
    return "Global"

# ==========================================
# 7. Database-Driven Language Detection Engine & Admin Manager
# ==========================================
SEED_LANGUAGES_DATA = {   'afrikaans': {   'display': '🇿🇦 Afrikaans',
                     'iso': 'AF',
                     'name': 'Afrikaans',
                     'phrases': {   'bevestigingskode': 4,
                                    'deel nie': 4,
                                    'jou': 3,
                                    'kode is': 3,
                                    'met enigiemand': 4,
                                    'moenie': 3,
                                    'sekuriteitskode': 4}},
    'albanian': {   'display': '🇦🇱 Albanian',
                    'iso': 'SQ',
                    'name': 'Albanian',
                    'phrases': {   'eshte': 2,
                                   'kodi juaj': 4,
                                   'me askend': 4,
                                   'mos e ndani': 4,
                                   'per te hyre': 3,
                                   'sigurise': 3,
                                   'verifikimit': 4}},
    'amharic': {'display': '🇪🇹 Amharic', 'iso': 'AM', 'name': 'Amharic', 'phrases': {}},
    'arabic': {'display': '🇸🇦 Arabic', 'iso': 'AR', 'name': 'Arabic', 'phrases': {}},
    'armenian': {'display': '🇦🇲 Armenian', 'iso': 'HY', 'name': 'Armenian', 'phrases': {}},
    'azerbaijani': {   'display': '🇦🇿 Azerbaijani',
                       'iso': 'AZ',
                       'name': 'Azerbaijani',
                       'phrases': {   'hec kimle': 5,
                                      'hec kimle paylasmayin': 6,
                                      'kodunuz': 3,
                                      'paylasmayin': 4,
                                      'tehlukesizlik kodu': 5,
                                      'tesdiq kodu': 5}},
    'basque': {   'display': '🇪🇸 Basque',
                  'iso': 'EU',
                  'name': 'Basque',
                  'phrases': {   'baieztapen-kodea': 5,
                                 'ez partekatu': 5,
                                 'inorekin': 4,
                                 'segurtasun-kodea': 5,
                                 'zure kodea': 4}},
    'bengali': {'display': '🇧🇩 Bengali', 'iso': 'BN', 'name': 'Bengali', 'phrases': {}},
    'bulgarian': {   'display': '🇧🇬 Bulgarian',
                     'iso': 'BG',
                     'name': 'Bulgarian',
                     'phrases': {   'koda si': 4,
                                    'kodat': 4,
                                    'kodat vi': 5,
                                    'kodyt': 4,
                                    'kodyt vi': 5,
                                    'ne spodelyayte': 6,
                                    'potvarzhdenie': 5,
                                    'potvyrzhdenie': 5,
                                    's nikogo': 4,
                                    'spodelyayte': 5,
                                    'vashiyat kod': 4,
                                    'za potvyrzhdenie': 5,
                                    'za sigurnost': 4}},
    'burmese': {'display': '🇲🇲 Burmese', 'iso': 'MY', 'name': 'Burmese', 'phrases': {}},
    'catalan': {   'display': '🇪🇸 Catalan',
                   'iso': 'CA',
                   'name': 'Catalan',
                   'phrases': {   'amb ningu': 4,
                                  'codi de': 3,
                                  'de seguretat': 3,
                                  'el teu': 3,
                                  'no comparteixis': 4,
                                  'verificacio': 3}},
    'chinese': {'display': '🇨🇳 Chinese', 'iso': 'ZH', 'name': 'Chinese', 'phrases': {}},
    'croatian': {   'display': '🇭🇷 Croatian',
                    'iso': 'HR',
                    'name': 'Croatian',
                    'phrases': {   'kod je': 3,
                                   'kod za potvrdu': 4,
                                   'kod za provjeru': 4,
                                   'ne delite': 4,
                                   'ne dijelite': 4,
                                   'ni sa kim': 4,
                                   'nikome': 3,
                                   'potvrdni kod': 4,
                                   'sigurnosni kod': 4,
                                   'vas kod': 3}},
    'czech': {   'display': '🇨🇿 Czech',
                 'iso': 'CS',
                 'name': 'Czech',
                 'phrases': {   'bezpecnostni': 3,
                                'kod je': 3,
                                'nesdilejte': 4,
                                'nikym': 3,
                                'overovaci kod': 4,
                                's nikym': 4,
                                'tento kod': 3,
                                'vas kod': 3}},
    'danish': {   'display': '🇩🇰 Danish',
                  'iso': 'DA',
                  'name': 'Danish',
                  'phrases': {   'bekraeftelseskode': 4,
                                 'del ikke': 4,
                                 'din kode': 3,
                                 'engangskode': 4,
                                 'kode er': 3,
                                 'med nogen': 4,
                                 'nogen': 2,
                                 'sikkerhedskode': 4}},
    'dutch': {   'display': '🇳🇱 Dutch',
                 'iso': 'NL',
                 'name': 'Dutch',
                 'phrases': {   'beveiligingscode': 4,
                                'bevestig': 3,
                                'bevestigingscode': 4,
                                'deel niet': 4,
                                'inloggen': 3,
                                'je code': 3,
                                'niemand': 3,
                                'niet met': 3,
                                'uw code': 3,
                                'verificatiecode': 4,
                                'wachtwoord': 3}},
    'english': {   'display': '🇬🇧 English',
                   'iso': 'EN',
                   'name': 'English',
                   'phrases': {   'confirm': 2,
                                  'device': 2,
                                  'do not give': 4,
                                  'do not share': 4,
                                  'is your': 3,
                                  'login code': 3,
                                  'one time': 3,
                                  'password': 2,
                                  'registered': 2,
                                  'reset': 2,
                                  'security code': 3,
                                  'sign in': 3,
                                  'to verify': 3,
                                  'verification code': 3,
                                  'your code': 3}},
    'estonian': {   'display': '🇪🇪 Estonian',
                    'iso': 'ET',
                    'name': 'Estonian',
                    'phrases': {   'arge jagage': 4,
                                   'kinnituskood': 4,
                                   'kood on': 3,
                                   'mitte kellegagi': 4,
                                   'teie': 3,
                                   'turvakood': 4}},
    'finnish': {   'display': '🇫🇮 Finnish',
                   'iso': 'FI',
                   'name': 'Finnish',
                   'phrases': {   'ala jaa': 4,
                                  'kenellekaan': 4,
                                  'koodisi': 3,
                                  'koodisi on': 4,
                                  'tata koodia': 3,
                                  'turvakoodi': 4,
                                  'vahvistuskoodi': 4}},
    'french': {   'display': '🇫🇷 French',
                  'iso': 'FR',
                  'name': 'French',
                  'phrases': {   'aucun': 3,
                                 'ce code': 3,
                                 'confirmation': 2,
                                 'connexion': 3,
                                 'de securite': 3,
                                 'entrez': 3,
                                 'identifiant': 3,
                                 'mot de passe': 4,
                                 'ne le partagez': 4,
                                 'ne partagez': 4,
                                 'partagez': 3,
                                 'personne': 2,
                                 'reinitialisation': 3,
                                 'reinitialiser': 3,
                                 'ton code': 4,
                                 'utiliser': 2,
                                 'verification': 2,
                                 'veuillez': 3,
                                 'votre code': 4,
                                 'votre compte': 3}},
    'galician': {   'display': '🇪🇸 Galician',
                    'iso': 'GL',
                    'name': 'Galician',
                    'phrases': {'codigo de verificacion': 5, 'con ninguen': 4, 'non compartas': 5, 'o teu codigo': 4}},
    'georgian': {'display': '🇬🇪 Georgian', 'iso': 'KA', 'name': 'Georgian', 'phrases': {}},
    'german': {   'display': '🇩🇪 German',
                  'iso': 'DE',
                  'name': 'German',
                  'phrases': {   'anmeldung': 3,
                                 'bestatigungscode': 4,
                                 'dein code': 4,
                                 'einmalpasswort': 4,
                                 'ihr code': 4,
                                 'ihr konto': 3,
                                 'ihre': 2,
                                 'ist dein': 3,
                                 'lautet': 3,
                                 'mit niemandem': 4,
                                 'nicht weitergeben': 4,
                                 'niemandem': 3,
                                 'passwort': 2,
                                 'sicherheitscode': 4,
                                 'teilen sie': 4,
                                 'verifizierungscode': 4,
                                 'weitergeben': 4,
                                 'zurucksetzen': 3}},
    'greek': {   'display': '🇬🇷 Greek',
                 'iso': 'EL',
                 'name': 'Greek',
                 'phrases': {   'asfaleias': 4,
                                'kodikos epivevaiosis': 5,
                                'min moirazeste': 5,
                                'o kodikos sas': 5,
                                'ton kodiko': 4}},
    'gujarati': {'display': '🇮🇳 Gujarati', 'iso': 'GU', 'name': 'Gujarati', 'phrases': {}},
    'hausa': {   'display': '🇳🇬 Hausa',
                 'iso': 'HA',
                 'name': 'Hausa',
                 'phrases': {'kada ka raba': 5, 'karka raba': 5, 'lambarka': 3, 'tabbatarwa': 4}},
    'hebrew': {'display': '🇮🇱 Hebrew', 'iso': 'HE', 'name': 'Hebrew', 'phrases': {}},
    'hindi': {'display': '🇮🇳 Hindi', 'iso': 'HI', 'name': 'Hindi', 'phrases': {}},
    'hungarian': {   'display': '🇭🇺 Hungarian',
                     'iso': 'HU',
                     'name': 'Hungarian',
                     'phrases': {   'biztonsagi kod': 4,
                                    'ellenorzo kod': 4,
                                    'kodod': 4,
                                    'megerosito kod': 4,
                                    'ne oszd meg': 4,
                                    'senkivel': 4}},
    'indonesian': {   'display': '🇮🇩 Indonesian',
                      'iso': 'ID',
                      'name': 'Indonesian',
                      'phrases': {   'adalah': 2,
                                     'adalah kode': 4,
                                     'akun anda': 3,
                                     'anda': 2,
                                     'jangan bagikan': 4,
                                     'jangan berikan': 4,
                                     'keamanan': 3,
                                     'kode': 2,
                                     'kode keamanan': 4,
                                     'kode konfirmasi': 4,
                                     'kode verifikasi': 4,
                                     'masuk ke': 3,
                                     'rahasia': 3}},
    'irish': {   'display': '🇮🇪 Irish',
                 'iso': 'GA',
                 'name': 'Irish',
                 'phrases': {'cod fioraithe': 5, 'do chod': 4, 'le haon duine': 4, 'na roinn': 5}},
    'italian': {   'display': '🇮🇹 Italian',
                   'iso': 'IT',
                   'name': 'Italian',
                   'phrases': {   'accesso': 2,
                                  'con nessuno': 4,
                                  'condividere': 3,
                                  'conferma': 2,
                                  'di sicurezza': 3,
                                  'di verifica': 3,
                                  'il tuo codice': 4,
                                  'nessuno': 3,
                                  'non condividere': 4,
                                  'per accedere': 3,
                                  'questo codice': 3,
                                  'reimpostare': 3,
                                  'suo codice': 4,
                                  'tuo codice': 4}},
    'japanese': {'display': '🇯🇵 Japanese', 'iso': 'JA', 'name': 'Japanese', 'phrases': {}},
    'kannada': {'display': '🇮🇳 Kannada', 'iso': 'KN', 'name': 'Kannada', 'phrases': {}},
    'kazakh': {'display': '🇰🇿 Kazakh', 'iso': 'KK', 'name': 'Kazakh', 'phrases': {}},
    'khmer': {'display': '🇰🇭 Khmer', 'iso': 'KM', 'name': 'Khmer', 'phrases': {}},
    'korean': {'display': '🇰🇷 Korean', 'iso': 'KO', 'name': 'Korean', 'phrases': {}},
    'lao': {'display': '🇱🇦 Lao', 'iso': 'LO', 'name': 'Lao', 'phrases': {}},
    'latvian': {   'display': '🇱🇻 Latvian',
                   'iso': 'LV',
                   'name': 'Latvian',
                   'phrases': {   'apstiprinajuma kods': 4,
                                  'ar nevienu': 4,
                                  'drosibas kods': 4,
                                  'jusu': 3,
                                  'kods ir': 3,
                                  'nedalieties': 4}},
    'lithuanian': {   'display': '🇱🇹 Lithuanian',
                      'iso': 'LT',
                      'name': 'Lithuanian',
                      'phrases': {   'jusu': 3,
                                     'kodas yra': 3,
                                     'nesidalinkite': 4,
                                     'patvirtinimo kodas': 4,
                                     'saugos kodas': 4,
                                     'su niekuo': 4}},
    'malay': {   'display': '🇲🇾 Malay',
                 'iso': 'MS',
                 'name': 'Malay',
                 'phrases': {   'akaun anda': 3,
                                'anda ialah': 3,
                                'ialah': 2,
                                'ialah kod': 4,
                                'jangan kongsi': 4,
                                'keselamatan': 3,
                                'kod': 2,
                                'kod keselamatan': 4,
                                'kod pengesahan': 4,
                                'kongsi': 3}},
    'malayalam': {'display': '🇮🇳 Malayalam', 'iso': 'ML', 'name': 'Malayalam', 'phrases': {}},
    'marathi': {'display': '🇮🇳 Marathi', 'iso': 'MR', 'name': 'Marathi', 'phrases': {}},
    'nepali': {'display': '🇳🇵 Nepali', 'iso': 'NE', 'name': 'Nepali', 'phrases': {}},
    'norwegian': {   'display': '🇳🇴 Norwegian',
                     'iso': 'NO',
                     'name': 'Norwegian',
                     'phrases': {   'bekreftelseskode': 4,
                                    'din kode': 3,
                                    'engangskode': 4,
                                    'ikke del': 4,
                                    'kode er': 3,
                                    'koden med noen': 4,
                                    'noen': 2,
                                    'sikkerhetskode': 4}},
    'odia': {'display': '🇮🇳 Odia', 'iso': 'OR', 'name': 'Odia', 'phrases': {}},
    'persian': {'display': '🇮🇷 Persian', 'iso': 'FA', 'name': 'Persian', 'phrases': {}},
    'polish': {   'display': '🇵🇱 Polish',
                  'iso': 'PL',
                  'name': 'Polish',
                  'phrases': {   'bezpieczenstwa': 3,
                                 'haslo': 3,
                                 'kod weryfikacyjny': 4,
                                 'logowania': 3,
                                 'nie udostepniaj': 4,
                                 'nikomu': 3,
                                 'resetowania': 3,
                                 'twoj kod': 4,
                                 'twoje konto': 3,
                                 'udostepniaj': 3}},
    'portuguese': {   'display': '🇵🇹 Portuguese',
                      'iso': 'PT',
                      'name': 'Portuguese',
                      'phrases': {   'aparelho': 3,
                                     'cadastrado': 3,
                                     'com ninguem': 4,
                                     'compartilhe': 3,
                                     'confirmacao': 2,
                                     'de seguranca': 3,
                                     'entrar na': 3,
                                     'este codigo': 3,
                                     'nao compartilhe': 4,
                                     'ninguem': 3,
                                     'para entrar': 3,
                                     'redefinir': 3,
                                     'senha': 3,
                                     'seu codigo': 4,
                                     'sua conta': 3,
                                     'sua senha': 4,
                                     'utilize': 3,
                                     'verificacao': 3}},
    'punjabi': {'display': '🇮🇳 Punjabi', 'iso': 'PA', 'name': 'Punjabi', 'phrases': {}},
    'romanian': {   'display': '🇷🇴 Romanian',
                    'iso': 'RO',
                    'name': 'Romanian',
                    'phrases': {   'autentificare': 3,
                                   'codul tau': 4,
                                   'de securitate': 3,
                                   'este': 1,
                                   'nimanui': 4,
                                   'nu trimite': 4,
                                   'verificare': 3}},
    'russian': {   'display': '🇷🇺 Russian',
                   'iso': 'RU',
                   'name': 'Russian',
                   'phrases': {   'dlya vkhoda': 4,
                                  'kod bezopasnosti': 5,
                                  'kod podtverzhdeniya': 5,
                                  'ne soobshchayte': 5,
                                  'nikomu ne': 4,
                                  'odnozoravyj': 4,
                                  'vash kod': 3}},
    'sinhala': {'display': '🇱🇰 Sinhala', 'iso': 'SI', 'name': 'Sinhala', 'phrases': {}},
    'slovak': {   'display': '🇸🇰 Slovak',
                  'iso': 'SK',
                  'name': 'Slovak',
                  'phrases': {   'bezpecnostny': 3,
                                 'kod je': 3,
                                 'nezdielajte': 4,
                                 'overovaci kod': 4,
                                 's nikym': 4,
                                 'tento kod': 3,
                                 'vas kod': 3}},
    'slovenian': {   'display': '🇸🇮 Slovenian',
                     'iso': 'SL',
                     'name': 'Slovenian',
                     'phrases': {   'koda je': 3,
                                    'ne delite': 4,
                                    'potrditvena koda': 4,
                                    'varnostna koda': 4,
                                    'vasa': 3,
                                    'z nikomer': 4}},
    'spanish': {   'display': '🇪🇸 Spanish',
                   'iso': 'ES',
                   'name': 'Spanish',
                   'phrases': {   'clave': 2,
                                  'codigo de': 3,
                                  'compartas': 3,
                                  'con nadie': 4,
                                  'confirmacion': 2,
                                  'de seguridad': 3,
                                  'dispositivo': 2,
                                  'este codigo': 3,
                                  'ingresa': 3,
                                  'iniciar sesion': 3,
                                  'nadie': 2,
                                  'no compartas': 4,
                                  'restablecer': 3,
                                  'su codigo': 4,
                                  'tu codigo': 4,
                                  'tu cuenta': 3,
                                  'utiliza': 2,
                                  'verificacion': 2}},
    'swahili': {   'display': '🇰🇪 Swahili',
                   'iso': 'SW',
                   'name': 'Swahili',
                   'phrases': {   'msimbo': 3,
                                  'na mtu': 3,
                                  'nambari yako': 4,
                                  'usishiriki': 4,
                                  'wa usalama': 4,
                                  'wa uthibitishaji': 4}},
    'swedish': {   'display': '🇸🇪 Swedish',
                   'iso': 'SV',
                   'name': 'Swedish',
                   'phrases': {   'dela inte': 4,
                                  'din kod': 3,
                                  'engangskod': 4,
                                  'inte koden': 3,
                                  'kod ar': 3,
                                  'med nagon': 4,
                                  'nagon': 2,
                                  'sakerhetskod': 4,
                                  'verifieringskod': 4}},
    'tagalog': {   'display': '🇵🇭 Tagalog',
                   'iso': 'TL',
                   'name': 'Tagalog',
                   'phrases': {   'ang iyong': 4,
                                  'huwag ibahagi': 4,
                                  'huwag ipamigay': 4,
                                  'ibahagi': 3,
                                  'sa kaninuman': 4}},
    'tamil': {'display': '🇮🇳 Tamil', 'iso': 'TA', 'name': 'Tamil', 'phrases': {}},
    'telugu': {'display': '🇮🇳 Telugu', 'iso': 'TE', 'name': 'Telugu', 'phrases': {}},
    'thai': {'display': '🇹🇭 Thai', 'iso': 'TH', 'name': 'Thai', 'phrases': {}},
    'turkish': {   'display': '🇹🇷 Turkish',
                   'iso': 'TR',
                   'name': 'Turkish',
                   'phrases': {   'dogrulama': 3,
                                  'giris': 2,
                                  'guvenlik': 3,
                                  'hesabiniz': 3,
                                  'kimseyle': 3,
                                  'kodu': 2,
                                  'kodunuz': 4,
                                  'onay': 2,
                                  'paylasmayin': 4,
                                  'sifirlama': 3,
                                  'sifre': 3,
                                  'tek kullanimlik': 4}},
    'ukrainian': {   'display': '🇺🇦 Ukrainian',
                     'iso': 'UK',
                     'name': 'Ukrainian',
                     'phrases': {   'dlya vkhodu': 4,
                                    'kod bezpeky': 5,
                                    'kod pidtverdzhennya': 5,
                                    'ne dilitisya': 5,
                                    'ne dilitsya': 5}},
    'urdu': {'display': '🇵🇰 Urdu', 'iso': 'UR', 'name': 'Urdu', 'phrases': {}},
    'uzbek': {   'display': '🇺🇿 Uzbek',
                 'iso': 'UZ',
                 'name': 'Uzbek',
                 'phrases': {'bermang': 4, 'hech kimga': 4, 'kodingiz': 4, 'tasdiqlash kodi': 4, 'xavfsizlik kodi': 4}},
    'vietnamese': {   'display': '🇻🇳 Vietnamese',
                      'iso': 'VI',
                      'name': 'Vietnamese',
                      'phrases': {   'bao mat': 3,
                                     'chia se': 2,
                                     'dang nhap': 3,
                                     'khong chia se': 4,
                                     'ma otp': 3,
                                     'ma xac minh': 4,
                                     'mat khau': 4,
                                     'tai khoan': 3}}}

DEFAULT_LANG_FLAGS: Dict[str, str] = {
    "french": "🇫🇷", "spanish": "🇪🇸", "portuguese": "🇵🇹", "german": "🇩🇪", "italian": "🇮🇹",
    "turkish": "🇹🇷", "bulgarian": "🇧🇬", "russian": "🇷🇺", "ukrainian": "🇺🇦", "greek": "🇬🇷",
    "indonesian": "🇮🇩", "malay": "🇲🇾", "dutch": "🇳🇱", "polish": "🇵🇱", "romanian": "🇷🇴",
    "swedish": "🇸🇪", "danish": "🇩🇰", "finnish": "🇫🇮", "norwegian": "🇳🇴", "czech": "🇨🇿",
    "slovak": "🇸🇰", "hungarian": "🇭🇺", "tagalog": "🇵🇭", "swahili": "🇰🇪", "croatian": "🇭🇷",
    "catalan": "🇪🇸", "albanian": "🇦🇱", "lithuanian": "🇱🇹", "latvian": "🇱🇻", "estonian": "🇪🇪",
    "slovenian": "🇸🇮", "azerbaijani": "🇦🇿", "uzbek": "🇺🇿", "afrikaans": "🇿🇦", "basque": "🇪🇸",
    "galician": "🇪🇸", "irish": "🇮🇪", "hausa": "🇳🇬", "vietnamese": "🇻🇳", "english": "🇬🇧",
    "amharic": "🇪🇹", "georgian": "🇬🇪", "armenian": "🇦🇲", "hebrew": "🇮🇱", "arabic": "🇸🇦",
    "urdu": "🇵🇰", "persian": "🇮🇷", "bengali": "🇧🇩", "punjabi": "🇮🇳", "gujarati": "🇮🇳",
    "marathi": "🇮🇳", "nepali": "🇳🇵", "hindi": "🇮🇳", "tamil": "🇮🇳", "telugu": "🇮🇳",
    "kannada": "🇮🇳", "malayalam": "🇮🇳", "sinhala": "🇱🇰", "odia": "🇮🇳", "thai": "🇹🇭",
    "lao": "🇱🇦", "khmer": "🇰🇭", "burmese": "🇲🇲", "kazakh": "🇰🇿", "japanese": "🇯🇵",
    "korean": "🇰🇷", "chinese": "🇨🇳", "somali": "🇸🇴", "kurdish": "🇮🇶"
}

def normalize_text_for_lang(text: str) -> str:
    low = text.lower().replace("ə", "e").replace("ı", "i")
    nfkd = unicodedata.normalize('NFD', low)
    return ''.join([c for c in nfkd if not unicodedata.combining(c)])

DB_LANGUAGES_CACHE: Dict[str, Dict[str, Any]] = {}
ADMIN_LANG_STATES: Dict[int, Dict[str, Any]] = {}

def init_and_seed_language_database(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS languages (
            lang_id TEXT PRIMARY KEY,
            lang_name TEXT NOT NULL,
            iso_code TEXT NOT NULL,
            display_name TEXT NOT NULL,
            is_enabled INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS language_phrases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lang_id TEXT NOT NULL,
            phrase TEXT NOT NULL,
            weight INTEGER DEFAULT 3,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lang_phrase ON language_phrases(lang_id);")

    try:
        count = conn.execute("SELECT COUNT(*) FROM languages;").fetchone()[0]
        if count == 0:
            seed_data = SEED_LANGUAGES_DATA
            for lid, info in seed_data.items():
                conn.execute("""
                    INSERT OR IGNORE INTO languages (lang_id, lang_name, iso_code, display_name, is_enabled)
                    VALUES (?, ?, ?, ?, 1);
                """, (lid, info["name"], info["iso"], info["display"]))
                for phrase, weight in info.get("phrases", {}).items():
                    conn.execute("""
                        INSERT INTO language_phrases (lang_id, phrase, weight)
                        VALUES (?, ?, ?);
                    """, (lid, phrase, weight))
            conn.commit()
    except Exception as e:
        logger.warning(f"Notice seeding language database: {e}")

def load_languages_from_db():
    global DB_LANGUAGES_CACHE
    DB_LANGUAGES_CACHE.clear()
    try:
        with get_db_connection() as conn:
            init_and_seed_language_database(conn)
            rows = conn.execute("SELECT lang_id, lang_name, iso_code, display_name, is_enabled FROM languages;").fetchall()
            for r in rows:
                DB_LANGUAGES_CACHE[r["lang_id"]] = {
                    "name": r["lang_name"],
                    "iso": r["iso_code"],
                    "display_name": r["display_name"],
                    "is_enabled": bool(r["is_enabled"]),
                    "phrases": {}
                }
            prows = conn.execute("SELECT lang_id, phrase, weight FROM language_phrases;").fetchall()
            for pr in prows:
                lid = pr["lang_id"]
                if lid in DB_LANGUAGES_CACHE:
                    DB_LANGUAGES_CACHE[lid]["phrases"][pr["phrase"]] = pr["weight"]
    except Exception as e:
        logger.warning(f"Notice loading language database: {e}")

def db_add_language(lang_name: str, iso_code: str, display_name: str = ""):
    lid = lang_name.strip().lower()
    cname = lang_name.strip().title()
    ciso = iso_code.strip().upper()
    flag = DEFAULT_LANG_FLAGS.get(lid, "🌐")
    disp = display_name.strip() or f"{flag} {cname}"
    with get_db_connection() as conn:
        init_and_seed_language_database(conn)
        conn.execute("""
            INSERT INTO languages (lang_id, lang_name, iso_code, display_name, is_enabled)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(lang_id) DO UPDATE SET
                lang_name = excluded.lang_name,
                iso_code = excluded.iso_code,
                display_name = excluded.display_name,
                is_enabled = 1;
        """, (lid, cname, ciso, disp))
        conn.commit()
    load_languages_from_db()

def db_edit_display_name(lang_id: str, new_display: str):
    lid = lang_id.strip().lower()
    disp = new_display.strip()
    if not disp:
        return
    with get_db_connection() as conn:
        init_and_seed_language_database(conn)
        conn.execute("UPDATE languages SET display_name = ? WHERE lang_id = ?;", (disp, lid))
        conn.commit()
    load_languages_from_db()

def db_add_phrase(lang_id: str, raw_text: str, weight: int = 5):
    lid = lang_id.strip().lower()
    norm = normalize_text_for_lang(raw_text).strip()
    if not norm or len(norm) < 2:
        return
    with get_db_connection() as conn:
        init_and_seed_language_database(conn)
        exists = conn.execute("SELECT 1 FROM language_phrases WHERE lang_id = ? AND phrase = ? LIMIT 1;", (lid, norm)).fetchone()
        if not exists:
            conn.execute("INSERT INTO language_phrases (lang_id, phrase, weight) VALUES (?, ?, ?);", (lid, norm, weight))
            conn.commit()
    load_languages_from_db()

def db_clear_phrases(lang_id: str):
    lid = lang_id.strip().lower()
    with get_db_connection() as conn:
        init_and_seed_language_database(conn)
        conn.execute("DELETE FROM language_phrases WHERE lang_id = ?;", (lid,))
        conn.commit()
    load_languages_from_db()

def db_delete_language(lang_id: str):
    lid = lang_id.strip().lower()
    with get_db_connection() as conn:
        init_and_seed_language_database(conn)
        conn.execute("DELETE FROM languages WHERE lang_id = ?;", (lid,))
        conn.execute("DELETE FROM language_phrases WHERE lang_id = ?;", (lid,))
        conn.commit()
    load_languages_from_db()

def db_toggle_language(lang_id: str) -> bool:
    lid = lang_id.strip().lower()
    with get_db_connection() as conn:
        init_and_seed_language_database(conn)
        row = conn.execute("SELECT is_enabled FROM languages WHERE lang_id = ? LIMIT 1;", (lid,)).fetchone()
        if row:
            new_val = 0 if row[0] == 1 else 1
            conn.execute("UPDATE languages SET is_enabled = ? WHERE lang_id = ?;", (new_val, lid))
            conn.commit()
            load_languages_from_db()
            return bool(new_val)
    return False

LANGS_PER_PAGE = 8

def build_languages_menu(page: int = 0) -> Tuple[str, InlineKeyboardMarkup]:
    all_langs = sorted(DB_LANGUAGES_CACHE.values(), key=lambda x: x["name"])
    total = len(all_langs)
    total_pages = max(1, (total + LANGS_PER_PAGE - 1) // LANGS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * LANGS_PER_PAGE
    page_langs = all_langs[start_idx : start_idx + LANGS_PER_PAGE]

    text = (
        "🌐 <b>SMS Language Detection Manager (Database-Driven)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "All language rules, display names, and phrases are stored in the SQLite database.\n"
        "Tap any language to customize its <b>display label</b>, add <b>SMS phrases</b>, or clear keywords.\n\n"
        f"• <b>Total Languages in DB:</b> <code>{total}</code>\n"
        f"• <b>Active Page:</b> <code>{page + 1} / {total_pages}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )

    buttons = []
    for i in range(0, len(page_langs), 2):
        row = []
        l1 = page_langs[i]
        tag1 = " (Off)" if not l1["is_enabled"] else ""
        lid1 = l1["name"].lower()
        row.append(InlineKeyboardButton(f"{l1['display_name']}{tag1}", callback_data=f"lang_v_{lid1}"))
        if i + 1 < len(page_langs):
            l2 = page_langs[i + 1]
            tag2 = " (Off)" if not l2["is_enabled"] else ""
            lid2 = l2["name"].lower()
            row.append(InlineKeyboardButton(f"{l2['display_name']}{tag2}", callback_data=f"lang_v_{lid2}"))
        buttons.append(row)

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"lang_mgr_p_{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"lang_mgr_p_{page + 1}"))
    buttons.append(nav_row)

    data = load_stored_data()
    show_lang = data.get("show_sms_language", True)
    lang_btn_text = "🔔 Language on SMS: ON ✅" if show_lang else "🔕 Language on SMS: OFF ❌"

    buttons.append([
        InlineKeyboardButton(lang_btn_text, callback_data=f"lang_toggle_sms_{page}")
    ])
    buttons.append([
        InlineKeyboardButton("➕ Add New Language", callback_data="lang_add_new"),
        InlineKeyboardButton("🔙 Back to Dashboard", callback_data="refresh_dash")
    ])

    return text, InlineKeyboardMarkup(buttons)

def build_language_detail_menu(lang_id: str) -> Tuple[str, InlineKeyboardMarkup]:
    lid = lang_id.strip().lower()
    info = DB_LANGUAGES_CACHE.get(lid)
    if not info:
        return "❌ Language not found in database.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="lang_mgr_p_0")]])

    phrases = list(info.get("phrases", {}).keys())
    phrase_lines = ""
    if phrases:
        phrase_lines = "\n\n📝 <b>Database Saved Phrases (" + str(len(phrases)) + "):</b>\n"
        for idx, p in enumerate(phrases[:8], 1):
            phrase_lines += f"  {idx}. <code>{html.escape(p)}</code>\n"
        if len(phrases) > 8:
            phrase_lines += f"  <i>...and {len(phrases) - 8} more in database</i>\n"
    else:
        phrase_lines = "\n\n📝 <b>Database Saved Phrases:</b> <i>None (native script detection)</i>"

    status_str = "🟢 Active & Enabled" if info["is_enabled"] else "⏸️ Disabled"

    text = (
        f"🌐 <b>Database Language: {html.escape(info['name'])}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Canonical Name:</b> <code>{html.escape(info['name'])}</code>\n"
        f"• <b>ISO Code:</b> <code>{html.escape(info['iso'])}</code>\n"
        f"• <b>SMS Display Label:</b> <code>{html.escape(info['display_name'])}</code>\n"
        f"• <b>Status:</b> {status_str}\n"
        "━━━━━━━━━━━━━━━━━━━━"
        f"{phrase_lines}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>SMS matching this language will show the exact Display Label above.</i>"
    )

    buttons = [
        [
            InlineKeyboardButton("✏️ Edit Display Name", callback_data=f"lang_edname_{lid}"),
            InlineKeyboardButton("➕ Add SMS / Phrase", callback_data=f"lang_addphr_{lid}")
        ]
    ]
    if phrases:
        buttons.append([InlineKeyboardButton("🗑️ Clear Saved Phrases", callback_data=f"lang_clrphr_{lid}")])

    tog_label = "⏸️ Disable" if info["is_enabled"] else "▶️ Enable"
    buttons.append([
        InlineKeyboardButton(tog_label, callback_data=f"lang_tog_{lid}"),
        InlineKeyboardButton("❌ Delete Language", callback_data=f"lang_del_{lid}")
    ])
    buttons.append([InlineKeyboardButton("🔙 Back to Languages", callback_data="lang_mgr_p_0")])

    return text, InlineKeyboardMarkup(buttons)

def detect_sms_language(text: str) -> Tuple[str, str]:
    if not text:
        eng = DB_LANGUAGES_CACHE.get("english", {})
        return (eng.get("display_name", "🇬🇧 English"), "EN")

    t = text.strip()

    # 1. Non-Latin Native Scripts
    if re.search(r"[\u1200-\u137F]", t):
        info = DB_LANGUAGES_CACHE.get("amharic", {})
        return (info.get("display_name", "🇪🇹 Amharic"), "AM")
    if re.search(r"[\u10A0-\u10FF\u2D00-\u2D2F]", t):
        info = DB_LANGUAGES_CACHE.get("georgian", {})
        return (info.get("display_name", "🇬🇪 Georgian"), "KA")
    if re.search(r"[\u0530-\u058F]", t):
        info = DB_LANGUAGES_CACHE.get("armenian", {})
        return (info.get("display_name", "🇦🇲 Armenian"), "HY")
    if re.search(r"[\u0590-\u05FF]", t):
        info = DB_LANGUAGES_CACHE.get("hebrew", {})
        return (info.get("display_name", "🇮🇱 Hebrew"), "HE")
    if re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", t):
        if any(c in t for c in ["ے", "ٹ", "ڈ", "ڑ", "ں"]) or "آپ" in t:
            info = DB_LANGUAGES_CACHE.get("urdu", {})
            return (info.get("display_name", "🇵🇰 Urdu"), "UR")
        if any(c in t for c in ["گ", "چ", "پ", "ژ"]) or "اینستاگرام" in t:
            info = DB_LANGUAGES_CACHE.get("persian", {})
            return (info.get("display_name", "🇮🇷 Persian"), "FA")
        info = DB_LANGUAGES_CACHE.get("arabic", {})
        return (info.get("display_name", "🇸🇦 Arabic"), "AR")
    if re.search(r"[\u0981-\u09BC\u09BE-\u09CD\u09D7\u09DC-\u09E3\u09F0-\u09FD]", t):
        info = DB_LANGUAGES_CACHE.get("bengali", {})
        return (info.get("display_name", "🇧🇩 Bengali"), "BN")
    if re.search(r"[\u0A01-\u0A75]", t):
        info = DB_LANGUAGES_CACHE.get("punjabi", {})
        return (info.get("display_name", "🇮🇳 Punjabi"), "PA")
    if re.search(r"[\u0A81-\u0AF9]", t):
        info = DB_LANGUAGES_CACHE.get("gujarati", {})
        return (info.get("display_name", "🇮🇳 Gujarati"), "GU")
    if re.search(r"[\u0904-\u0939\u093D-\u094F\u0958-\u0963]", t):
        low_d = t.lower()
        if "आहे" in low_d or "तुमचा" in low_d:
            info = DB_LANGUAGES_CACHE.get("marathi", {})
            return (info.get("display_name", "🇮🇳 Marathi"), "MR")
        if "हो" in low_d or "तपाईंको" in low_d:
            info = DB_LANGUAGES_CACHE.get("nepali", {})
            return (info.get("display_name", "🇳🇵 Nepali"), "NE")
        info = DB_LANGUAGES_CACHE.get("hindi", {})
        return (info.get("display_name", "🇮🇳 Hindi"), "HI")
    if re.search(r"[\u0B00-\u0B7F]", t):
        info = DB_LANGUAGES_CACHE.get("odia", {})
        return (info.get("display_name", "🇮🇳 Odia"), "OR")
    if re.search(r"[\u0B80-\u0BFF]", t):
        info = DB_LANGUAGES_CACHE.get("tamil", {})
        return (info.get("display_name", "🇮🇳 Tamil"), "TA")
    if re.search(r"[\u0C00-\u0C7F]", t):
        info = DB_LANGUAGES_CACHE.get("telugu", {})
        return (info.get("display_name", "🇮🇳 Telugu"), "TE")
    if re.search(r"[\u0C80-\u0CFF]", t):
        info = DB_LANGUAGES_CACHE.get("kannada", {})
        return (info.get("display_name", "🇮🇳 Kannada"), "KN")
    if re.search(r"[\u0D00-\u0D7F]", t):
        info = DB_LANGUAGES_CACHE.get("malayalam", {})
        return (info.get("display_name", "🇮🇳 Malayalam"), "ML")
    if re.search(r"[\u0D80-\u0DFF]", t):
        info = DB_LANGUAGES_CACHE.get("sinhala", {})
        return (info.get("display_name", "🇱🇰 Sinhala"), "SI")
    if re.search(r"[\u0E00-\u0E7F]", t):
        info = DB_LANGUAGES_CACHE.get("thai", {})
        return (info.get("display_name", "🇹🇭 Thai"), "TH")
    if re.search(r"[\u0EA0-\u0EFF]", t):
        info = DB_LANGUAGES_CACHE.get("lao", {})
        return (info.get("display_name", "🇱🇦 Lao"), "LO")
    if re.search(r"[\u1780-\u17FF]", t):
        info = DB_LANGUAGES_CACHE.get("khmer", {})
        return (info.get("display_name", "🇰🇭 Khmer"), "KM")
    if re.search(r"[\u1000-\u109F]", t):
        info = DB_LANGUAGES_CACHE.get("burmese", {})
        return (info.get("display_name", "🇲🇲 Burmese"), "MY")
    if re.search(r"[\u0370-\u03FF]", t):
        info = DB_LANGUAGES_CACHE.get("greek", {})
        return (info.get("display_name", "🇬🇷 Greek"), "EL")

    if re.search(r"[\u0400-\u04FF]", t):
        low_raw = t.lower()
        ukr_chars = ["є", "ї", "ґ", "\u0454", "\u0457", "\u0491"]
        ukr_words = ["підтвердження", "безпеки", "не діліться", "входу"]
        if any(c in t for c in ukr_chars) or any(w in low_raw for w in ukr_words):
            info = DB_LANGUAGES_CACHE.get("ukrainian", {})
            return (info.get("display_name", "🇺🇦 Ukrainian"), "UK")
        if any(w in low_raw for w in ["за потвърждение", "вашият", "сигурност", "кодът", "споделяйте", "с никого"]):
            info = DB_LANGUAGES_CACHE.get("bulgarian", {})
            return (info.get("display_name", "🇧🇬 Bulgarian"), "BG")
        if "кодыңыз" in low_raw or "үшін" in low_raw or "растау" in low_raw:
            info = DB_LANGUAGES_CACHE.get("kazakh", {})
            return (info.get("display_name", "🇰🇿 Kazakh"), "KK")
        info = DB_LANGUAGES_CACHE.get("russian", {})
        return (info.get("display_name", "🇷🇺 Russian"), "RU")

    if re.search(r"[\u3040-\u30FF]", t):
        info = DB_LANGUAGES_CACHE.get("japanese", {})
        return (info.get("display_name", "🇯🇵 Japanese"), "JA")
    if re.search(r"[\uAC00-\uD7AF\u1100-\u11FF]", t):
        info = DB_LANGUAGES_CACHE.get("korean", {})
        return (info.get("display_name", "🇰🇷 Korean"), "KO")
    if re.search(r"[\u4E00-\u9FFF]", t):
        info = DB_LANGUAGES_CACHE.get("chinese", {})
        return (info.get("display_name", "🇨🇳 Chinese"), "ZH")

    # 2. Database Phrase Scoring
    norm = normalize_text_for_lang(t)
    scores: Dict[str, int] = {}
    for lid, info in DB_LANGUAGES_CACHE.items():
        if not info.get("is_enabled", True):
            continue
        score = 0
        for phrase, weight in info.get("phrases", {}).items():
            pattern = r"(?:\b|^)" + re.escape(phrase) + r"(?:\b|$)"
            matches = len(re.findall(pattern, norm))
            score += matches * weight
        if score > 0:
            scores[lid] = score

    if scores:
        best_lid = max(scores.items(), key=lambda x: x[1])[0]
        norm_words = norm.split()
        if any(w in norm_words for w in ("seu", "sua", "voce")) or "nao compartilhe" in norm:
            if "portuguese" in scores:
                best_lid = "portuguese"
        elif any(w in norm_words for w in ("tu", "nadie")) or "no compartas" in norm:
            if "spanish" in scores:
                best_lid = "spanish"
        elif any(w in norm_words for w in ("votre", "vos", "veuillez")) or "ne partagez" in norm:
            if "french" in scores:
                best_lid = "french"

        info = DB_LANGUAGES_CACHE[best_lid]
        return (info["display_name"], info["iso"])

    eng_info = DB_LANGUAGES_CACHE.get("english", {})
    return (eng_info.get("display_name", "🇬🇧 English"), "EN")

def format_otp_notification(item: Dict[str, Any], sms_format: Optional[str] = None) -> tuple:
    """Returns (text, otp_code, raw_message) with professional header/divider layout."""
    if not sms_format:
        try:
            sms_format = load_stored_data().get("sms_format", "short")
        except Exception:
            sms_format = "short"
    sms_format = str(sms_format).lower().strip()

    source        = html.escape(str(item.get("sourceAddress") or item.get("source") or item.get("caller") or item.get("sender") or "SMS Service").strip())
    raw_number    = str(item.get("destinationNumber") or item.get("number") or item.get("rangeTemplate") or item.get("template") or item.get("dst") or "")
    masked_number = html.escape(mask_phone_number(raw_number)) if raw_number else ""
    raw_message   = str(item.get("messageBody") or item.get("message") or item.get("text") or item.get("body") or item.get("otp") or "")
    otp_code      = extract_otp_code(raw_message)

    country_iso   = get_country_iso_display(item).strip()
    parts = country_iso.split(maxsplit=1)
    flag = parts[0] if parts else "🌐"
    iso  = parts[1] if len(parts) > 1 else "XX"

    country_name = get_country_full_name(iso, item)
    lang_name, lang_code = detect_sms_language(raw_message)

    DIVIDER = "━━━━━━━━━━━━━━━━━━━━"

    header = "⚡ <b>NEW SMS RECEIVED</b> ⚡"

    lines = [header, DIVIDER]

    if masked_number:
        lines.append(f"• <b>Number:</b> {flag} <code>{masked_number}</code>")
    else:
        lines.append(f"• <b>Country:</b> {flag} <code>{country_name} ({iso})</code>")

    # Check if WhatsApp service - either by source OR message text content
    low_source = source.lower()
    low_msg = raw_message.lower()
    wa_keywords = ["whatsapp", "‏واتساب‏", "واتساب", "ватсап", "wa code", "wa.me"]
    is_wa = "whatsapp" in low_source or any(k in low_msg for k in wa_keywords)
    if is_wa and "whatsapp" not in low_source:
        source = "WhatsApp"

    lines.append(f"• <b>Service:</b> <code>{source}</code>")

    if masked_number:
        lines.append(f"• <b>Country:</b> <code>{country_name} ({iso})</code>")
    if load_stored_data().get("show_sms_language", True):
        lines.append(f"• <b>Language:</b> <code>{lang_name}</code>")

    # Message Content block:
    # In 'long' format (or when no OTP code): display full raw real SMS formatted cleanly in monospace
    # In 'short' format with OTP: hide message content block completely
    clean_msg = raw_message.strip()
    if clean_msg and (not otp_code or sms_format == "long"):
        lines.append("")
        lines.append("💬 <b>Message Content:</b>")
        lines.append(f"<code>{html.escape(clean_msg, quote=False)}</code>")

    lines.append(DIVIDER)

    return "\n".join(lines), otp_code, raw_message

# ==========================================
# 9. Telegram Bot Engine
# ==========================================
def get_effective_base_url() -> str:
    data = load_stored_data()
    stored = data.get("thirdwave_base_url") or data.get("base_url")
    if stored and str(stored).strip():
        raw = str(stored).strip()
    else:
        raw = THIRDWAVE_BASE_URL
    raw = raw.rstrip("/")
    if raw and not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    return raw

total_forwarded_count: int = 0
country_forwarded_counts: Dict[str, int] = {}  # tracks per-country OTP count (ISO display string -> count)
client = ThirdwaveClient(
    base_url=get_effective_base_url(),
    api_key=THIRDWAVE_API_KEY,
    timeout=20.0,
)

async def send_with_retry(bot: Bot, chat_id: int, text: str, max_retries: int = 3,
                          reply_markup=None) -> bool:
    for attempt in range(1, max_retries + 1):
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                read_timeout=30.0,
                write_timeout=30.0,
                connect_timeout=30.0,
            )
            return True
        except RetryAfter as r_err:
            wait_time = r_err.retry_after + 1
            logger.warning(f"Telegram rate-limit. Waiting {wait_time}s...")
            await asyncio.sleep(wait_time)
        except (TimedOut, NetworkError) as net_err:
            logger.warning(f"Network error sending to {chat_id}: {net_err}. Retry {attempt}/{max_retries}...")
            await asyncio.sleep(3.0)
        except Exception as e:
            logger.error(f"❌ Cannot send to {chat_id}: {e}")
            return False
    return False

def _get_otp_dest_ids() -> Set[int]:
    """Returns the set of chat IDs to deliver OTPs to (ALL configured Groups)."""
    dest: Set[int] = set()
    for gid in get_target_group_chat_ids():
        if gid:
            dest.add(gid)
    return dest

async def _deliver_item(bot: Bot, item: Dict[str, Any], dest_ids: Set[int]) -> bool:
    """Formats and sends one OTP item to the Telegram Group. Returns True if sent successfully."""
    mid        = str(item.get("id") or "").strip()
    data       = load_stored_data()
    sms_format = data.get("sms_format", "short")
    formatted_text, otp, raw_msg = format_otp_notification(item, sms_format=sms_format)
    sent_to_any = False

    # Build inline buttons: OTP code button + optional Admin Custom Link button
    markup = None
    try:
        buttons = []
        if otp:
            buttons.append([InlineKeyboardButton(otp, copy_text=CopyTextButton(text=otp))])

        link_cfg = data.get("custom_link_button")
        if isinstance(link_cfg, dict) and link_cfg.get("text") and link_cfg.get("url"):
            b_text = str(link_cfg["text"]).strip()
            b_url  = str(link_cfg["url"]).strip()
            if b_url.startswith(("http://", "https://", "tg://")):
                buttons.append([InlineKeyboardButton(b_text, url=b_url)])

        if buttons:
            markup = InlineKeyboardMarkup(buttons)
    except Exception:
        markup = None

    for cid in dest_ids:
        try:
            ok = await send_with_retry(bot, cid, formatted_text, reply_markup=markup)
            if ok:
                sent_to_any = True
        except Exception as e:
            logger.warning(f"Delivery error to {cid}: {e}")

    if sent_to_any:
        country  = extract_country_name(str(item.get("rangeName") or item.get("destinationName") or ""))
        raw_num  = str(item.get("destinationNumber") or item.get("number") or item.get("rangeTemplate") or item.get("template") or item.get("dst") or "")
        num      = mask_phone_number(raw_num)
        raw_msg  = str(item.get("messageBody") or item.get("message") or item.get("text") or item.get("body") or item.get("otp") or "")
        otp      = extract_otp_code(raw_msg)
        gid      = get_linked_group_chat_id()
        save_processed_message(item, gid or 0, country, num, otp)
        country_iso = get_country_iso_display(item)
        country_forwarded_counts[country_iso] = country_forwarded_counts.get(country_iso, 0) + 1
        logger.info(f"✅ Forwarded Live OTP (ID: {mid}) to Group [{country_iso}]")
    else:
        logger.error(f"❌ Failed to deliver OTP (ID: {mid})")

    return sent_to_any

async def poll_incoming_messages(application: Application):
    global total_forwarded_count, _gist_dirty, _is_handover, _handover_epoch
    init_db()
    bot_start_time = datetime.now(timezone.utc).timestamp()
    logger.info(f"🚀 OTP WAVE polling engine started at epoch {bot_start_time:.0f}.")

    # 1. Ensure Gist exists and load continuous state
    if gist_storage.enabled:
        await gist_storage.ensure_gist()
        gist_state = await gist_storage.load_state()
        gist_seen = gist_state.get("seen", {})
        for k, ts in gist_seen.items():
            seen_message_ids.add(k)
            seen_timestamps[k] = ts

        # Check if previous session performed a clean zero-restart handover
        if gist_state.get("handover"):
            _is_handover = True
            _handover_epoch = float(gist_state.get("handover_epoch") or 0.0)
            saved_total = int(gist_state.get("total_forwarded") or 0)
            if saved_total > total_forwarded_count:
                total_forwarded_count = saved_total
            saved_countries = gist_state.get("country_counts", {})
            if isinstance(saved_countries, dict):
                for c_iso, c_cnt in saved_countries.items():
                    country_forwarded_counts[c_iso] = max(country_forwarded_counts.get(c_iso, 0), int(c_cnt))
            saved_base = gist_state.get("base_url")
            if saved_base and saved_base != client.base_url:
                client.update_base_url(saved_base)

            logger.info(
                f"🔄 Zero-Restart Handover Active: {len(gist_seen)} messages restored, "
                f"{total_forwarded_count} forwarded previously, last epoch {_handover_epoch:.0f}."
            )

        # Restore bot configuration settings secretly from cloud database
        saved_bot_data = gist_state.get("bot_data")
        if isinstance(saved_bot_data, dict) and saved_bot_data:
            local_data = load_stored_data()
            local_data.update(saved_bot_data)
            save_stored_data(local_data)
            custom_name = local_data.get("bot_name")
            if custom_name:
                try:
                    await application.bot.set_my_name(name=custom_name)
                    logger.info(f"🤖 Applied custom bot name from secret database: {custom_name}")
                except Exception:
                    pass

        # Populate local SQLite DB from cloud memory so database is never empty on restarts
        try:
            with get_db_connection() as conn:
                for k in gist_seen.keys():
                    conn.execute("INSERT OR IGNORE INTO processed_otps (id) VALUES (?);", (k,))
                conn.commit()
        except Exception as e:
            logger.warning(f"DB sync from Gist warning: {e}")
        logger.info(f"☁️ Restored {len(gist_seen)} persistent message IDs from GitHub Gist.")

    # 2. Preload seen IDs from local DB
    try:
        with get_db_connection() as conn:
            for row in conn.execute("SELECT id FROM processed_otps ORDER BY forwarded_at DESC LIMIT 10000;"):
                mid = str(row["id"])
                seen_message_ids.add(mid)
                if mid not in seen_timestamps:
                    seen_timestamps[mid] = bot_start_time
        logger.info(f"Preloaded {len(seen_message_ids)} total seen message keys from persistent database.")
    except Exception as e:
        logger.warning(f"Preload error: {e}")

    # 3. Startup pass: baseline history or catch handover transition gap OTPs
    try:
        initial_msgs = await client.fetch_incoming_messages()
        dest_ids = _get_otp_dest_ids()
        baselined = 0
        forwarded_handover_gap = 0
        now_ts = datetime.now(timezone.utc).timestamp()

        for item in reversed(initial_msgs):  # oldest first
            mid = str(item.get("id") or "").strip()
            if not mid:
                continue
            if is_message_seen(mid):
                continue

            # If this is a handover continuation, forward any OTP received during the runner switch gap!
            if _is_handover and _handover_epoch > 0:
                msg_ts = parse_message_timestamp(str(item.get("receivedAt") or item.get("messageTime") or item.get("createdAt") or ""))
                # If received around or after handover (within 60s buffer) and within last 5 minutes
                if ((msg_ts > 0 and msg_ts >= (_handover_epoch - 60.0)) or (now_ts - msg_ts < 300.0 and msg_ts > 0)):
                    if dest_ids:
                        ok = await _deliver_item(application.bot, item, dest_ids)
                        if ok:
                            total_forwarded_count += 1
                            forwarded_handover_gap += 1
                            seen_message_ids.add(mid)
                            seen_timestamps[mid] = now_ts
                            _gist_dirty = True
                            continue

            # Otherwise, baseline historical message without forwarding
            seen_message_ids.add(mid)
            seen_timestamps[mid] = bot_start_time
            country = extract_country_name(str(item.get("rangeName") or item.get("destinationName") or ""))
            raw_num = str(item.get("destinationNumber") or item.get("number") or item.get("rangeTemplate") or item.get("dst") or "")
            num     = mask_phone_number(raw_num)
            raw_msg = str(item.get("messageBody") or item.get("message") or item.get("text") or item.get("body") or item.get("otp") or "")
            otp     = extract_otp_code(raw_msg)
            gid     = get_linked_group_chat_id()
            save_processed_message(item, gid or 0, country, num, otp)
            baselined += 1

        if forwarded_handover_gap > 0:
            logger.info(f"⚡ Zero-Downtime Handover: Delivered {forwarded_handover_gap} OTP(s) received during runner switch gap!")
        if baselined > 0 and gist_storage.enabled:
            await gist_storage.save_state(
                seen_dict=seen_timestamps,
                is_handover=False,
                total_forwarded=total_forwarded_count,
                country_counts=country_forwarded_counts,
                base_url=client.base_url,
            )
        logger.info(f"✅ Startup pass complete: {baselined} historical messages baselined (0 old messages forwarded).")
    except Exception as e:
        logger.warning(f"Startup pass error: {e}")

    # ── LIVE POLLING LOOP ── runs forever, never crashes ──────────────────────
    while True:
        try:
            dest_ids = _get_otp_dest_ids()
            if dest_ids:
                messages  = await client.fetch_incoming_messages()
                new_items = []
                for m in messages:
                    mid = str(m.get("id") or "").strip()
                    if not mid or is_message_seen(mid):
                        continue
                    # Timestamp check: ignore any SMS older than bot startup time
                    msg_ts = parse_message_timestamp(str(m.get("receivedAt") or m.get("messageTime") or m.get("createdAt") or ""))
                    if msg_ts > 0 and msg_ts < (bot_start_time - 15.0):
                        seen_message_ids.add(mid)
                        seen_timestamps[mid] = msg_ts
                        continue
                    new_items.append(m)
                    seen_message_ids.add(mid)
                    seen_timestamps[mid] = datetime.now(timezone.utc).timestamp()
                    _gist_dirty = True

                if new_items:
                    logger.info(f"🔔 {len(new_items)} new SMS/OTP(s) detected!")
                    for item in reversed(new_items):  # oldest first
                        ok = await _deliver_item(application.bot, item, dest_ids)
                        if ok:
                            total_forwarded_count += 1
                        # Small delay so each OTP is sent as a clearly separate new message
                        await asyncio.sleep(1.0)

        except (TimedOut, NetworkError) as net_err:
            logger.warning(f"⚠️ Network hiccup: {net_err}. Retrying in 3s...")
            await asyncio.sleep(3.0)
        except Exception as e:
            logger.error(f"⚠️ Polling error: {e}. Continuing in 3s...")
            await asyncio.sleep(3.0)

        await asyncio.sleep(POLL_INTERVAL_SECONDS)

# ==========================================
# 10. Bot Commands
# ==========================================
def build_status_dashboard() -> Tuple[str, InlineKeyboardMarkup]:
    group_ids = get_target_group_chat_ids()
    db_count  = get_total_processed_count()
    group_text = f"{len(group_ids)} Linked Groups ✅" if len(group_ids) > 1 else ("Linked ✅" if group_ids else "Not Linked ⚠️")

    # Uptime calculation
    uptime_secs = int(time.time() - bot_process_start_time)
    hours, rem = divmod(uptime_secs, 3600)
    mins, secs = divmod(rem, 60)
    uptime_str = f"{hours}h {mins}m {secs}s" if hours else f"{mins}m {secs}s"

    # Handover countdown
    session_timeout = int(os.getenv("SESSION_TIMEOUT", "0"))
    if session_timeout > 0:
        handover_secs = max(0, session_timeout - uptime_secs)
        h_hours, h_rem = divmod(handover_secs, 3600)
        h_mins, _ = divmod(h_rem, 60)
        handover_info = f"<code>{h_hours}h {h_mins}m remaining</code> (Auto-Sync 🔄)"
    else:
        handover_info = "<code>Always-Online (Continuous)</code>"

    # Format & Link button status
    data = load_stored_data()
    active_bot_name = data.get("bot_name", "OTP Wave Bot")
    active_format = data.get("sms_format", "short")
    fmt_label = "Short (Modern ⚡)" if active_format == "short" else "Long (Detailed 📜)"

    link_cfg = data.get("custom_link_button")
    if isinstance(link_cfg, dict) and link_cfg.get("text"):
        link_str = f"<code>{html.escape(str(link_cfg['text']))}</code> ({html.escape(str(link_cfg.get('url','')))})"
    else:
        link_str = "<i>None configured</i>"

    # Build per-country breakdown (sorted by count desc, top 15)
    country_lines = ""
    if country_forwarded_counts:
        sorted_countries = sorted(country_forwarded_counts.items(), key=lambda x: x[1], reverse=True)
        country_lines = "\n🌍 <b>OTPs by Country (Cumulative):</b>\n"
        for idx, (iso_display, cnt) in enumerate(sorted_countries[:15], 1):
            country_lines += f"  {idx}. {iso_display} — <code>{cnt}</code>\n"
        country_lines += "━━━━━━━━━━━━━━━━━━━━\n"

    gist_status = f"Encrypted Cloud DB ({GIST_ID[:8]}...) 🔒" if gist_storage.enabled else "Local Storage"
    msg = (
        f"⚡ <b>{active_bot_name} 24/7 (Zero-Restart Engine)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Engine Status:</b> <code>100% Online & Forwarding ✅</code>\n"
        f"• <b>Bot Name:</b> <code>{active_bot_name}</code> (/setname)\n"
        f"• <b>Handover Mode:</b> <code>Zero-Restart Handover Active 🔄</code>\n"
        f"• <b>Session Uptime:</b> <code>{uptime_str}</code>\n"
        f"• <b>Next Handover:</b> {handover_info}\n"
        f"• <b>Platform:</b> <code>Thirdwave IPRN</code>\n"
        f"• <b>API Base:</b> <code>{client.base_url}</code>\n"
        f"• <b>Database:</b> <code>{gist_status}</code>\n"
        f"• <b>Target Groups:</b> <code>{group_text}</code>\n"
        f"• <b>OTPs Forwarded:</b> <code>{total_forwarded_count}</code> <i>(accumulated)</i>\n"
        f"• <b>Lifetime DB:</b> <code>{db_count} total records</code>\n"
        f"• <b>Poll Speed:</b> <code>{POLL_INTERVAL_SECONDS}s</code>\n"
        f"• <b>SMS Format:</b> <code>{fmt_label}</code> (/toggleformat)\n"
        f"• <b>SMS Language:</b> <code>{lang_status_str}</code> (/togglelang)\n"
        f"• <b>Link Button:</b> {link_str} (/setlink)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{country_lines}"
        f"🔔 <i>Real-time seamless forwarding active. Zero restart alerts.</i>"
    )

    next_fmt_label = "Long (Detailed 📜)" if active_format == "short" else "Short (Modern ⚡)"
    show_lang = data.get("show_sms_language", True)
    lang_status_str = "ON ✅" if show_lang else "OFF ❌"

    keyboard = [
        [InlineKeyboardButton(f"📋 Toggle Format ({active_format.upper()})", callback_data="toggle_format"),
         InlineKeyboardButton(f"🌐 SMS Lang: {lang_status_str}", callback_data="toggle_sms_lang")],
        [InlineKeyboardButton("✏️ Change Bot Name", callback_data="bot_name_help"),
         InlineKeyboardButton("🔗 Link Button", callback_data="link_help")],
        [InlineKeyboardButton("🌐 Language Settings", callback_data="lang_mgr_p_0"), InlineKeyboardButton("🔄 Refresh", callback_data="refresh_dash")]
    ]
    return msg, InlineKeyboardMarkup(keyboard)

async def sync_data_to_secret_db():
    """Immediately sync all persistent state and settings secretly to the cloud database."""
    if "gist_storage" in globals() and gist_storage.enabled:
        try:
            await gist_storage.save_state(
                seen_dict=seen_timestamps,
                is_handover=_is_handover,
                total_forwarded=total_forwarded_count,
                country_counts=country_forwarded_counts,
                base_url=client.base_url if "client" in globals() else "",
            )
            logger.info("🔒 Bot state and settings securely synced to cloud database.")
        except Exception as e:
            logger.debug(f"Cloud DB sync notice: {e}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat    = update.effective_chat
    user    = update.effective_user
    msg_obj = update.effective_message
    if not chat or not msg_obj:
        return
    user_id = user.id if user else 0
    if not is_user_authorized(user_id):
        await msg_obj.reply_text("⛔ <b>Access Restricted</b>: Admins only.", parse_mode=ParseMode.HTML)
        return
    msg, markup = build_status_dashboard()
    await msg_obj.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=markup)

async def toggleformat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat    = update.effective_chat
    user    = update.effective_user
    msg_obj = update.effective_message
    if not chat or not msg_obj:
        return
    user_id = user.id if user else 0
    if not is_user_authorized(user_id):
        await msg_obj.reply_text("⛔ <b>Access Restricted</b>: Admins only.", parse_mode=ParseMode.HTML)
        return

    data = load_stored_data()
    curr = data.get("sms_format", "short")

    arg = context.args[0].strip().lower() if (context and context.args) else ""
    if arg in ("short", "long"):
        new_fmt = arg
    else:
        new_fmt = "long" if curr == "short" else "short"

    data["sms_format"] = new_fmt
    save_stored_data(data)
    asyncio.create_task(sync_data_to_secret_db())

    mode_label = "Long (Detailed with Full SMS 📜)" if new_fmt == "long" else "Short (Modern & Clean ⚡)"
    await msg_obj.reply_text(
        f"✅ <b>SMS Notification Format Updated!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Active Mode:</b> <code>{mode_label}</code>\n"
        f"• <b>Format Setting:</b> <code>{new_fmt}</code>\n"
        f"• <b>Database:</b> <code>Saved Secretly to Cloud Store 🔒</code>\n\n"
        f"<i>Tip: Send <code>/format short</code> or <code>/format long</code> or <code>/toggleformat</code> anytime.</i>",
        parse_mode=ParseMode.HTML
    )

async def setname_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat    = update.effective_chat
    user    = update.effective_user
    msg_obj = update.effective_message
    if not chat or not msg_obj:
        return
    user_id = user.id if user else 0
    if not is_user_authorized(user_id):
        await msg_obj.reply_text("⛔ <b>Access Restricted</b>: Admins only.", parse_mode=ParseMode.HTML)
        return

    new_name = " ".join(context.args).strip() if context.args else ""
    if not new_name:
        await msg_obj.reply_text(
            "✏️ <b>Change Bot Name:</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Usage: <code>/setname Your New Bot Name</code>\n\n"
            "<b>Example:</b>\n"
            "<code>/setname OTP Wave VIP</code>\n"
            "<code>/setname Thirdwave OTP Hub</code>\n\n"
            "• Changes the bot's official Telegram name\n"
            "• Persists secretly in the cloud database\n"
            "• To reset to default, use <code>/resetname</code>",
            parse_mode=ParseMode.HTML
        )
        return

    if len(new_name) > 64:
        await msg_obj.reply_text("❌ Bot name must be 64 characters or fewer.", parse_mode=ParseMode.HTML)
        return

    try:
        await context.bot.set_my_name(name=new_name)
    except Exception as e:
        logger.warning(f"Failed to set Telegram bot name: {e}")

    data = load_stored_data()
    data["bot_name"] = new_name
    save_stored_data(data)
    asyncio.create_task(sync_data_to_secret_db())

    await msg_obj.reply_text(
        f"✅ <b>Bot Name Successfully Updated!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>New Name:</b> <code>{html.escape(new_name)}</code>\n"
        f"• <b>Telegram API:</b> <code>Applied Officially ✅</code>\n"
        f"• <b>Database:</b> <code>Saved Secretly to Cloud Store 🔒</code>",
        parse_mode=ParseMode.HTML
    )

async def resetname_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat    = update.effective_chat
    user    = update.effective_user
    msg_obj = update.effective_message
    if not chat or not msg_obj:
        return
    user_id = user.id if user else 0
    if not is_user_authorized(user_id):
        await msg_obj.reply_text("⛔ <b>Access Restricted</b>: Admins only.", parse_mode=ParseMode.HTML)
        return

    DEFAULT_NAME = "OTP Wave Bot"
    try:
        await context.bot.set_my_name(name=DEFAULT_NAME)
    except Exception as e:
        logger.warning(f"Reset bot name warning: {e}")

    data = load_stored_data()
    if "bot_name" in data:
        del data["bot_name"]
        save_stored_data(data)
        asyncio.create_task(sync_data_to_secret_db())

    await msg_obj.reply_text(
        f"✅ <b>Bot Name Reset to Default!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Active Name:</b> <code>{DEFAULT_NAME}</code>\n"
        f"• <b>Database:</b> <code>Secret Cloud Store Synced 🔒</code>",
        parse_mode=ParseMode.HTML
    )

async def setlink_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat    = update.effective_chat
    user    = update.effective_user
    msg_obj = update.effective_message
    if not chat or not msg_obj:
        return
    user_id = user.id if user else 0
    if not is_user_authorized(user_id):
        await msg_obj.reply_text("⛔ <b>Access Restricted</b>: Admins only.", parse_mode=ParseMode.HTML)
        return

    raw_args = " ".join(context.args).strip() if context.args else ""
    if not raw_args or "|" not in raw_args:
        await msg_obj.reply_text(
            "🔗 <b>How to set a Custom Link Button:</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Use the command with <code>Button Name | URL</code>:\n\n"
            "<b>Example:</b>\n"
            "<code>/setlink 📢 Join Our Channel | https://t.me/mychannel</code>\n"
            "<code>/setlink 👥 Support Group | https://t.me/mysupport</code>\n"
            "<code>/setlink 🤖 Official Bot | https://t.me/mybot</code>\n\n"
            "To remove the button at any time, run <code>/removelink</code>.",
            parse_mode=ParseMode.HTML
        )
        return

    parts = raw_args.split("|", 1)
    btn_text = parts[0].strip()
    btn_url  = parts[1].strip()

    if not btn_text:
        await msg_obj.reply_text("❌ Button text cannot be empty.", parse_mode=ParseMode.HTML)
        return
    if not btn_url.startswith(("http://", "https://", "tg://")):
        await msg_obj.reply_text("❌ URL must start with <code>https://</code>, <code>http://</code>, or <code>tg://</code>.", parse_mode=ParseMode.HTML)
        return

    data = load_stored_data()
    data["custom_link_button"] = {"text": btn_text, "url": btn_url}
    save_stored_data(data)
    asyncio.create_task(sync_data_to_secret_db())

    await msg_obj.reply_text(
        f"✅ <b>Custom Link Button Configured!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Button Label:</b> <code>{html.escape(btn_text)}</code>\n"
        f"• <b>Target URL:</b> <code>{html.escape(btn_url)}</code>\n"
        f"• <b>Database:</b> <code>Saved Secretly to Cloud Store 🔒</code>\n\n"
        f"Every forwarded SMS will now include this button.",
        parse_mode=ParseMode.HTML
    )

async def removelink_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat    = update.effective_chat
    user    = update.effective_user
    msg_obj = update.effective_message
    if not chat or not msg_obj:
        return
    user_id = user.id if user else 0
    if not is_user_authorized(user_id):
        await msg_obj.reply_text("⛔ <b>Access Restricted</b>: Admins only.", parse_mode=ParseMode.HTML)
        return

    data = load_stored_data()
    if "custom_link_button" in data:
        del data["custom_link_button"]
        save_stored_data(data)
        asyncio.create_task(sync_data_to_secret_db())
        await msg_obj.reply_text("✅ <b>Custom link button removed.</b> SMS messages will no longer show the extra button.", parse_mode=ParseMode.HTML)
    else:
        await msg_obj.reply_text("ℹ️ No custom link button is currently set.", parse_mode=ParseMode.HTML)

async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    user_id = query.from_user.id if query.from_user else 0
    if not is_user_authorized(user_id):
        await query.answer("⛔ Access Restricted: Admins only.", show_alert=True)
        return

    if query.data.startswith("lang_"):
        cdata = query.data
        if cdata.startswith("lang_mgr_p_"):
            page = int(cdata.split("_")[-1])
            text, markup = build_languages_menu(page)
            try:
                await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            except Exception:
                pass
            await query.answer()
        elif cdata.startswith("lang_v_"):
            lid = cdata[7:]
            text, markup = build_language_detail_menu(lid)
            try:
                await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            except Exception:
                pass
            await query.answer()
        elif cdata.startswith("lang_edname_"):
            lid = cdata[12:]
            ADMIN_LANG_STATES[user_id] = {"action": "awaiting_edit_display_name", "lang_id": lid}
            cancel_markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"lang_v_{lid}")]])
            await query.message.reply_text(
                f"✏️ <b>Enter the new SMS Display Label for <code>{lid.title()}</code>:</b>\n\n"
                f"<i>Example:</i> <code>🇧🇬 Bulgarian [BG]</code> or <code>Bulgarian</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=cancel_markup
            )
            await query.answer()
        elif cdata.startswith("lang_addphr_"):
            lid = cdata[12:]
            ADMIN_LANG_STATES[user_id] = {"action": "awaiting_add_phrase", "lang_id": lid}
            cancel_markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"lang_v_{lid}")]])
            await query.message.reply_text(
                f"➕ <b>Add SMS Content / Phrase for <code>{lid.title()}</code>:</b>\n\n"
                f"Paste a full SMS message or specific security phrases received in this language.\n"
                f"<i>Example:</i>\n"
                f"<code>&lt;#&gt; Kodyt vi v WhatsApp: 879-885\nNe spodelyayte koda s nikogo</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=cancel_markup
            )
            await query.answer()
        elif cdata.startswith("lang_clrphr_"):
            lid = cdata[12:]
            db_clear_phrases(lid)
            asyncio.create_task(sync_data_to_secret_db())
            text, markup = build_language_detail_menu(lid)
            try:
                await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            except Exception:
                pass
            await query.answer("All custom phrases cleared ✅", show_alert=True)
        elif cdata.startswith("lang_toggle_sms_"):
            page_str = cdata.replace("lang_toggle_sms_", "")
            page = int(page_str) if page_str.isdigit() else 0
            data = load_stored_data()
            curr_val = data.get("show_sms_language", True)
            new_val = not curr_val
            data["show_sms_language"] = new_val
            save_stored_data(data)
            asyncio.create_task(sync_data_to_secret_db())
            status_txt = "ON ✅ (Language will show on SMS)" if new_val else "OFF ❌ (Language hidden from SMS)"
            await query.answer(f"SMS Language is now {status_txt}")
            text, markup = build_languages_menu(page)
            try:
                await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            except Exception:
                pass
        elif cdata.startswith("lang_del_"):
            lid = cdata[9:]
            db_delete_language(lid)
            asyncio.create_task(sync_data_to_secret_db())
            text, markup = build_languages_menu(0)
            try:
                await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            except Exception:
                pass
            await query.answer(f"Language '{lid}' deleted ✅", show_alert=True)
        elif cdata.startswith("lang_tog_"):
            lid = cdata[9:]
            new_st = db_toggle_language(lid)
            asyncio.create_task(sync_data_to_secret_db())
            text, markup = build_language_detail_menu(lid)
            try:
                await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            except Exception:
                pass
            st_msg = "Language Enabled ✅" if new_st else "Language Disabled ⏸️"
            await query.answer(st_msg)
        elif cdata == "lang_add_new":
            ADMIN_LANG_STATES[user_id] = {"action": "awaiting_new_lang"}
            cancel_markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="lang_mgr_p_0")]])
            await query.message.reply_text(
                "➕ <b>Add New Language:</b>\n\n"
                "Send the <b>Language Name | ISO Code</b> in chat.\n\n"
                "<b>Example:</b>\n"
                "<code>Somali | SO</code>\n"
                "or\n"
                "<code>Kurdish | KU</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=cancel_markup
            )
            await query.answer()
        return

    if query.data == "toggle_sms_lang":
        data = load_stored_data()
        curr_val = data.get("show_sms_language", True)
        new_val = not curr_val
        data["show_sms_language"] = new_val
        save_stored_data(data)
        asyncio.create_task(sync_data_to_secret_db())
        status_txt = "ON ✅" if new_val else "OFF ❌"
        await query.answer(f"SMS Language: {status_txt}")
        try:
            msg_text, reply_markup = build_status_dashboard()
            await query.edit_message_text(text=msg_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        except Exception:
            pass
        return

    if query.data == "toggle_format":
        data = load_stored_data()
        curr = str(data.get("sms_format", "short")).lower().strip()
        new_fmt = "long" if curr == "short" else "short"
        data["sms_format"] = new_fmt
        save_stored_data(data)
        asyncio.create_task(sync_data_to_secret_db())

        mode_label = "Long (Detailed 📜)" if new_fmt == "long" else "Short (Modern ⚡)"
        try:
            await query.answer(f"Format switched to: {mode_label}", show_alert=False)
        except Exception:
            pass

        try:
            msg_text, reply_markup = build_status_dashboard()
            await query.edit_message_text(text=msg_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        except Exception as e:
            logger.warning(f"Could not edit dashboard message on toggle: {e}")
    elif query.data == "bot_name_help":
        await query.answer()
        await query.message.reply_text(
            "✏️ <b>Change Bot Name:</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "To change the bot display name, send:\n"
            "<code>/setname Your New Bot Name</code>\n\n"
            "<b>Example:</b>\n"
            "<code>/setname OTP Wave VIP</code>\n\n"
            "To reset to default, send <code>/resetname</code>.",
            parse_mode=ParseMode.HTML
        )
    elif query.data == "link_help":
        await query.answer()
        await query.message.reply_text(
            "🔗 <b>Custom Link Button Guide:</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "• <b>To add/change button:</b>\n"
            "  <code>/setlink Button Name | https://yourlink.com</code>\n\n"
            "• <b>To delete button:</b>\n"
            "  <code>/removelink</code>\n\n"
            "The button will appear automatically on every forwarded SMS.",
            parse_mode=ParseMode.HTML
        )
    elif query.data == "refresh_dash":
        try:
            msg_text, reply_markup = build_status_dashboard()
            await query.edit_message_text(text=msg_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            await query.answer("Dashboard Refreshed 🔄")
        except Exception:
            await query.answer("Already up to date ✅")


async def togglelanguage_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg_obj = update.effective_message
    if not user or not msg_obj:
        return
    if not is_user_authorized(user.id):
        await msg_obj.reply_text("⛔ <b>Access Restricted</b>: Admins only.", parse_mode=ParseMode.HTML)
        return
    data = load_stored_data()
    curr_val = data.get("show_sms_language", True)
    new_val = not curr_val
    data["show_sms_language"] = new_val
    save_stored_data(data)
    asyncio.create_task(sync_data_to_secret_db())
    status_str = "🟢 <b>ON</b> (Language will be displayed on SMS)" if new_val else "🔴 <b>OFF</b> (Language hidden from SMS)"
    await msg_obj.reply_text(
        f"🌐 <b>SMS Language Display Setting</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Status:</b> {status_str}\n"
        f"• <b>SMS Card:</b> {'• Language: &lt;Detected Language&gt;' if new_val else '(Hidden)'}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 <i>Tap /togglelang again to switch anytime, or use the Admin Panel.</i>",
        parse_mode=ParseMode.HTML
    )

async def languages_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg_obj = update.effective_message
    if not user or not msg_obj:
        return
    if not is_user_authorized(user.id):
        await msg_obj.reply_text("⛔ <b>Access Restricted</b>: Admins only.", parse_mode=ParseMode.HTML)
        return
    text, markup = build_languages_menu(0)
    await msg_obj.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)

async def handle_admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg_obj = update.effective_message
    if not user or not msg_obj:
        return
    if not is_user_authorized(user.id):
        return

    state = ADMIN_LANG_STATES.get(user.id)
    if not state:
        return

    action = state.get("action")
    raw_text = msg_obj.text.strip() if msg_obj.text else ""
    if not raw_text:
        return

    if action == "awaiting_new_lang":
        del ADMIN_LANG_STATES[user.id]
        parts = [p.strip() for p in raw_text.split("|") if p.strip()]
        name = parts[0]
        iso = parts[1] if len(parts) > 1 else "XX"
        db_add_language(name, iso)
        asyncio.create_task(sync_data_to_secret_db())
        lid = name.lower()
        detail_text, detail_markup = build_language_detail_menu(lid)
        await msg_obj.reply_text(
            f"✅ <b>New Language '{name} ({iso.upper()})' Added!</b>\n\n" + detail_text,
            parse_mode=ParseMode.HTML,
            reply_markup=detail_markup
        )
    elif action == "awaiting_edit_display_name":
        del ADMIN_LANG_STATES[user.id]
        lid = state.get("lang_id")
        if lid:
            db_edit_display_name(lid, raw_text)
            asyncio.create_task(sync_data_to_secret_db())
            detail_text, detail_markup = build_language_detail_menu(lid)
            await msg_obj.reply_text(
                f"✅ <b>Display Name Updated to '{raw_text}'!</b>\n\n" + detail_text,
                parse_mode=ParseMode.HTML,
                reply_markup=detail_markup
            )
    elif action == "awaiting_add_phrase":
        del ADMIN_LANG_STATES[user.id]
        lid = state.get("lang_id")
        if lid:
            lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
            for line in lines:
                db_add_phrase(lid, line)
            asyncio.create_task(sync_data_to_secret_db())
            detail_text, detail_markup = build_language_detail_menu(lid)
            await msg_obj.reply_text(
                f"✅ <b>Custom SMS phrase(s) saved into database!</b>\n\n" + detail_text,
                parse_mode=ParseMode.HTML,
                reply_markup=detail_markup
            )

async def seturl_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat    = update.effective_chat
    user    = update.effective_user
    msg_obj = update.effective_message
    if not chat or not msg_obj:
        return
    user_id = user.id if user else 0
    if not is_user_authorized(user_id):
        await msg_obj.reply_text("⛔ <b>Access Restricted</b>: Admins only.", parse_mode=ParseMode.HTML)
        return

    if not context.args:
        await msg_obj.reply_text(
            f"⚙️ <b>Thirdwave API Base URL Configuration</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Active URL:</b> <code>{client.base_url}</code>\n\n"
            f"<b>How to update in the future:</b>\n"
            f"1. <b>Instant Change (Telegram):</b>\n"
            f"   <code>/seturl &lt;new_url&gt;</code>\n"
            f"   <i>Example:</i> <code>/seturl https://clients.thirdwave.im</code>\n\n"
            f"2. <b>Permanent Cloud Hosting (GitHub Secrets):</b>\n"
            f"   Add or update the secret <code>THIRDWAVE_BASE_URL</code> in GitHub Repository Settings.",
            parse_mode=ParseMode.HTML
        )
        return

    raw_input = " ".join(context.args).strip()
    new_url = client.update_base_url(raw_input)

    # Persist in bot_data.json so it survives restarts
    data = load_stored_data()
    data["thirdwave_base_url"] = new_url
    save_stored_data(data)

    await msg_obj.reply_text(
        f"✅ <b>API Base URL Successfully Updated!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>New Active URL:</b> <code>{new_url}</code>\n\n"
        f"The bot is now requesting OTP traffic from this URL in real-time.",
        parse_mode=ParseMode.HTML
    )

async def send_startup_announcement(application: Application):
    """
    Sends startup notification to Admin private DM only on initial deployment or push.
    COMPLETELY SILENT on routine 24/7 handover sessions (zero restart messages).
    """
    global _is_handover
    # Suppress all restart notifications during automated handovers or silent mode
    silent_env = os.getenv("SILENT_STARTUP", "").strip().lower() in ("true", "1", "yes")
    if _is_handover or silent_env:
        logger.info("🤫 Automated session handover continuation: restart notification suppressed (zero-restart mode).")
        return

    # Only send alert on code push or if explicitly requested via secret/env
    admin_alert_enabled = os.getenv("ADMIN_STARTUP_ALERT", "false").strip().lower() in ("true", "1", "yes")
    if not admin_alert_enabled and STARTUP_TYPE != "push":
        logger.info("ℹ️ Bot started cleanly in silent 24/7 background mode (no admin spam).")
        return

    admin_msg = (
        "⚡ <b>OTP WAVE 24/7 ONLINE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "• <b>Status:</b> <code>Active & Monitoring Live OTPs ✅</code>\n"
        "• <b>Engine:</b> <code>Zero-Restart Handover Engine 🔄</code>\n"
        "• <b>Platform:</b> <code>Thirdwave IPRN</code>\n"
        "• <b>Storage:</b> <code>28h Memory Active ☁️</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👑 <i>Send /start or /status anytime to view live status.</i>"
    )
    for aid in ADMIN_USER_IDS:
        if aid:
            try:
                await send_with_retry(application.bot, aid, admin_msg)
                logger.info(f"✅ Initial alert sent to admin private chat {aid}")
            except Exception as e:
                logger.warning(f"Initial alert failed for admin {aid}: {e}")

async def periodic_db_cleanup_loop():
    while True:
        await asyncio.sleep(3600)  # every hour
        cleanup_old_messages(max_age_days=7)
        logger.info("🧹 Old OTP records cleaned up (>7 days).")

async def periodic_gist_sync_loop():
    global _gist_dirty
    while True:
        await asyncio.sleep(30.0)
        if _gist_dirty and gist_storage.enabled:
            _gist_dirty = False
            await gist_storage.save_state(
                seen_dict=seen_timestamps,
                is_handover=False,
                total_forwarded=total_forwarded_count,
                country_counts=country_forwarded_counts,
                base_url=client.base_url,
            )

# ==========================================
# 11. Diagnostics (--test mode)
# ==========================================
async def run_diagnostics():
    print("\n=======================================================")
    print("           OTP WAVE SYSTEM DIAGNOSTICS")
    print("=======================================================")

    # 1. Telegram bot
    print("[1/4] Checking Telegram Bot Token...")
    try:
        req = HTTPXRequest(connection_pool_size=4)
        bot = Bot(token=TELEGRAM_BOT_TOKEN, request=req)
        me  = await bot.get_me()
        print(f"  -> SUCCESS! Bot: @{me.username} (ID: {me.id})")
    except Exception as e:
        print(f"  -> FAILED: {e}")
        return

    # 2. Linked groups (Primary + Secondary)
    print("\n[2/4] Checking Linked Groups...")
    group_ids = get_target_group_chat_ids()
    if group_ids:
        for idx, gid in enumerate(group_ids, 1):
            try:
                chat = await bot.get_chat(gid)
                tag = "Primary" if idx == 1 else f"Secondary #{idx-1}"
                print(f"  -> SUCCESS! [{tag}] Group: '{chat.title}' (ID: {gid})")
            except Exception as e:
                print(f"  -> WARNING: Could not fetch group ID {gid} ({e})")
    else:
        print("  -> WARNING: No group chat IDs configured.")

    # 3. Thirdwave API
    print("\n[3/4] Checking Thirdwave API Connection...")
    init_db()
    try:
        msgs = await client.fetch_incoming_messages()
        print(f"  -> SUCCESS! Connected to {THIRDWAVE_BASE_URL}")
        print(f"  -> Total recent live messages fetched: {len(msgs)}")
        if msgs:
            m = msgs[0]
            print(f"  -> Latest SMS Sample: ID {m.get('id')} | Sender: {m.get('sourceAddress')} | Range: {m.get('rangeName')}")
            print(f"  -> Message Preview: \"{str(m.get('messageBody',''))[:80]}\"")
    except Exception as e:
        print(f"  -> FAILED: {e}")

    # 4. GitHub Gist
    print("\n[4/5] Checking GitHub Gist Cloud Storage (28h Retention)...")
    if gist_storage.enabled:
        try:
            await gist_storage.ensure_gist()
            data = await gist_storage.load_seen()
            print(f"  -> SUCCESS! Connected to GitHub Gist: '{gist_storage.gist_id}'")
            print(f"  -> Stored 28h seen message count: {len(data)}")
        except Exception as e:
            print(f"  -> WARNING: Gist check failed ({e})")
    else:
        print("  -> INFO: GitHub Gist storage not configured (using local SQLite database).")

    # 5. SQLite DB
    print("\n[5/5] Checking Local SQLite Database...")
    try:
        count = get_total_processed_count()
        print(f"  -> SUCCESS! Database connected: '{DB_FILE}'")
        print(f"  -> Total stored OTP records: {count}")
    except Exception as e:
        print(f"  -> FAILED: {e}")

    await bot.shutdown()
    print("\n=======================================================")
    print("    >>> ALL SYSTEMS ARE PROPERLY CONFIGURED! <<<")
    print("=======================================================\n")

# ==========================================
# 12. Main Entry Point
# ==========================================
def validate_config():
    errors = []
    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN is missing")
    if not THIRDWAVE_API_KEY:
        errors.append("THIRDWAVE_API_KEY is missing")
    if not TELEGRAM_GROUP_CHAT_ID and not ADMIN_USER_IDS:
        errors.append("Either TELEGRAM_GROUP_CHAT_ID or ADMIN_USER_IDS must be set")
    if not GIST_TOKEN:
        logger.info("ℹ️ GIST_TOKEN not set — bot running with local SQLite persistence & GitHub Actions cache.")
    if errors:
        print("\n❌ Configuration errors:")
        for err in errors:
            print(f"  - {err}")
        print("\nSet these in your .env file or GitHub Secrets.\n")
        sys.exit(1)

async def main():
    parser = argparse.ArgumentParser(description="OTP WAVE Bot")
    parser.add_argument("--test", action="store_true", help="Run diagnostics and exit")
    args = parser.parse_args()

    validate_config()

    if args.test:
        await run_diagnostics()
        return

    logger.info("⚡ OTP WAVE starting up...")

    req = HTTPXRequest(connection_pool_size=8)
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(req)
        .build()
    )
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", start_command))
    application.add_handler(CommandHandler("seturl", seturl_command))
    application.add_handler(CommandHandler("toggleformat", toggleformat_command))
    application.add_handler(CommandHandler("format", toggleformat_command))
    application.add_handler(CommandHandler("setformat", toggleformat_command))
    application.add_handler(CommandHandler("setname", setname_command))
    application.add_handler(CommandHandler("resetname", resetname_command))
    application.add_handler(CommandHandler("setlink", setlink_command))
    application.add_handler(CommandHandler("removelink", removelink_command))
    application.add_handler(CallbackQueryHandler(admin_callback_handler))

    application.add_handler(CommandHandler("togglelang", togglelanguage_command))
    application.add_handler(CommandHandler("togglelanguage", togglelanguage_command))
    application.add_handler(CommandHandler("languages", languages_command))
    application.add_handler(CommandHandler("lang", languages_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_text_input))


    def start_health_server():
        port_str = os.getenv("PORT")
        if not port_str:
            return
        try:
            from http.server import HTTPServer, BaseHTTPRequestHandler
            import threading

            class HealthHandler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.send_header("Content-type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"OK")

                def log_message(self, format, *args):
                    return

            port = int(port_str)
            server = HTTPServer(("0.0.0.0", port), HealthHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            logger.info(f"🌐 Cloud health check server active on port {port}")
        except Exception as e:
            logger.warning(f"Cloud health server notice: {e}")

    start_health_server()

    try:
        await application.initialize()
        await application.start()
        # Explicitly dispatch startup announcement right after initialization (silent on handover)
        asyncio.create_task(send_startup_announcement(application))
        asyncio.create_task(poll_incoming_messages(application))
        asyncio.create_task(periodic_db_cleanup_loop())
        asyncio.create_task(periodic_gist_sync_loop())
        try:
            await application.bot.set_my_commands([
                ("start",        "📊 Bot status & admin dashboard"),
                ("status",       "⚡ Live zero-restart engine status"),
                ("setname",      "✏️ Change bot display name"),
                ("resetname",    "🔄 Reset bot name to default"),
                ("toggleformat", "🔄 Toggle SMS format (Short / Long)"),
                ("setlink",      "🔗 Add custom link button on SMS"),
                ("removelink",   "🗑️ Remove custom link button"),
                ("seturl",       "🌐 View or update the Thirdwave API base URL"),
            ])
        except Exception:
            pass
        logger.info("✅ OTP WAVE is fully online and monitoring incoming messages...")

        # Robust polling starter with automatic conflict recovery
        for attempt in range(1, 6):
            try:
                await application.updater.start_polling(drop_pending_updates=True)
                break
            except Conflict:
                logger.warning(f"⚠️ Telegram conflict (previous session still releasing). Waiting 4s (attempt {attempt}/5)...")
                await asyncio.sleep(4.0)
            except Exception as poll_err:
                logger.warning(f"Telegram polling warning on attempt {attempt}: {poll_err}")
                await asyncio.sleep(3.0)

        # Resilient keepalive loop with scheduled zero-restart session handover
        start_time = time.time()
        session_timeout = int(os.getenv("SESSION_TIMEOUT", "0"))
        if session_timeout > 0:
            logger.info(f"⏱️ Zero-restart handover timer armed: {session_timeout}s ({session_timeout/3600:.2f}h)")

        while True:
            try:
                now = time.time()
                elapsed = now - start_time
                # 60s before session timeout: initiate pre-handover state snapshot & sync
                if session_timeout > 0 and elapsed >= (session_timeout - 60):
                    logger.info(f"⏱️ Session limit approaching ({elapsed:.0f}s elapsed of {session_timeout}s).")
                    logger.info("🔄 Pre-timeout analysis: saving workflow state to Gist & SQLite for seamless handover...")
                    try:
                        with get_db_connection() as conn:
                            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                    except Exception as e:
                        logger.warning(f"DB checkpoint notice: {e}")

                    if gist_storage.enabled:
                        await gist_storage.save_state(
                            seen_dict=seen_timestamps,
                            is_handover=True,
                            total_forwarded=total_forwarded_count,
                            country_counts=country_forwarded_counts,
                            base_url=client.base_url,
                        )
                    logger.info("✅ Pre-handover state saved. Exiting cleanly for next runner switch (exit 0)...")
                    break
                sleep_chunk = min(15, session_timeout) if session_timeout > 0 else 3600
                await asyncio.sleep(sleep_chunk)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Keepalive loop warning: {e}")
                await asyncio.sleep(5)
    finally:
        try:
            if application.updater and application.updater.running:
                await application.updater.stop()
            if application.running:
                await application.stop()
            await application.shutdown()
        except Exception:
            pass
        try:
            with get_db_connection() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception:
            pass
        if gist_storage.enabled:
            near_timeout = session_timeout > 0 and (time.time() - start_time) >= (session_timeout - 120)
            await gist_storage.save_state(
                seen_dict=seen_timestamps,
                is_handover=near_timeout or _is_handover,
                total_forwarded=total_forwarded_count,
                country_counts=country_forwarded_counts,
                base_url=client.base_url,
            )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except SystemExit as se:
        if se.code is not None and se.code != 0:
            logger.error(f"Bot exited with error code {se.code}")
            sys.exit(se.code)
        logger.info("Bot stopped cleanly.")
    except Exception as fatal_err:
        logger.error(f"Fatal error in bot main: {fatal_err}")
        sys.exit(2)


# Last updated: 2026-08-27 21:10
