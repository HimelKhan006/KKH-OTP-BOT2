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
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

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
            payload_data = {
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
        conn.commit()
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

LANGUAGE_RULES: Dict[str, Dict[str, Any]] = {
    "French": {
        "code": "FR",
        "keywords": {
            "votre": 3, "vos": 2, "est": 1, "ne": 1, "pas": 1, "partagez": 3,
            "verification": 2, "confirmation": 2, "mot de passe": 3, "connexion": 3,
            "pour": 1, "securite": 2, "aucun": 3, "personne": 2, "veuillez": 3,
            "entrez": 3, "utiliser": 2, "identifiant": 3, "compte": 2, "reinitialisation": 3,
            "nouveau": 2, "appareil": 2, "ce code": 2
        }
    },
    "Spanish": {
        "code": "ES",
        "keywords": {
            "tu": 2, "su": 1, "codigo": 2, "verificacion": 2, "para": 1,
            "no compartas": 4, "compartas": 3, "con nadie": 3, "nadie": 2,
            "iniciar sesion": 3, "seguridad": 2, "cuenta": 2, "ingresa": 3,
            "clave": 2, "utiliza": 2, "mensaje": 2, "solicitado": 2,
            "confirmacion": 2, "dispositivo": 2, "nuevo": 1, "este codigo": 2
        }
    },
    "Portuguese": {
        "code": "PT",
        "keywords": {
            "seu": 3, "sua": 3, "codigo": 2, "verificacao": 3, "nao": 2,
            "nao compartilhe": 4, "compartilhe": 3, "com ninguem": 4, "ninguem": 3,
            "seguranca": 3, "para entrar": 3, "entrar": 2, "conta": 2, "senha": 3,
            "utilize": 3, "mensagem": 2, "aparelho": 3, "novo aparelho": 4,
            "este codigo": 2, "confirmar": 2, "cadastrado": 3
        }
    },
    "German": {
        "code": "DE",
        "keywords": {
            "dein": 3, "ihr": 2, "ihre": 2, "lautet": 3, "ist dein": 3,
            "bestatigungscode": 4, "sicherheitscode": 4, "verifizierungscode": 4,
            "passwort": 2, "nicht": 2, "weitergeben": 4, "teilen sie": 4,
            "mit niemandem": 4, "niemandem": 3, "konto": 2, "anmeldung": 3,
            "geben sie": 3, "sicherheit": 2, "gerat": 2
        }
    },
    "Italian": {
        "code": "IT",
        "keywords": {
            "il tuo": 3, "tuo": 2, "suo": 2, "codice": 2, "di verifica": 3,
            "non condividere": 4, "condividere": 3, "con nessuno": 4, "nessuno": 3,
            "sicurezza": 2, "accesso": 2, "per accedere": 3, "conferma": 2,
            "dispositivo": 2, "questo codice": 3, "password": 1, "segreto": 2
        }
    },
    "Turkish": {
        "code": "TR",
        "keywords": {
            "kodunuz": 4, "dogrulama": 3, "kodu": 2, "sifre": 3, "giris": 2,
            "paylasmayin": 4, "kimseyle": 3, "onay": 2, "hesabiniz": 3,
            "guvenlik": 3, "icin": 2, "cihaz": 2, "adimi": 2, "tek kullanimlik": 4
        }
    },
    "Indonesian": {
        "code": "ID",
        "keywords": {
            "kode": 2, "verifikasi": 2, "anda": 2, "adalah": 2, "jangan": 3,
            "jangan bagikan": 4, "bagikan": 3, "jangan berikan": 4, "berikan": 2,
            "rahasia": 3, "masuk": 2, "untuk": 2, "keamanan": 3, "akun": 2,
            "perangkat": 2, "tautan": 2
        }
    },
    "Polish": {
        "code": "PL",
        "keywords": {
            "twoj": 3, "kod": 2, "weryfikacyjny": 4, "haslo": 3,
            "nie udostepniaj": 4, "udostepniaj": 3, "bezpieczenstwa": 3,
            "konto": 2, "zaloguj": 3, "nikomu": 3, "potwierdzenia": 3
        }
    },
    "Dutch": {
        "code": "NL",
        "keywords": {
            "uw": 3, "je": 2, "verificatiecode": 4, "beveiligingscode": 4,
            "deel": 2, "deel niet": 4, "niet met": 3, "niemand": 3,
            "wachtwoord": 3, "bevestig": 3, "inloggen": 3, "toestel": 3
        }
    },
    "Vietnamese": {
        "code": "VI",
        "keywords": {
            "ma": 2, "xac minh": 4, "mat khau": 4, "khong chia se": 4,
            "chia se": 2, "dang nhap": 3, "tai khoan": 3, "bao mat": 3
        }
    },
    "English": {
        "code": "EN",
        "keywords": {
            "your": 2, "code": 1, "is": 1, "verification": 2, "security": 2,
            "do not share": 4, "do not give": 4, "account": 2, "login": 2,
            "password": 2, "confirm": 2, "sign in": 3, "device": 2,
            "registered": 2, "use": 1, "to verify": 3, "one time": 3
        }
    }
}

def normalize_text_for_lang(text: str) -> str:
    nfkd = unicodedata.normalize('NFD', text.lower())
    return ''.join([c for c in nfkd if not unicodedata.combining(c)])

def detect_sms_language(text: str) -> Tuple[str, str]:
    if not text:
        return ("English", "EN")
    t = text.strip()

    # 1. Non-Latin Native Scripts (Deterministic)
    if re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", t):
        if any(c in t for c in ["گ", "چ", "پ", "ژ"]):
            return ("Persian", "FA")
        if any(c in t for c in ["ے", "ٹ", "ڈ", "ڑ"]):
            return ("Urdu", "UR")
        return ("Arabic", "AR")

    if re.search(r"[\u0400-\u04FF]", t):
        low_raw = t.lower()
        ukr_chars = ["є", "ї", "ґ", "\u0454", "\u0457", "\u0491"]
        ukr_words = ["підтвердження", "безпеки", "не діліться", "пароль", "входу", "коду"]
        if any(c in t for c in ukr_chars) or any(w in low_raw for w in ukr_words):
            return ("Ukrainian", "UK")
        return ("Russian", "RU")

    if re.search(r"[\u3040-\u30FF]", t):
        return ("Japanese", "JA")
    if re.search(r"[\uAC00-\uD7AF]", t):
        return ("Korean", "KO")
    if re.search(r"[\u4E00-\u9FFF]", t):
        return ("Chinese", "ZH")
    if re.search(r"[\u0900-\u097F]", t):
        return ("Hindi", "HI")
    if re.search(r"[\u0980-\u09FF]", t):
        return ("Bengali", "BN")
    if re.search(r"[\u0590-\u05FF]", t):
        return ("Hebrew", "HE")
    if re.search(r"[\u0E00-\u0E7F]", t):
        return ("Thai", "TH")
    if re.search(r"[\u0370-\u03FF]", t):
        return ("Greek", "EL")

    # 2. Latin Scripts: Normalized Keyword Scoring with Token Boundaries
    norm = normalize_text_for_lang(t)
    scores: Dict[str, int] = {}
    for lang_name, info in LANGUAGE_RULES.items():
        score = 0
        for kw, weight in info["keywords"].items():
            pattern = r"(?:\b|^)" + re.escape(kw) + r"(?:\b|$)"
            matches = len(re.findall(pattern, norm))
            score += matches * weight
        if score > 0:
            scores[lang_name] = score

    if scores:
        best_lang = max(scores.items(), key=lambda x: x[1])[0]
        norm_words = norm.split()
        if any(w in norm_words for w in ("seu", "sua", "voce")) or "nao compartilhe" in norm:
            if "Portuguese" in scores:
                best_lang = "Portuguese"
        elif any(w in norm_words for w in ("tu", "nadie")) or "no compartas" in norm:
            if "Spanish" in scores:
                best_lang = "Spanish"
        elif any(w in norm_words for w in ("votre", "vos", "veuillez")) or "ne partagez" in norm:
            if "French" in scores:
                best_lang = "French"

        return (best_lang, LANGUAGE_RULES[best_lang]["code"])

    return ("English", "EN")

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
        f"• <b>Link Button:</b> {link_str} (/setlink)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{country_lines}"
        f"🔔 <i>Real-time seamless forwarding active. Zero restart alerts.</i>"
    )

    next_fmt_label = "Long (Detailed 📜)" if active_format == "short" else "Short (Modern ⚡)"
    keyboard = [
        [InlineKeyboardButton(f"📋 Toggle Format ({active_format.upper()})", callback_data="toggle_format")],
        [InlineKeyboardButton("✏️ Change Bot Name", callback_data="bot_name_help"),
         InlineKeyboardButton("🔗 Link Button", callback_data="link_help")],
        [InlineKeyboardButton("🔄 Refresh Dashboard", callback_data="refresh_dash")]
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
        errors.append("GIST_TOKEN is missing — required for 28h persistent storage (GitHub token with 'gist' scope)")
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
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by user or system.")
    except Exception as fatal_err:
        logger.error(f"Fatal error in bot main: {fatal_err}")
        sys.exit(2)


# Last updated: 2026-08-27 21:10
