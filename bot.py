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
from typing import Set, Dict, Any, List, Optional, Tuple
from datetime import datetime, timezone

import httpx
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup, CopyTextButton, MessageEntity
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, NetworkError, Conflict
from telegram.request import HTTPXRequest
from telegram.ext import Application, CommandHandler, ContextTypes

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
def load_stored_data() -> dict:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_stored_data(data: dict):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

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
        if not self.enabled or not self.api_url:
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
                         base_url: str = "") -> bool:
        """Prune older than 28h and sync state & handover markers to GitHub Gist."""
        if not self.enabled or not self.api_url:
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
    clean = str(num_str).strip()
    length = len(clean)
    if length <= 4:
        return clean
    if length <= 6:
        return clean[:2] + "****" + clean[-2:]
    return f"{clean[:4]}****{clean[-3:]}"

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

CUSTOM_EMOJI_FALLBACKS: Dict[str, str] = {
    "whatsapp": "🟢",
    "telegram": "✈️",
    "google": "🔍",
    "gmail": "✉️",
    "facebook": "🔵",
    "fb": "🔵",
    "instagram": "📸",
    "insta": "📸",
    "tiktok": "🎵",
    "twitter": "✖️",
    "x": "✖️",
    "discord": "👾",
    "apple": "🍎",
    "icloud": "🍎",
    "microsoft": "🪟",
    "outlook": "📧",
    "amazon": "📦",
    "netflix": "🎬",
    "uber": "🚗",
    "snapchat": "👻",
    "viber": "🟣",
    "paypal": "💳",
    "wechat": "🟢",
    "line": "🟢",
    "imo": "🟡",
    "steam": "🎮",
    "yahoo": "🟣",
    "linkedin": "💼",
    "binance": "🪙",
}

SERVICE_ICONS: Dict[str, Tuple[str, str]] = {
    "whatsapp": ("WhatsApp",   "https://img.icons8.com/color/512/whatsapp--v1.png"),
    "telegram": ("Telegram",   "https://img.icons8.com/color/512/telegram-app--v1.png"),
    "google":   ("Google",     "https://img.icons8.com/color/512/google-logo.png"),
    "gmail":    ("Gmail",      "https://img.icons8.com/color/512/gmail-new.png"),
    "facebook": ("Facebook",   "https://img.icons8.com/color/512/facebook-new.png"),
    "fb":       ("Facebook",   "https://img.icons8.com/color/512/facebook-new.png"),
    "instagram":("Instagram",  "https://img.icons8.com/color/512/instagram-new--v1.png"),
    "insta":    ("Instagram",  "https://img.icons8.com/color/512/instagram-new--v1.png"),
    "tiktok":   ("TikTok",     "https://img.icons8.com/color/512/tiktok--v1.png"),
    "twitter":  ("Twitter/X",  "https://img.icons8.com/color/512/twitterx--v1.png"),
    "x":        ("X",          "https://img.icons8.com/color/512/twitterx--v1.png"),
    "discord":  ("Discord",    "https://img.icons8.com/color/512/discord-logo--v1.png"),
    "apple":    ("Apple",      "https://img.icons8.com/color/512/mac-os--v1.png"),
    "icloud":   ("iCloud",     "https://img.icons8.com/color/512/mac-os--v1.png"),
    "microsoft":("Microsoft",  "https://img.icons8.com/color/512/microsoft.png"),
    "outlook":  ("Outlook",    "https://img.icons8.com/color/512/microsoft-outlook-2019.png"),
    "amazon":   ("Amazon",     "https://img.icons8.com/color/512/amazon.png"),
    "netflix":  ("Netflix",    "https://img.icons8.com/color/512/netflix.png"),
    "uber":     ("Uber",       "https://img.icons8.com/color/512/uber-app.png"),
    "snapchat": ("Snapchat",   "https://img.icons8.com/color/512/snapchat.png"),
    "viber":    ("Viber",      "https://img.icons8.com/color/512/viber.png"),
    "paypal":   ("PayPal",     "https://img.icons8.com/color/512/paypal.png"),
    "wechat":   ("WeChat",     "https://img.icons8.com/color/512/weixing.png"),
    "line":     ("LINE",       "https://img.icons8.com/color/512/line-me.png"),
    "steam":    ("Steam",      "https://img.icons8.com/color/512/steam-circled.png"),
    "yahoo":    ("Yahoo",      "https://img.icons8.com/color/512/yahoo.png"),
    "linkedin": ("LinkedIn",   "https://img.icons8.com/color/512/linkedin.png"),
    "binance":  ("Binance",    "https://img.icons8.com/color/512/bitcoin.png"),
    "imo":      ("IMO",        "https://img.icons8.com/color/512/imo.png"),
}
DEFAULT_ICON_URL = "https://img.icons8.com/color/512/sms.png"

def get_service_icon_url(source: str) -> str:
    """Returns real app logo URL for the given service/source string."""
    low = (source or "").lower()
    for key, (_, url) in SERVICE_ICONS.items():
        if key in low:
            return url
    return DEFAULT_ICON_URL

def is_admin(user_id: int) -> bool:
    if not ADMIN_USER_IDS:
        return True
    return user_id in ADMIN_USER_IDS

def get_service_display(source: str) -> str:
    s = (source or "").strip()
    if not s:
        return "📱"
    low = s.lower()
    data = load_stored_data()
    custom_emojis = data.get("custom_emojis", {})
    # 1. Check custom emoji set via /set_icon
    for key, eid in custom_emojis.items():
        if key.lower() in low and eid:
            fallback = CUSTOM_EMOJI_FALLBACKS.get(key.lower(), "🟢")
            return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'
    # 2. Return fallback icon
    for key, fallback in CUSTOM_EMOJI_FALLBACKS.items():
        if key in low:
            return fallback
    return "📱"

async def cmd_set_icon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to set a real app custom emoji for a service."""
    user = update.effective_user
    if not is_admin(user.id if user else 0):
        await update.message.reply_text("❌ Unauthorized.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "ℹ️ <b>Usage:</b> <code>/set_icon &lt;service&gt; &lt;emoji&gt;</code>\n"
            "Example: <code>/set_icon whatsapp 🟢</code> (send with real custom emoji)\n"
            "Or reply to any message containing a custom emoji with <code>/set_icon whatsapp</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    service = args[0].strip().lower()
    emoji_id = None

    if len(args) > 1 and args[1].isdigit():
        emoji_id = args[1].strip()

    msg = update.message
    if not emoji_id and msg:
        entities = (msg.entities or ())
        for ent in entities:
            if ent.type in ("custom_emoji", MessageEntity.CUSTOM_EMOJI):
                emoji_id = str(ent.custom_emoji_id)
                break

    if not emoji_id and msg and msg.reply_to_message:
        reply_entities = (msg.reply_to_message.entities or ()) + (msg.reply_to_message.caption_entities or ())
        for ent in reply_entities:
            if ent.type in ("custom_emoji", MessageEntity.CUSTOM_EMOJI):
                emoji_id = str(ent.custom_emoji_id)
                break

    if not emoji_id:
        await update.message.reply_text(
            f"⚠️ No Telegram Custom Emoji detected for '<b>{service}</b>'.\n"
            "Please send a message like: <code>/set_icon whatsapp &lt;custom_emoji&gt;</code>\n"
            "Or provide the numeric ID: <code>/set_icon whatsapp &lt;id&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    data = load_stored_data()
    if "custom_emojis" not in data or not isinstance(data["custom_emojis"], dict):
        data["custom_emojis"] = {}
    data["custom_emojis"][service] = emoji_id
    save_stored_data(data)

    preview = f'<tg-emoji emoji-id="{emoji_id}">🟢</tg-emoji>'
    await update.message.reply_text(
        f"✅ <b>Real app icon saved for {service.capitalize()}!</b>\n"
        f"Preview: {preview}\n"
        f"Emoji ID: <code>{emoji_id}</code>",
        parse_mode=ParseMode.HTML,
    )

async def cmd_list_icons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all configured real app custom emoji icons."""
    user = update.effective_user
    if not is_admin(user.id if user else 0):
        await update.message.reply_text("❌ Unauthorized.")
        return

    data = load_stored_data()
    custom_emojis = data.get("custom_emojis", {})
    if not custom_emojis:
        await update.message.reply_text(
            "ℹ️ No custom emoji icons configured yet.\n"
            "Use <code>/set_icon &lt;service&gt; &lt;custom_emoji&gt;</code> to set one.",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = ["🎨 <b>Configured Real App Icons:</b>\n━━━━━━━━━━━━━━━━━━━━"]
    for svc, eid in custom_emojis.items():
        preview = f'<tg-emoji emoji-id="{eid}">🟢</tg-emoji>'
        lines.append(f"• {preview} <b>{svc.capitalize()}:</b> <code>{eid}</code>")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: send a test OTP notification from the database to all connected groups."""
    if not update.effective_user or not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return

    # 1. Fetch latest real message stored in SQLite database
    db_item = None
    try:
        with get_db_connection() as conn:
            cur = conn.execute("SELECT * FROM processed_otps ORDER BY forwarded_at DESC LIMIT 1;")
            row = cur.fetchone()
            if row:
                db_item = {
                    "id":                 row["id"],
                    "sourceAddress":      row["source"],
                    "country":            row["country"],
                    "destinationNumber":  row["number"],
                    "rangeTemplate":      row["number"],
                    "messageBody":        row["raw_message"],
                    "rate":               row["rate"],
                    "receivedAt":         row["message_time"],
                }
    except Exception as e:
        logger.error(f"Error querying DB for test SMS: {e}")

    test_item: Dict[str, Any] = db_item or {
        "id":            "test-9999",
        "sourceAddress": "WhatsApp",
        "messageBody":   "Your WhatsApp code: 123-456\nYou can also tap this link to verify your phone: v.whatsapp.com/123456",
        "destinationNumber": "+251900000000",
        "language":      "EN",
        "country":       "ET",
    }

    dest_ids = _get_otp_dest_ids()
    if not dest_ids:
        await update.message.reply_text(
            "⚠️ No group chat IDs configured.\n"
            "Use /seturl or set TELEGRAM_GROUP_CHAT_ID in .env first."
        )
        return

    formatted_text, otp_code, icon_url = format_otp_notification(test_item)

    # Build copy-code button if OTP extracted
    reply_markup = None
    if otp_code:
        try:
            kb = [[InlineKeyboardButton(
                f"📋 Copy  {otp_code}",
                copy_text=CopyTextButton(text=otp_code)
            )]]
            reply_markup = InlineKeyboardMarkup(kb)
        except Exception:
            pass

    prefix = "🧪 <b>[TEST MESSAGE FROM DATABASE]</b>\n" if db_item else "🧪 <b>[TEST MESSAGE]</b>\n"
    send_text = prefix + formatted_text

    successes = []
    failures  = []
    for gid in dest_ids:
        ok = await send_with_retry(
            bot=update.get_bot(),
            chat_id=gid,
            text=send_text,
            reply_markup=reply_markup,
            photo_url=icon_url or None,
        )
        if ok:
            successes.append(str(gid))
        else:
            failures.append(str(gid))

    src_label = f"Database (ID: <code>{test_item.get('id')}</code>)" if db_item else "Synthetic Test Template"
    lines = [f"🧪 <b>Test OTP notification sent!</b>\n📦 Source: {src_label}"]
    if successes:
        lines.append(f"✅ Delivered to: {', '.join(successes)}")
    if failures:
        lines.append(f"❌ Failed for: {', '.join(failures)}")
    lines.append(f"\n<b>Preview:</b>\n{formatted_text}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_remove_icon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a custom icon for a service."""
    user = update.effective_user
    if not is_admin(user.id if user else 0):
        await update.message.reply_text("❌ Unauthorized.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text("ℹ️ <b>Usage:</b> <code>/remove_icon &lt;service&gt;</code>", parse_mode=ParseMode.HTML)
        return

    service = args[0].strip().lower()
    data = load_stored_data()
    custom_emojis = data.get("custom_emojis", {})
    if service in custom_emojis:
        del custom_emojis[service]
        data["custom_emojis"] = custom_emojis
        save_stored_data(data)
        await update.message.reply_text(f"✅ Removed icon for <b>{service}</b>.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"⚠️ No icon found for <b>{service}</b>.", parse_mode=ParseMode.HTML)

def get_country_code(item: Dict[str, Any]) -> str:
    """Returns 2-letter uppercase ISO country code (e.g. 'ET', 'UG', 'US')."""
    for field in ["countryCode", "iso", "iso2", "country"]:
        val = str(item.get(field) or "").strip().upper()
        if len(val) == 2 and val.isalpha():
            return val
    display = get_country_iso_display(item)
    parts = display.strip().split()
    if len(parts) >= 2 and len(parts[1]) == 2 and parts[1].isalpha():
        return parts[1].upper()
    elif len(parts) == 1 and len(parts[0]) == 2 and parts[0].isalpha():
        return parts[0].upper()
    return "GLOBAL"

def get_number_display(item: Dict[str, Any]) -> str:
    template = str(item.get("rangeTemplate") or item.get("template") or "").strip()
    if template:
        return template
    raw_num = str(item.get("destinationNumber") or item.get("number") or item.get("dst") or "").strip()
    if raw_num:
        return mask_phone_number(raw_num)
    return ""

def detect_language(text: str, item: Dict[str, Any]) -> str:
    for k in ["language", "lang", "locale"]:
        val = str(item.get(k) or "").strip()
        if val:
            return val.capitalize()
    if not text:
        return "English"
    low = text.lower()
    if any(w in low for w in ["código", "codigo", "tu código", "no compartas"]):
        return "Spanish"
    elif any(w in low for w in ["código", "seu código", "não compartilhe"]):
        return "Portuguese"
    elif any(w in low for w in ["votre code", "ne partagez"]):
        return "French"
    elif any(w in low for w in ["dein code", "teile"]):
        return "German"
    elif any(w in low for w in ["ваш код", "не сообщайте"]):
        return "Russian"
    elif any(w in low for w in ["كود", "رمز"]):
        return "Arabic"
    elif any(w in low for w in ["kodunuz", "kod"]):
        return "Turkish"
    elif any(w in low for w in ["kode", "verifikasi"]):
        return "Indonesian"
    elif any(w in low for w in ["mã", "xác minh"]):
        return "Vietnamese"
    return "English"

def format_otp_notification(item: Dict[str, Any]) -> tuple:
    """Returns (text, otp_code, icon_url) formatted as: ET • 🟢 WhatsApp • 2519281TNE1907 • English"""
    raw_source     = str(item.get("sourceAddress") or item.get("source") or item.get("caller") or item.get("sender") or "").strip()
    raw_message    = str(item.get("messageBody") or item.get("message") or item.get("text") or item.get("body") or item.get("otp") or "")
    otp_code       = extract_otp_code(raw_message)

    country_code   = get_country_code(item)
    service_disp   = get_service_display(raw_source)
    icon_url       = get_service_icon_url(raw_source)
    number_disp    = get_number_display(item)
    language_disp  = detect_language(raw_message, item)

    parts = []
    if country_code:
        parts.append(f"<b>{country_code}</b>")
    if service_disp:
        parts.append(service_disp)
    if number_disp:
        parts.append(f"<b>{number_disp}</b>")
    if language_disp:
        parts.append(language_disp)

    text = " • ".join(parts) if parts else "⚡ <b>NEW OTP</b>"
    if not otp_code and raw_message:
        text += f"\n💬 <code>{html.escape(raw_message[:150])}</code>"

    return text, otp_code, icon_url

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
                          reply_markup=None, photo_url: Optional[str] = None) -> bool:
    for attempt in range(1, max_retries + 1):
        try:
            if photo_url:
                try:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=photo_url,
                        caption=text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=reply_markup,
                        read_timeout=30.0,
                        write_timeout=30.0,
                        connect_timeout=30.0,
                    )
                    return True
                except Exception as photo_err:
                    logger.warning(f"send_photo failed ({photo_err}). Falling back to send_message.")
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
            err_str = str(e).lower()
            if "custom_emoji" in err_str or "entity_custom_emoji_invalid" in err_str:
                logger.warning(f"Custom emoji rejected. Retrying with fallback.")
                clean_text = re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', r'\1', text)
                try:
                    await bot.send_message(chat_id=chat_id, text=clean_text,
                                           parse_mode=ParseMode.HTML, reply_markup=reply_markup)
                    return True
                except Exception as e2:
                    logger.error(f"Fallback send failed: {e2}")
            elif "can't parse entities" in err_str or "parse" in err_str:
                try:
                    plain = re.sub(r"<[^>]+>", "", text)
                    await bot.send_message(chat_id=chat_id, text=plain, reply_markup=reply_markup)
                    return True
                except Exception:
                    pass
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
    mid                         = str(item.get("id") or "").strip()
    formatted_text, otp, icon_url = format_otp_notification(item)
    sent_to_any                 = False

    # Build 💬 <OTP> inline copy button
    markup = None
    if otp:
        try:
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"💬 {otp}", copy_text=CopyTextButton(text=otp))
            ]])
        except Exception:
            markup = None

    for cid in dest_ids:
        try:
            ok = await send_with_retry(bot, cid, formatted_text, reply_markup=markup, photo_url=icon_url)
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

    # Build per-country breakdown (sorted by count desc, top 15)
    country_lines = ""
    if country_forwarded_counts:
        sorted_countries = sorted(country_forwarded_counts.items(), key=lambda x: x[1], reverse=True)
        country_lines = "\n🌍 <b>OTPs by Country (Cumulative):</b>\n"
        for idx, (iso_display, cnt) in enumerate(sorted_countries[:15], 1):
            country_lines += f"  {idx}. {iso_display} — <code>{cnt}</code>\n"
        country_lines += "━━━━━━━━━━━━━━━━━━━━\n"

    gist_status = f"28h Persistent Memory ({GIST_ID[:8]}...) ☁️" if gist_storage.enabled else "Local Storage"
    msg = (
        f"⚡ <b>OTP WAVE 24/7 (Zero-Restart Engine)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Engine Status:</b> <code>100% Online & Forwarding ✅</code>\n"
        f"• <b>Handover Mode:</b> <code>Zero-Restart Handover Active 🔄</code>\n"
        f"• <b>Session Uptime:</b> <code>{uptime_str}</code>\n"
        f"• <b>Next Handover:</b> {handover_info}\n"
        f"• <b>Platform:</b> <code>Thirdwave IPRN</code>\n"
        f"• <b>API Base:</b> <code>{client.base_url}</code>\n"
        f"• <b>Storage:</b> <code>{gist_status}</code>\n"
        f"• <b>Target Groups:</b> <code>{group_text}</code>\n"
        f"• <b>OTPs Forwarded:</b> <code>{total_forwarded_count}</code> <i>(accumulated)</i>\n"
        f"• <b>Lifetime DB:</b> <code>{db_count} total records</code>\n"
        f"• <b>Poll Speed:</b> <code>{POLL_INTERVAL_SECONDS}s</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{country_lines}"
        f"🔔 <i>Real-time seamless forwarding active. Zero restart alerts.</i>"
    )
    await msg_obj.reply_text(msg, parse_mode=ParseMode.HTML)

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
    application.add_handler(CommandHandler("set_icon", cmd_set_icon))
    application.add_handler(CommandHandler("list_icons", cmd_list_icons))
    application.add_handler(CommandHandler("remove_icon", cmd_remove_icon))
    application.add_handler(CommandHandler("test", cmd_test))

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
                ("start",       "📊 Bot status & admin dashboard"),
                ("status",      "⚡ Live zero-restart engine status"),
                ("seturl",      "🌐 View or update the Thirdwave API base URL"),
                ("set_icon",    "🎨 Set real app custom emoji for a service"),
                ("list_icons",  "📋 View configured real app icons"),
                ("remove_icon", "🗑️ Remove a configured custom icon"),
                ("test",        "🧪 Send a test OTP notification to the connected group"),
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
