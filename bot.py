#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
⚡ Thirdwave SMS Wave OTP Bot - 100% Fully Automatic Standalone Telegram Bot
====================================================================
Single self-contained Python file. No restarts. Always online.
- Auto-installs dependencies
- SQLite deduplication (7-day retention)
- Forwards all incoming OTPs to Group + Admin private chat
- Crash-proof polling loop (never stops)
- Thirdwave API v1/traffic (30 req/min rate limit compliant)
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
import hashlib
import textwrap
from typing import Set, Dict, Any, List, Optional, Tuple
from datetime import datetime, timezone, timedelta

# Bangladesh Standard Time (BST = UTC+6)
BANGLADESH_TZ = timezone(timedelta(hours=6))

def get_bangladesh_now() -> datetime:
    """Returns the current datetime in Bangladesh Standard Time (BST, UTC+6)."""
    return datetime.now(timezone.utc).astimezone(BANGLADESH_TZ)

def format_bangladesh_12h(dt: Optional[datetime] = None) -> Tuple[str, str]:
    """Formats datetime in Bangladesh 12-hour format with AM/PM returning (time_str, date_str)."""
    if dt is None:
        dt = get_bangladesh_now()
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc).astimezone(BANGLADESH_TZ)
    else:
        dt = dt.astimezone(BANGLADESH_TZ)
    time_str = dt.strftime("%I:%M:%S %p")
    date_str = dt.strftime("%d-%m-%Y")
    return time_str, date_str

def get_current_commit() -> str:
    """Returns the current git commit SHA (short) from env or git."""
    sha = os.getenv("GITHUB_SHA", "").strip()
    if sha:
        return sha[:7]
    try:
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=3,
            cwd=repo_dir
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return ""

import httpx
from telegram import (
    Update,
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CopyTextButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats,
    BotCommandScopeChat,
    MenuButtonCommands,
    MenuButtonDefault,
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, NetworkError, Conflict, BadRequest
from telegram.request import HTTPXRequest
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Global background tasks set to prevent garbage collection in asyncio
_BACKGROUND_TASKS: Set[asyncio.Task] = set()

def create_safe_task(coro) -> asyncio.Task:
    """Creates an asyncio background task and holds a strong reference to prevent GC collection."""
    t = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(t)
    t.add_done_callback(_BACKGROUND_TASKS.discard)
    return t

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

_raw_base = os.getenv("THIRDWAVE_BASE_URL", "https://thirdwave.im").strip().rstrip("/")
if _raw_base.endswith("/api/v1/traffic"):
    _raw_base = _raw_base[:-len("/api/v1/traffic")].rstrip("/")
if _raw_base and not _raw_base.startswith(("http://", "https://")):
    _raw_base = f"https://{_raw_base}"
THIRDWAVE_BASE_URL    = _raw_base or "https://thirdwave.im"

TELEGRAM_GROUP_CHAT_ID = os.getenv("TELEGRAM_GROUP_CHAT_ID", "").strip()
SECONDARY_GROUP_CHAT_ID = os.getenv("SECONDARY_GROUP_CHAT_ID", os.getenv("TELEGRAM_SECONDARY_GROUP_CHAT_ID", "")).strip()
MIN_FORWARDING_GROUPS: int = 1
MAX_FORWARDING_GROUPS: int = 10
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "9.0"))
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

def _load_settings_from_sqlite() -> dict:
    """
    Loads all bot configuration settings from the permanent SQLite database (bot_settings table).
    Reads the base bot_data JSON document and merges all individual key-value rows.
    """
    result = {}
    try:
        if os.path.exists(DB_FILE):
            conn = sqlite3.connect(DB_FILE, timeout=30.0)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA busy_timeout = 30000;")
                cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bot_settings';")
                if not cur.fetchone():
                    return {}
                # 1. Base configuration document
                cur = conn.execute("SELECT value FROM bot_settings WHERE key = 'bot_data' LIMIT 1;")
                row = cur.fetchone()
                if row and row["value"]:
                    try:
                        parsed = json.loads(row["value"])
                        if isinstance(parsed, dict):
                            result.update(parsed)
                    except Exception:
                        pass
                # 2. Overlay all individual key-value rows for real-time fidelity
                cur = conn.execute("SELECT key, value FROM bot_settings WHERE key != 'bot_data';")
                for r in cur.fetchall():
                    k = r["key"]
                    v = r["value"]
                    if v is None:
                        continue
                    val_str = str(v).strip()
                    try:
                        result[k] = json.loads(val_str)
                    except Exception:
                        result[k] = val_str
            finally:
                conn.close()
    except Exception as e:
        logger.warning(f"Error loading settings from SQLite: {e}")
    return result

def get_bot_setting(key: str, default: str = "") -> str:
    """Reads a single setting string directly from SQLite bot_settings table."""
    try:
        if os.path.exists(DB_FILE):
            conn = sqlite3.connect(DB_FILE, timeout=30.0)
            try:
                conn.execute("PRAGMA busy_timeout = 30000;")
                cur = conn.execute("SELECT value FROM bot_settings WHERE key = ? LIMIT 1;", (key,))
                row = cur.fetchone()
                if row and row[0] is not None:
                    return str(row[0]).strip()
            finally:
                conn.close()
    except Exception as e:
        logger.debug(f"get_bot_setting notice ({key}): {e}")
    return default

def set_bot_setting(key: str, value: str) -> bool:
    """Writes a single setting string directly to SQLite bot_settings table."""
    try:
        conn = sqlite3.connect(DB_FILE, timeout=30.0)
        try:
            conn.execute("PRAGMA busy_timeout = 30000;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key         TEXT PRIMARY KEY,
                    value       TEXT,
                    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.execute(
                "INSERT OR REPLACE INTO bot_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP);",
                (key, str(value))
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"set_bot_setting error ({key}): {e}")
    return False

def delete_bot_setting(key: str) -> bool:
    """Deletes a setting from SQLite bot_settings table."""
    try:
        if os.path.exists(DB_FILE):
            conn = sqlite3.connect(DB_FILE, timeout=30.0)
            try:
                conn.execute("PRAGMA busy_timeout = 30000;")
                conn.execute("DELETE FROM bot_settings WHERE key = ?;", (key,))
                conn.commit()
                return True
            finally:
                conn.close()
    except Exception as e:
        logger.warning(f"delete_bot_setting error ({key}): {e}")
    return False

def _save_settings_to_sqlite(data: dict):
    """
    Saves all bot configuration settings permanently to the SQLite database (bot_settings table).
    Stores both the consolidated JSON document and individual key-value rows.
    Purges deleted keys so removed buttons/settings are never resurrected.
    """
    try:
        conn = sqlite3.connect(DB_FILE, timeout=30.0)
        try:
            conn.execute("PRAGMA busy_timeout = 30000;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key         TEXT PRIMARY KEY,
                    value       TEXT,
                    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.execute(
                "INSERT OR REPLACE INTO bot_settings (key, value, updated_at) VALUES ('bot_data', ?, CURRENT_TIMESTAMP);",
                (json.dumps(data),)
            )
            for k, v in data.items():
                if v is None:
                    continue
                val_str = json.dumps(v) if isinstance(v, (dict, list, bool)) else str(v)
                conn.execute(
                    "INSERT OR REPLACE INTO bot_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP);",
                    (k, val_str)
                )

            # Explicitly purge legacy keys when custom link buttons are empty or cleared
            if data.get("custom_link_buttons_cleared") or data.get("custom_link_buttons") == []:
                conn.execute("DELETE FROM bot_settings WHERE key IN ('custom_link_button', 'custom_link_button_url', 'custom_link_button_text');")
                conn.execute("INSERT OR REPLACE INTO bot_settings (key, value, updated_at) VALUES ('custom_link_buttons', '[]', CURRENT_TIMESTAMP);")
                conn.execute("INSERT OR REPLACE INTO bot_settings (key, value, updated_at) VALUES ('custom_link_buttons_cleared', '1', CURRENT_TIMESTAMP);")
            elif "custom_link_button" not in data:
                conn.execute("DELETE FROM bot_settings WHERE key IN ('custom_link_button', 'custom_link_button_url', 'custom_link_button_text');")

            conn.commit()
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
            except Exception:
                pass
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"Error saving settings to SQLite: {e}")

def load_stored_data() -> dict:
    """
    Loads bot settings. SQLite persistent database is ALWAYS authoritative first.
    Falls back to bot_data.json only for fresh initial boots, and syncs immediately to SQLite.
    """
    global _STORED_DATA_CACHE
    if not _STORED_DATA_CACHE:
        # 1. Authoritative SQLite database FIRST (persists across git pushes & runner handovers)
        db_loaded = _load_settings_from_sqlite()
        if db_loaded:
            _STORED_DATA_CACHE = db_loaded
            try:
                with open(DATA_FILE, "w", encoding="utf-8") as f:
                    json.dump(db_loaded, f, indent=2)
            except Exception:
                pass
        # 2. Fallback to local bot_data.json if database was empty / new install
        elif os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict) and loaded:
                        _STORED_DATA_CACHE = loaded
                        _save_settings_to_sqlite(loaded)
            except Exception:
                pass
    return dict(_STORED_DATA_CACHE)

def save_stored_data(data: dict):
    """
    Persists bot settings to in-memory cache, local JSON file, and permanent SQLite database.
    """
    global _STORED_DATA_CACHE
    try:
        _STORED_DATA_CACHE = dict(data)
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning(f"Error saving stored data to {DATA_FILE}: {e}")
    _save_settings_to_sqlite(data)

def normalize_button_url(raw_url: str) -> Optional[str]:
    """
    Normalizes URLs, Telegram usernames (@bot / bot), and links to clean, valid clickable URLs.
    Supports:
    - https://... or http://... or tg://...
    - @username or @channel    -> https://t.me/username
    - t.me/username            -> https://t.me/username
    - telegram.me/username     -> https://t.me/username
    - username (alphanumeric)  -> https://t.me/username
    Strips '?start=get' artifacts so links remain clean, professional, and standard.
    """
    url = str(raw_url or "").strip()
    if not url:
        return None

    # Strip unwanted ?start=get artifact to keep links clean and professional
    if url.endswith("?start=get"):
        url = url[:-len("?start=get")].rstrip("/")

    def _tg_url(username: str) -> str:
        clean = username.strip().lstrip("@")
        return f"https://t.me/{clean}"

    if url.startswith("@"):
        return _tg_url(url)
    if url.startswith("t.me/"):
        return f"https://{url}"
    if url.startswith("telegram.me/"):
        clean_user = url.replace("telegram.me/", "").strip().lstrip("/")
        return _tg_url(clean_user)
    if url.startswith(("http://", "https://", "tg://")):
        return url
    if all(c.isalnum() or c == "_" for c in url):
        return _tg_url(url)
    if "." in url and not url.startswith("http"):
        return f"https://{url}"
    return url

def get_custom_link_buttons() -> List[Dict[str, str]]:
    """
    Returns list of custom buttons: [{"id": "1", "text": "...", "url": "..."}, ...]
    Maintains backward compatibility with legacy single dict data['custom_link_button']
    ONLY if custom buttons were never configured or cleared.
    """
    data = load_stored_data()

    # 1. If admin explicitly cleared buttons or marked cleared in SQLite/data, return empty list!
    if data.get("custom_link_buttons_cleared") or get_bot_setting("custom_link_buttons_cleared") == "1":
        return []

    # 2. Check explicit custom_link_buttons list
    buttons = []
    raw_list = data.get("custom_link_buttons")
    if isinstance(raw_list, list):
        for idx, item in enumerate(raw_list):
            if isinstance(item, dict) and item.get("text") and item.get("url"):
                b_text = str(item["text"]).strip()
                raw_u  = str(item["url"]).strip()
                b_url  = normalize_button_url(raw_u) or raw_u
                b_id   = str(item.get("id") or (idx + 1))
                buttons.append({"id": b_id, "text": b_text, "url": b_url})
        return buttons

    # 3. Only if custom_link_buttons key was NEVER defined anywhere in data/db:
    if "custom_link_buttons" not in data:
        legacy = data.get("custom_link_button")
        if isinstance(legacy, dict) and legacy.get("text") and legacy.get("url"):
            legacy_text = str(legacy["text"]).strip()
            raw_lu      = str(legacy["url"]).strip()
            legacy_url  = normalize_button_url(raw_lu) or raw_lu
            buttons.append({"id": "1", "text": legacy_text, "url": legacy_url})
            data["custom_link_buttons"] = buttons
            save_stored_data(data)
    return buttons

def save_custom_link_buttons(buttons: List[Dict[str, str]]):
    """Saves normalized custom link buttons list and syncs both locally and to cloud Gist."""
    data = load_stored_data()
    normalized = []
    for idx, b in enumerate(buttons):
        raw_u  = str(b.get("url", "")).strip()
        norm_u = normalize_button_url(raw_u) or raw_u
        normalized.append({
            "id": str(idx + 1),
            "text": str(b.get("text", "")).strip(),
            "url": norm_u,
        })
    data["custom_link_buttons"] = normalized
    if normalized:
        data["custom_link_buttons_cleared"] = False
        data["custom_link_button"] = {"text": normalized[0]["text"], "url": normalized[0]["url"]}
        set_bot_setting("custom_link_buttons_cleared", "0")
        set_bot_setting("custom_link_buttons", json.dumps(normalized))
        set_bot_setting("custom_link_button", json.dumps(data["custom_link_button"]))
    else:
        data["custom_link_buttons_cleared"] = True
        data.pop("custom_link_button", None)
        set_bot_setting("custom_link_buttons_cleared", "1")
        set_bot_setting("custom_link_buttons", "[]")
        delete_bot_setting("custom_link_button")
        delete_bot_setting("custom_link_button_url")
        delete_bot_setting("custom_link_button_text")

    save_stored_data(data)
    if "sync_data_to_secret_db" in globals():
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(sync_data_to_secret_db())
        except (RuntimeError, Exception):
            pass

def get_dynamic_linked_groups() -> List[int]:
    """Returns list of dynamically linked group chat IDs configured by admin."""
    data = load_stored_data()
    raw = data.get("linked_groups", [])
    result = []
    if isinstance(raw, list):
        for item in raw:
            try:
                cid = int(str(item).strip())
                if cid not in result:
                    result.append(cid)
            except ValueError:
                pass
    return result

def add_dynamic_linked_group(chat_id: int) -> Tuple[bool, str]:
    """
    Adds a new group chat ID to the forwarding pool with strict 1-10 limit enforcement.
    Returns (True, "SUCCESS") or (False, reason).
    """
    all_current = get_target_group_chat_ids()
    if chat_id in all_current:
        return False, "ALREADY_LINKED"
    if len(all_current) >= MAX_FORWARDING_GROUPS:
        return False, "MAX_LIMIT_REACHED"

    data = load_stored_data()
    groups = get_dynamic_linked_groups()
    if chat_id not in groups:
        groups.append(chat_id)
        data["linked_groups"] = groups
        save_stored_data(data)
        if "sync_data_to_secret_db" in globals():
            try:
                asyncio.create_task(sync_data_to_secret_db())
            except Exception:
                pass
        return True, "SUCCESS"
    return False, "ALREADY_LINKED"

def remove_dynamic_linked_group(chat_id: int) -> Tuple[bool, str]:
    """
    Removes a dynamic group chat ID from the forwarding pool with strict 1-10 limit enforcement.
    Returns (True, "SUCCESS") or (False, reason).
    """
    all_current = get_target_group_chat_ids()
    if len(all_current) <= MIN_FORWARDING_GROUPS:
        return False, "MIN_LIMIT_REACHED"

    data = load_stored_data()
    groups = get_dynamic_linked_groups()
    if chat_id in groups:
        groups = [c for c in groups if c != chat_id]
        data["linked_groups"] = groups
        save_stored_data(data)
        if "sync_data_to_secret_db" in globals():
            try:
                asyncio.create_task(sync_data_to_secret_db())
            except Exception:
                pass
        return True, "SUCCESS"
    return False, "ENV_PROTECTED"

def clear_dynamic_linked_groups() -> Tuple[bool, str]:
    """
    Clears all dynamically added groups, ensuring at least MIN_FORWARDING_GROUPS remain.
    """
    all_current = get_target_group_chat_ids()
    dynamic_groups = get_dynamic_linked_groups()
    env_remaining = [g for g in all_current if g not in dynamic_groups]
    if len(env_remaining) < MIN_FORWARDING_GROUPS:
        return False, "MIN_LIMIT_REACHED"

    data = load_stored_data()
    data["linked_groups"] = []
    save_stored_data(data)
    if "sync_data_to_secret_db" in globals():
        try:
            asyncio.create_task(sync_data_to_secret_db())
        except Exception:
            pass
    return True, "SUCCESS"

def get_target_group_chat_ids() -> List[int]:
    """Returns a list of all configured target group chat IDs (Dynamic + Primary + Secondary), capped at MAX_FORWARDING_GROUPS (10)."""
    groups: List[int] = []
    seen = set()

    # 1. Dynamic groups added by admin
    for gid in get_dynamic_linked_groups():
        if gid not in seen:
            seen.add(gid)
            groups.append(gid)

    # 2. Check stored json overrides
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

    # 3. Check primary env variable (supports comma-separated list as well)
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

    # 4. Check secondary env variable
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

    return groups[:MAX_FORWARDING_GROUPS]

def get_linked_group_chat_id() -> Optional[int]:
    """Returns the primary linked group chat ID."""
    targets = get_target_group_chat_ids()
    return targets[0] if targets else None

def is_admin(user_id: int) -> bool:
    """Strict admin check: checks environment ADMIN_USER_IDS and database bot_admins."""
    if not user_id:
        return False
    if ADMIN_USER_IDS and user_id in ADMIN_USER_IDS:
        return True
    try:
        with get_db_connection() as conn:
            row = conn.execute("SELECT user_id FROM bot_admins WHERE user_id = ?;", (user_id,)).fetchone()
            if row:
                return True
    except Exception:
        pass
    return False

def is_user_authorized(user_id: int) -> bool:
    if not ADMIN_USER_IDS:
        return True
    return is_admin(user_id)

def get_all_admin_ids() -> List[int]:
    """Returns all unique admin user IDs from environment and database."""
    admins = set(ADMIN_USER_IDS)
    try:
        with get_db_connection() as conn:
            for r in conn.execute("SELECT user_id FROM bot_admins;").fetchall():
                admins.add(r["user_id"])
    except Exception:
        pass
    return list(admins)

def get_all_admin_details() -> List[Dict[str, Any]]:
    """Returns detailed information for all administrators."""
    results = []
    seen = set()
    # 1. Environment Super Admins
    for aid in ADMIN_USER_IDS:
        seen.add(aid)
        results.append({
            "user_id": aid,
            "username": "",
            "first_name": "Super Admin",
            "is_super": True,
            "added_by": 0,
        })
    # 2. Database Admins
    try:
        with get_db_connection() as conn:
            rows = conn.execute("SELECT user_id, added_by, username, first_name FROM bot_admins;").fetchall()
            for r in rows:
                uid = r["user_id"]
                if uid not in seen:
                    seen.add(uid)
                    results.append({
                        "user_id": uid,
                        "username": r["username"] or "",
                        "first_name": r["first_name"] or "Admin",
                        "is_super": False,
                        "added_by": r["added_by"] or 0,
                    })
    except Exception:
        pass
    return results

def add_admin(user_id: int, added_by: int = 0, username: str = "", first_name: str = "") -> bool:
    """Adds a new admin to the database and syncs to cloud Gist."""
    try:
        with get_db_connection() as conn:
            conn.execute("""
                INSERT INTO bot_admins (user_id, added_by, username, first_name)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = CASE WHEN excluded.username != '' THEN excluded.username ELSE bot_admins.username END,
                    first_name = CASE WHEN excluded.first_name != '' THEN excluded.first_name ELSE bot_admins.first_name END;
            """, (user_id, added_by, username or "", first_name or ""))
            conn.commit()
        if "gist_storage" in globals() and gist_storage.enabled:
            try:
                asyncio.create_task(sync_admins_to_gist())
            except Exception:
                pass
        return True
    except Exception as e:
        logger.error(f"Error adding admin {user_id}: {e}")
        return False

def remove_admin(user_id: int) -> bool:
    """Removes an admin from database (cannot remove env super admins)."""
    if ADMIN_USER_IDS and user_id in ADMIN_USER_IDS:
        return False
    try:
        with get_db_connection() as conn:
            conn.execute("DELETE FROM bot_admins WHERE user_id = ?;", (user_id,))
            conn.commit()
        if "gist_storage" in globals() and gist_storage.enabled:
            try:
                asyncio.create_task(sync_admins_to_gist())
            except Exception:
                pass
        return True
    except Exception as e:
        logger.error(f"Error removing admin {user_id}: {e}")
        return False

async def notify_admin_revocation(bot: Bot, target_id: int):
    """
    Sends a professional confirmation message to the removed administrator
    informing them that their privileges have been revoked, and automatically
    removes their admin reply keyboard.
    """
    try:
        bot_name = get_bot_name()
        notification_text = (
            "⛔ <b>Administrator Privileges Revoked</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Your administrator access on <b>{html.escape(bot_name)}</b> has been revoked by the Super Administrator.\n\n"
            "• <b>Account Status:</b> <code>Standard User</code>\n"
            "• <b>Admin Privileges:</b> <code>Disabled</code>\n"
            "• <b>Dashboard Access:</b> <code>Cleared & Deactivated</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Your admin controls have been removed from this account.</i>"
        )
        await send_with_retry(bot, target_id, notification_text, reply_markup=ReplyKeyboardRemove())
        logger.info(f"Delivered admin revocation notification to user {target_id}")
    except Exception as e:
        logger.warning(f"Could not deliver admin revocation notice to {target_id}: {e}")

async def notify_admin_promoted(bot: Bot, target_id: int):
    """Sends a professional notification when a user is promoted to Administrator."""
    try:
        bot_name = get_bot_name()
        notification_text = (
            "👑 <b>Administrator Privileges Granted</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"You have been granted Administrator privileges on <b>{html.escape(bot_name)}</b>!\n\n"
            "• <b>Account Status:</b> <code>Administrator</code>\n"
            "• <b>Admin Dashboard:</b> <code>Active & Unlocked</code>\n"
            "• <b>Permissions:</b> <code>OTP forwarding, linked groups & bot controls</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Your menu has been updated with Admin controls. Tap below to access the Admin Panel:</i>"
        )
        await send_with_retry(bot, target_id, notification_text, reply_markup=get_admin_reply_keyboard())
        logger.info(f"Delivered admin promotion notification to user {target_id}")
    except Exception as e:
        logger.warning(f"Could not deliver admin promotion notice to {target_id}: {e}")

def build_admin_manage_view() -> Tuple[str, InlineKeyboardMarkup]:
    admins = get_all_admin_details()
    super_count = sum(1 for a in admins if a.get("is_super"))
    db_count = len(admins) - super_count

    admin_lines = []
    buttons = []
    for idx, a in enumerate(admins, start=1):
        role = "👑 <b>Super Admin</b> (Env)" if a["is_super"] else "🛡️ <b>Admin</b> (Database)"
        u_name = html.escape(a.get("first_name") or "Administrator")
        u_tag = f"@{html.escape(a['username'])}" if a.get("username") else u_name
        admin_lines.append(
            f"{idx}. <b>{u_name}</b> ({u_tag})\n"
            f"   🆔 <code>{a['user_id']}</code> | 🔰 Role: {role}"
        )
        btn_icon = "👑" if a["is_super"] else "🛡️"
        buttons.append([InlineKeyboardButton(
            f"{btn_icon} Manage ID: {a['user_id']} ({u_tag[:14]})",
            callback_data=f"admin_inspect_{a['user_id']}"
        )])

    admins_formatted = "\n\n".join(admin_lines) if admin_lines else "<i>No administrators found.</i>"
    text = (
        f"👑 <b>Administrator Management & Access Control</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Total Administrators:</b> {len(admins)}\n"
        f"• <b>Super Administrators:</b> {super_count}\n"
        f"• <b>Database Administrators:</b> {db_count}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{admins_formatted}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 <i>Tap any administrator button below to view details or revoke privileges.</i>"
    )
    buttons.append([InlineKeyboardButton("➕ Add New Administrator", callback_data="admin_add_admin_prompt")])
    buttons.append([InlineKeyboardButton("❌ Close", callback_data="close_admin_manage")])
    return text, InlineKeyboardMarkup(buttons)

async def sync_admins_to_gist():
    if not gist_storage.enabled:
        return
    try:
        today_total, total_28h, active_countries = get_dashboard_stats()
        base = client.base_url if "client" in globals() else ""
        await gist_storage.save_state(
            seen_dict=seen_timestamps,
            is_handover=False,
            total_forwarded=today_total,
            country_counts=active_countries,
            base_url=base,
        )
    except Exception as e:
        logger.warning(f"Gist admin sync notice: {e}")

# ==========================================
# 6. GitHub Gist Cloud Storage (28-Hour Retention)
# ==========================================
GIST_ID    = os.getenv("GIST_ID", os.getenv("GITHUB_GIST_ID", "")).strip()
GIST_TOKEN = os.getenv("GIST_TOKEN", os.getenv("GH_TOKEN", os.getenv("GITHUB_TOKEN", ""))).strip()
seen_message_ids: Set[str] = set()
seen_timestamps: Dict[str, float] = {}
seen_content_signatures: Dict[str, float] = {}
_gist_dirty: bool = False
_is_handover: bool = os.getenv("IS_HANDOVER", "false").strip().lower() in ("true", "1", "yes")
_manual_restart: bool = os.getenv("IS_MANUAL_RESTART", "false").strip().lower() in ("true", "1", "yes")
_startup_alert_sent: bool = False  # in-process guard — prevents double-fire within a single bot.py run
_handover_epoch: float = 0.0
bot_process_start_time: float = time.time()

GIST_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

class GistStorage:
    def __init__(self, gist_id: str, token: str, filename: str = "thirdwave_seen_messages.json",
                 description: str = "Thirdwave SMS Wave OTP Bot — 28h persistent storage"):
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
                res = await http.get("https://api.github.com/gists?per_page=100", headers=self._auth_headers())
                if res.is_success:
                    gists = res.json()
                    matching_gists = []
                    for g in gists:
                        files = g.get("files", {})
                        if self.filename in files:
                            matching_gists.append(g)

                    if matching_gists:
                        primary = matching_gists[0]
                        self.gist_id = primary.get("id", "")
                        self.api_url = f"https://api.github.com/gists/{self.gist_id}"
                        logger.info(f"☁️ Reusing primary Gist ID: {self.gist_id}")

                        # Clean up any stale duplicate Gists
                        for dup in matching_gists[1:]:
                            dup_id = dup.get("id")
                            if dup_id:
                                try:
                                    del_res = await http.delete(f"https://api.github.com/gists/{dup_id}", headers=self._auth_headers())
                                    if del_res.status_code == 204:
                                        logger.info(f"🧹 Cleaned up duplicate Gist ID: {dup_id}")
                                except Exception as de:
                                    logger.warning(f"Error deleting duplicate Gist {dup_id}: {de}")
                        return True

                # Create fresh Gist if none found
                payload = {
                    "description": self.description,
                    "public": False,
                    "files": {
                        self.filename: {
                            "content": json.dumps({"seen": {}, "bot": self.bot_name})
                        }
                    }
                }
                res = await http.post(
                    "https://api.github.com/gists",
                    headers=self._auth_headers(),
                    json=payload
                )
                if res.is_success:
                    data = res.json()
                    self.gist_id = data.get("id", "")
                    self.api_url = f"https://api.github.com/gists/{self.gist_id}"
                    logger.info(f"☁️ Auto-created new storage Gist: {self.gist_id}")
                    return True
                else:
                    logger.warning(f"Failed to auto-create Gist: {res.status_code} {res.text[:100]}")
        except Exception as e:
            logger.warning(f"Gist ensure error: {e}")
        return False

    async def load_state(self) -> dict:
        """Loads continuous state (seen map, handover status, forwarded counts, config overrides)."""
        if not self.enabled:
            return {}
        if not self.api_url:
            await self.ensure_gist()
        if not self.api_url:
            return {}
        try:
            async with httpx.AsyncClient(timeout=15.0) as http:
                res = await http.get(self.api_url, headers=self._auth_headers())
                if res.status_code == 404:
                    logger.warning("Gist 404 (deleted on GitHub). Auto-recreating new Gist...")
                    self.api_url = ""
                    await self.ensure_gist()
                    return {}
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

                            # Reconcile bot admins
                            admins_data = parsed.get("admins", [])
                            if isinstance(admins_data, list):
                                try:
                                    with get_db_connection() as aconn:
                                        for a in admins_data:
                                            aid = a.get("user_id")
                                            if aid and not a.get("is_super"):
                                                aconn.execute("""
                                                    INSERT INTO bot_admins (user_id, added_by, username, first_name)
                                                    VALUES (?, ?, ?, ?)
                                                    ON CONFLICT(user_id) DO UPDATE SET
                                                        username = CASE WHEN excluded.username != '' THEN excluded.username ELSE bot_admins.username END,
                                                        first_name = CASE WHEN excluded.first_name != '' THEN excluded.first_name ELSE bot_admins.first_name END;
                                                """, (aid, a.get("added_by", 0), a.get("username", ""), a.get("first_name", "")))
                                        aconn.commit()
                                except Exception as ae:
                                    logger.warning(f"Error restoring admins from Gist: {ae}")

                            logger.info(f"☁️ Restored {len(valid_seen)} seen messages from GitHub Gist ({self.gist_id[:8]}...).")
                            return {
                                "seen": valid_seen,
                                "handover": bool(parsed.get("handover", False)),
                                "handover_epoch": float(parsed.get("handover_epoch", 0.0)),
                                "total_forwarded": int(parsed.get("total_forwarded", 0)),
                                "country_counts": parsed.get("country_counts", {}),
                                "base_url": parsed.get("base_url", ""),
                                "bot_data": parsed.get("bot_data", {}),
                                "manual_restart": bool(parsed.get("manual_restart", False)),
                                "last_notified_commit": str(parsed.get("last_notified_commit", "")),
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

            # Active database admins for cloud persistence
            admins_list = []
            try:
                with get_db_connection() as aconn:
                    for row in aconn.execute("SELECT user_id, added_by, username, first_name FROM bot_admins;").fetchall():
                        admins_list.append({
                            "user_id": row["user_id"],
                            "added_by": row["added_by"],
                            "username": row["username"],
                            "first_name": row["first_name"],
                            "is_super": False
                        })
            except Exception:
                pass

            payload_data = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "bot": self.bot_name,
                "count": len(cleaned),
                "seen": cleaned,
                "handover": is_handover,
                "handover_epoch": datetime.now(timezone.utc).timestamp() if is_handover else 0.0,
                "last_active_epoch": datetime.now(timezone.utc).timestamp(),
                "total_forwarded": total_forwarded,
                "country_counts": country_counts or {},
                "base_url": base_url or "",
                "bot_data": load_stored_data(),
                "admins": admins_list,
                "manual_restart": bool(kwargs.get("manual_restart", False)),
                "last_notified_commit": str(kwargs.get("last_notified_commit") or get_bot_setting("last_notified_commit", "")),
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
        base = client.base_url if "client" in globals() else ""
        return await self.save_state(seen_dict, is_handover=False, total_forwarded=total_forwarded_count,
                                     country_counts=country_forwarded_counts, base_url=base)

gist_storage = GistStorage(
    GIST_ID, GIST_TOKEN,
    filename="thirdwave_seen_messages.json",
    description="Thirdwave SMS Wave OTP Bot — 28h persistent storage (auto-managed)"
)

async def sync_data_to_secret_db():
    """Background helper to asynchronously sync current bot data and settings to persistent Cloud Storage."""
    try:
        if "gist_storage" in globals() and gist_storage and gist_storage.enabled:
            base = client.base_url if "client" in globals() else ""
            seen = seen_timestamps if "seen_timestamps" in globals() else {}
            total = total_forwarded_count if "total_forwarded_count" in globals() else 0
            counts = country_forwarded_counts if "country_forwarded_counts" in globals() else {}
            await gist_storage.save_state(seen, is_handover=False, total_forwarded=total, country_counts=counts, base_url=base)
    except Exception as e:
        logger.warning(f"Cloud DB sync background warning: {e}")

# ==========================================
# 7. Local SQLite Database Storage
# ==========================================
class ClosingConnection:
    """Wraps an SQLite connection so context management commits/rollbacks and guarantees clean closing."""
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self._conn.__enter__()
        return self._conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            return self._conn.__exit__(exc_type, exc_val, exc_tb)
        finally:
            self._conn.close()

    def close(self):
        self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)

def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return ClosingConnection(conn)

def init_db():
    with get_db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_otps (
                id                 TEXT PRIMARY KEY,
                source             TEXT,
                country            TEXT,
                number             TEXT,
                otp_code           TEXT,
                raw_message        TEXT,
                rate               TEXT,
                message_time       TEXT,
                received_at        TEXT,
                received_at_epoch  REAL,
                expires_at         TEXT,
                expires_at_epoch   REAL,
                chat_id            INTEGER,
                forwarded_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status             TEXT DEFAULT 'forwarded'
            );
        """)

        # Automatic schema migration for existing databases
        existing_cols = {col["name"] for col in conn.execute("PRAGMA table_info(processed_otps);").fetchall()}
        if "received_at" not in existing_cols:
            conn.execute("ALTER TABLE processed_otps ADD COLUMN received_at TEXT;")
        if "received_at_epoch" not in existing_cols:
            conn.execute("ALTER TABLE processed_otps ADD COLUMN received_at_epoch REAL;")
        if "expires_at" not in existing_cols:
            conn.execute("ALTER TABLE processed_otps ADD COLUMN expires_at TEXT;")
        if "expires_at_epoch" not in existing_cols:
            conn.execute("ALTER TABLE processed_otps ADD COLUMN expires_at_epoch REAL;")
        if "status" not in existing_cols:
            conn.execute("ALTER TABLE processed_otps ADD COLUMN status TEXT DEFAULT 'forwarded';")

        # Backfill any existing records with calculated expiration timing
        rows_to_backfill = conn.execute(
            "SELECT id, message_time, forwarded_at FROM processed_otps WHERE expires_at IS NULL OR expires_at_epoch IS NULL;"
        ).fetchall()
        for r in rows_to_backfill:
            mid = r["id"]
            msg_t = str(r["message_time"] or "")
            fwd_t = str(r["forwarded_at"] or "")
            ts = parse_message_timestamp(msg_t) or parse_message_timestamp(fwd_t) or datetime.now(timezone.utc).timestamp()
            recv_iso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            exp_epoch = ts + (28 * 3600)
            exp_iso = datetime.fromtimestamp(exp_epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """UPDATE processed_otps 
                   SET received_at = COALESCE(received_at, ?),
                       received_at_epoch = COALESCE(received_at_epoch, ?),
                       expires_at = ?,
                       expires_at_epoch = ?
                   WHERE id = ?;""",
                (recv_iso, ts, exp_iso, exp_epoch, mid)
            )

        # High-performance indexes for instantaneous lookups, deduplication & cleanup
        conn.execute("CREATE INDEX IF NOT EXISTS idx_forwarded_at ON processed_otps(forwarded_at);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_processed_otps_num_otp ON processed_otps(number, otp_code);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_processed_otps_num_msg ON processed_otps(number, raw_message);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_processed_otps_received_at ON processed_otps(received_at);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_processed_otps_recv_epoch ON processed_otps(received_at_epoch);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_processed_otps_expires_at ON processed_otps(expires_at);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_processed_otps_expires_epoch ON processed_otps(expires_at_epoch);")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key         TEXT PRIMARY KEY,
                value       TEXT,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_admins (
                user_id     INTEGER PRIMARY KEY,
                added_by    INTEGER DEFAULT 0,
                username    TEXT DEFAULT '',
                first_name  TEXT DEFAULT '',
                added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
        except Exception:
            pass
    cleanup_old_messages()
    logger.info("📦 SQLite database initialized with 28h timing schema & indexes at %s", DB_FILE)

def is_message_duplicate(mid: str, item: Optional[Dict[str, Any]] = None) -> bool:
    """
    Professional 4-layer duplicate and repeat forwarding guard:
    1. Fast in-memory message ID cache (seen_message_ids)
    2. Fast in-memory content signatures (seen_content_signatures: phone+OTP & phone+SMS body)
    3. Primary Key check in SQLite DB (id = ?)
    4. SQLite active content signature check:
       - (same phone number + same OTP code within active 28h window)
       - (same phone number + identical SMS body within active 28h window)
    Guarantees that on bot restarts, workflow handovers, or API repeats, NO duplicate OTP is EVER sent!
    """
    if mid and mid in seen_message_ids:
        return True

    raw_num = ""
    masked_num = ""
    raw_msg = ""
    otp_code = ""
    digits = ""
    if item:
        raw_num = str(item.get("number") or item.get("destinationNumber") or item.get("range_template") or item.get("rangeTemplate") or item.get("dst") or "").strip()
        if raw_num:
            masked_num = mask_phone_number(raw_num)
            digits = re.sub(r"\D", "", raw_num)
        raw_msg = str(item.get("sms") or item.get("messageBody") or item.get("message") or item.get("text") or item.get("body") or item.get("otp") or "").strip()
        otp_code = extract_otp_code(raw_msg)

    # Layer 2: Fast in-memory content signature check
    if digits:
        if otp_code:
            sig_otp = f"otp:{digits}:{otp_code}"
            if sig_otp in seen_content_signatures:
                if mid:
                    seen_message_ids.add(mid)
                return True
        if raw_msg:
            clean_b = re.sub(r"\s+", " ", raw_msg).strip()
            sig_body = f"body:{digits}:{clean_b}"
            if sig_body in seen_content_signatures:
                if mid:
                    seen_message_ids.add(mid)
                return True

    num_variants = []
    if raw_num:
        num_variants.append(raw_num)
        if digits:
            if digits not in num_variants:
                num_variants.append(digits)
            with_plus = f"+{digits}"
            if with_plus not in num_variants:
                num_variants.append(with_plus)
    if masked_num and masked_num not in num_variants:
        num_variants.append(masked_num)

    try:
        now_epoch = datetime.now(timezone.utc).timestamp()
        with get_db_connection() as conn:
            # Check 1: Primary ID match
            if mid:
                cur = conn.execute("SELECT 1 FROM processed_otps WHERE id = ? LIMIT 1;", (mid,))
                if cur.fetchone() is not None:
                    seen_message_ids.add(mid)
                    return True

            # Check 2: Phone number variants + OTP code match (active within 28h window)
            if otp_code and num_variants:
                placeholders = ",".join(["?"] * len(num_variants))
                cur = conn.execute(
                    f"""SELECT 1 FROM processed_otps 
                       WHERE number IN ({placeholders}) AND otp_code = ? 
                         AND (expires_at_epoch IS NULL OR expires_at_epoch > ?)
                       LIMIT 1;""",
                    (*num_variants, otp_code, now_epoch)
                )
                if cur.fetchone() is not None:
                    if mid:
                        seen_message_ids.add(mid)
                    if digits:
                        seen_content_signatures[f"otp:{digits}:{otp_code}"] = now_epoch
                    return True

            # Check 3: Phone number variants + identical SMS body match (active within 28h window)
            if raw_msg and num_variants:
                placeholders = ",".join(["?"] * len(num_variants))
                cur = conn.execute(
                    f"""SELECT 1 FROM processed_otps 
                       WHERE number IN ({placeholders}) AND TRIM(raw_message) = ? 
                         AND (expires_at_epoch IS NULL OR expires_at_epoch > ?)
                       LIMIT 1;""",
                    (*num_variants, raw_msg, now_epoch)
                )
                if cur.fetchone() is not None:
                    if mid:
                        seen_message_ids.add(mid)
                    if digits:
                        clean_b = re.sub(r"\s+", " ", raw_msg).strip()
                        seen_content_signatures[f"body:{digits}:{clean_b}"] = now_epoch
                    return True

    except Exception as e:
        logger.error(f"DB duplicate check error: {e}")

    return False

def is_message_seen(message_id: str) -> bool:
    """Backward compatibility wrapper for is_message_duplicate."""
    return is_message_duplicate(message_id)

def save_processed_message(item: Dict[str, Any], chat_id: int,
                           country: str = "", masked_num: str = "", otp_code: str = "",
                           status: str = "forwarded") -> bool:
    """
    Saves received SMS to the permanent SQLite database with explicit receiving and 28h expiration timing:
    - received_at & received_at_epoch: Exact arrival timestamp
    - expires_at & expires_at_epoch: Expiration timestamp (strictly 28h = 100,800s after arrival)
    - status: 'forwarded', 'silent_recorded', 'blocked_pre_resume', 'matched_existing'
    """
    global _gist_dirty
    mid = str(item.get("id") or "").strip()
    if not mid:
        return False

    raw_message  = str(item.get("sms") or item.get("messageBody") or item.get("message") or item.get("text") or item.get("body") or item.get("otp") or "").strip()
    raw_number   = str(item.get("number") or item.get("destinationNumber") or item.get("range_template") or item.get("rangeTemplate") or item.get("dst") or "").strip()
    stored_num   = raw_number or masked_num
    otp_val      = otp_code or extract_otp_code(raw_message)
    source       = str(item.get("sender_id") or item.get("sourceAddress") or item.get("source") or item.get("caller") or item.get("sender") or "").strip()
    rate         = str(item.get("rate") or "").strip()

    # Parse arrival time
    message_time = str(item.get("received_at") or item.get("receivedAt") or item.get("messageTime") or item.get("createdAt") or "").strip()
    item_ts = parse_message_timestamp(message_time)
    now_utc = datetime.now(timezone.utc)
    now_epoch = now_utc.timestamp()

    if item_ts > 0:
        recv_epoch = item_ts
    else:
        recv_epoch = now_epoch
        message_time = now_utc.strftime("%Y-%m-%d %H:%M:%S")

    recv_iso = datetime.fromtimestamp(recv_epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Explicit 28-hour expiration timing (100,800 seconds)
    retention_seconds = 28 * 3600
    cutoff_28h = now_epoch - retention_seconds
    if recv_epoch < cutoff_28h:
        # Reject storing any OTP already older than 28 hours
        seen_message_ids.add(mid)
        seen_timestamps[mid] = recv_epoch
        return False

    expires_epoch = recv_epoch + retention_seconds
    expires_iso = datetime.fromtimestamp(expires_epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    fwd_time_str = now_utc.strftime("%Y-%m-%d %H:%M:%S")

    # Populate in-memory deduplication tracking
    seen_message_ids.add(mid)
    seen_timestamps[mid] = recv_epoch
    if stored_num:
        num_digits = re.sub(r"\D", "", stored_num)
        if num_digits and otp_val:
            seen_content_signatures[f"otp:{num_digits}:{otp_val}"] = recv_epoch
        if num_digits and raw_message:
            clean_b = re.sub(r"\s+", " ", raw_message).strip()
            seen_content_signatures[f"body:{num_digits}:{clean_b}"] = recv_epoch
    _gist_dirty = True

    try:
        with get_db_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO processed_otps
                    (id, source, country, number, otp_code, raw_message, rate, message_time,
                     received_at, received_at_epoch, expires_at, expires_at_epoch,
                     chat_id, forwarded_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (mid, source, country, stored_num, otp_val, raw_message, rate, message_time,
                  recv_iso, recv_epoch, expires_iso, expires_epoch,
                  chat_id, fwd_time_str, status))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"DB save error: {e}")
    return False

def get_today_start_utc() -> str:
    """Returns today's 00:00:00 AM (BST = UTC+6) converted to UTC 'YYYY-MM-DD HH:MM:SS' string for today metrics."""
    now_bd = get_bangladesh_now()
    today_start_bd = now_bd.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_start_bd.astimezone(timezone.utc)
    return today_start_utc.strftime("%Y-%m-%d %H:%M:%S")

def get_latest_processed_timestamp() -> float:
    """Returns the UNIX epoch timestamp of the newest OTP in SQLite processed_otps, or 0.0."""
    try:
        with get_db_connection() as conn:
            cur = conn.execute("""
                SELECT forwarded_at, message_time 
                FROM processed_otps 
                ORDER BY datetime(forwarded_at) DESC 
                LIMIT 1;
            """)
            row = cur.fetchone()
            if row:
                fwd_str = str(row["forwarded_at"] or "")
                msg_str = str(row["message_time"] or "")
                ts1 = parse_message_timestamp(fwd_str)
                ts2 = parse_message_timestamp(msg_str)
                return max(ts1, ts2)
    except Exception as e:
        logger.debug(f"Error getting latest processed timestamp: {e}")
    return 0.0

def update_last_active_timestamp(epoch: Optional[float] = None):
    """Persists last active forwarding timestamp to bot_data.json and SQLite bot_settings."""
    ts = epoch or datetime.now(timezone.utc).timestamp()
    try:
        data = load_stored_data()
        data["last_active_epoch"] = ts
        save_stored_data(data)
    except Exception:
        pass
    try:
        with get_db_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bot_settings (key, value, updated_at) VALUES ('last_active_epoch', ?, CURRENT_TIMESTAMP);",
                (str(ts),)
            )
            conn.commit()
    except Exception:
        pass

def get_previous_session_last_epoch(gist_state: Optional[Dict[str, Any]] = None) -> float:
    """
    Finds the exact timestamp when the previous bot session was last active.
    Combines:
    1. Latest OTP timestamp in SQLite database
    2. last_active_epoch stored in SQLite bot_settings
    3. last_active_epoch in bot_data.json
    4. handover_epoch / last_active_epoch from Gist cloud state
    """
    candidates = []
    
    # 1. Latest OTP recorded in database
    db_ts = get_latest_processed_timestamp()
    if db_ts > 0:
        candidates.append(db_ts)
        
    # 2. bot_settings table
    try:
        with get_db_connection() as conn:
            cur = conn.execute("SELECT value FROM bot_settings WHERE key = 'last_active_epoch' LIMIT 1;")
            row = cur.fetchone()
            if row and row[0]:
                candidates.append(float(row[0]))
    except Exception:
        pass
        
    # 3. bot_data.json
    try:
        data = load_stored_data()
        val = data.get("last_active_epoch")
        if val:
            candidates.append(float(val))
    except Exception:
        pass
        
    # 4. Gist state handover_epoch / last_active_epoch
    if gist_state and isinstance(gist_state, dict):
        ge = float(gist_state.get("handover_epoch") or 0.0)
        if ge > 0:
            candidates.append(ge)
        la = float(gist_state.get("last_active_epoch") or 0.0)
        if la > 0:
            candidates.append(la)
            
    return max(candidates) if candidates else 0.0

def is_otp_forwarding_enabled() -> bool:
    """
    Returns True if live OTP group forwarding is active,
    False if turned OFF by admin (silent mode: DB & receiving history only).
    """
    data = load_stored_data()
    val = data.get("otp_forwarding_enabled")
    if val is not None:
        return bool(val)
    try:
        with get_db_connection() as conn:
            cur = conn.execute("SELECT value FROM bot_settings WHERE key = 'otp_forwarding_enabled' LIMIT 1;")
            row = cur.fetchone()
            if row and row[0] is not None:
                return str(row[0]).strip().lower() in ("true", "1", "yes", "on")
    except Exception:
        pass
    return True

def get_forwarding_resumed_epoch() -> float:
    """Returns the UNIX epoch timestamp when forwarding was last turned ON, or 0.0."""
    data = load_stored_data()
    val = data.get("forwarding_resumed_epoch")
    if val:
        try:
            return float(val)
        except Exception:
            pass
    try:
        with get_db_connection() as conn:
            cur = conn.execute("SELECT value FROM bot_settings WHERE key = 'forwarding_resumed_epoch' LIMIT 1;")
            row = cur.fetchone()
            if row and row[0]:
                return float(row[0])
    except Exception:
        pass
    return 0.0

def set_otp_forwarding_enabled(enabled: bool):
    """
    Saves OTP group forwarding toggle state persistently to bot_data.json and SQLite bot_settings.
    Also records forwarding_resumed_epoch when turned ON so old OTPs from the turned-off window
    are strictly NEVER forwarded to Telegram groups!
    """
    now_epoch = datetime.now(timezone.utc).timestamp()
    data = load_stored_data()
    data["otp_forwarding_enabled"] = bool(enabled)
    if enabled:
        data["forwarding_resumed_epoch"] = now_epoch
    else:
        data["forwarding_paused_epoch"] = now_epoch
    save_stored_data(data)
    try:
        with get_db_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bot_settings (key, value, updated_at) VALUES ('otp_forwarding_enabled', ?, CURRENT_TIMESTAMP);",
                ("true" if enabled else "false",)
            )
            if enabled:
                conn.execute(
                    "INSERT OR REPLACE INTO bot_settings (key, value, updated_at) VALUES ('forwarding_resumed_epoch', ?, CURRENT_TIMESTAMP);",
                    (str(now_epoch),)
                )
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO bot_settings (key, value, updated_at) VALUES ('forwarding_paused_epoch', ?, CURRENT_TIMESTAMP);",
                    (str(now_epoch),)
                )
            conn.commit()
    except Exception as e:
        logger.warning(f"Error persisting forwarding setting: {e}")

def get_dashboard_stats(max_age_hours: int = 28) -> Tuple[int, int, Dict[str, int]]:
    """
    Computes exact statistics directly from active database records:
    - total active OTPs forwarded today (BST calendar day)
    - total active OTPs in the 28-hour retention window
    - per-country OTP counts in the 28-hour retention window
    """
    cleanup_old_messages(max_age_hours=max_age_hours)
    cutoff_epoch = datetime.now(timezone.utc).timestamp() - (max_age_hours * 3600)
    cutoff_iso = datetime.fromtimestamp(cutoff_epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    today_start_str = get_today_start_utc()
    total_28h = 0
    total_today = 0
    country_counts: Dict[str, int] = {}
    try:
        with get_db_connection() as conn:
            # 28-hour active count
            cur = conn.execute(
                "SELECT COUNT(*) FROM processed_otps WHERE datetime(forwarded_at) >= datetime(?);",
                (cutoff_iso,)
            )
            row = cur.fetchone()
            total_28h = row[0] if row else 0

            # Today-only count
            cur = conn.execute(
                "SELECT COUNT(*) FROM processed_otps WHERE datetime(forwarded_at) >= datetime(?);",
                (today_start_str,)
            )
            row = cur.fetchone()
            total_today = row[0] if row else 0

            # Per-country breakdown for 28-hour active records
            cur = conn.execute("""
                SELECT country, COUNT(*) as cnt 
                FROM processed_otps 
                WHERE datetime(forwarded_at) >= datetime(?) 
                  AND country IS NOT NULL AND country != '' 
                GROUP BY country 
                ORDER BY cnt DESC;
            """, (cutoff_iso,))
            for r in cur.fetchall():
                c_name = str(r[0]).strip()
                c_cnt = int(r[1])
                if c_name:
                    disp = get_country_iso_display({"country": c_name})
                    country_counts[disp] = country_counts.get(disp, 0) + c_cnt
    except Exception as e:
        logger.warning(f"Error computing dashboard stats: {e}")
    return total_today, total_28h, country_counts

def get_dashboard_stats_today() -> Tuple[int, Dict[str, int]]:
    """Backwards compatibility wrapper returning (today_total, country_counts)."""
    today_cnt, _, countries = get_dashboard_stats()
    return today_cnt, countries

def get_total_processed_count() -> int:
    _, total_28h, _ = get_dashboard_stats()
    return total_28h

# In-memory fast cache for full SMS bodies (supporting popup alerts)
_SMS_BODY_CACHE: Dict[str, str] = {}

def _cache_sms_message(key: str, message: str):
    if not key or not message:
        return
    _SMS_BODY_CACHE[key] = str(message)
    if len(_SMS_BODY_CACHE) > 3000:
        for k in list(_SMS_BODY_CACHE.keys())[:500]:
            _SMS_BODY_CACHE.pop(k, None)

def get_cached_sms_message(key: str) -> Optional[str]:
    if not key:
        return None
    cached = _SMS_BODY_CACHE.get(key)
    if cached:
        return cached
    try:
        with get_db_connection() as conn:
            cur = conn.execute("SELECT raw_message FROM processed_otps WHERE id = ? LIMIT 1;", (key,))
            row = cur.fetchone()
            if row and row[0]:
                msg = str(row[0]).strip()
                _SMS_BODY_CACHE[key] = msg
                return msg
    except Exception:
        pass
    return None

def cleanup_old_messages(max_age_hours: int = 28):
    """
    Cleans up expired OTP records older than 28 hours from:
    1. Local SQLite database (`processed_otps` table) using exact expires_at_epoch & expires_at.
    2. In-memory deduplication caches (`seen_timestamps`, `seen_message_ids`, `seen_content_signatures`).
    Ensures each OTP is cleared individually one-by-one as its exact 28-hour expiration arrives.
    IMPORTANT: Telegram group messages are NEVER deleted or touched.
    """
    try:
        now_utc = datetime.now(timezone.utc)
        now_epoch = now_utc.timestamp()
        now_iso = now_utc.strftime("%Y-%m-%d %H:%M:%S")
        cutoff_epoch = now_epoch - (max_age_hours * 3600)
        cutoff_iso = datetime.fromtimestamp(cutoff_epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        # 1. Prune in-memory caches older than 28h
        expired_ids = [mid for mid, ts in list(seen_timestamps.items()) if ts < cutoff_epoch]
        for mid in expired_ids:
            seen_timestamps.pop(mid, None)
            seen_message_ids.discard(mid)

        expired_sigs = [sig for sig, ts in list(seen_content_signatures.items()) if ts < cutoff_epoch]
        for sig in expired_sigs:
            seen_content_signatures.pop(sig, None)

        # 2. Prune local SQLite database of any records older than 28 hours using expires_at_epoch / expires_at
        with get_db_connection() as conn:
            cur = conn.execute(
                """DELETE FROM processed_otps 
                   WHERE (expires_at_epoch IS NOT NULL AND expires_at_epoch <= ?)
                      OR (expires_at IS NOT NULL AND datetime(expires_at) <= datetime(?))
                      OR (expires_at IS NULL AND datetime(forwarded_at) < datetime(?))
                      OR (expires_at IS NULL AND message_time IS NOT NULL AND message_time != '' AND datetime(message_time) < datetime(?));""",
                (now_epoch, now_iso, cutoff_iso, cutoff_iso)
            )
            deleted = cur.rowcount
            conn.commit()
            if deleted > 0 or expired_ids:
                logger.info(
                    f"🧹 28h Expiration Cleanup: Deleted {deleted} expired SQLite records & {len(expired_ids)} cache items (older than {max_age_hours}h). Telegram group messages are never deleted."
                )
    except Exception as e:
        logger.warning(f"DB cleanup error: {e}")

# ==========================================
# 8. Thirdwave API Client (GET /api/v1/traffic)
# ==========================================
class ThirdwaveClient:
    """
    Client for Thirdwave SMS API (GET https://thirdwave.im/api/v1/traffic)
    - Documentation: https://thirdwave.im/api/v1/traffic
    - Authentication: api_key parameter in query string (headers are NOT accepted)
    - Parameters: limit (1 to 100, default 20, safe max 60)
    - Rate limit: 30 requests per minute per key (respects Retry-After on HTTP 429)
    - Response structure: {"items": [{"received_at", "sender_id", "range_name", "range_template", "number", "sms", "rate"}]}
    """
    def __init__(self, base_url: str, api_key: str, timeout: float = 8.0):
        self.base_url = self.clean_base_url(base_url)
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.cooldown_until: float = 0.0
        self._client: Optional[httpx.AsyncClient] = None

    @staticmethod
    def clean_base_url(url: str) -> str:
        clean = (url or "").strip().rstrip("/")
        if clean.endswith("/api/v1/traffic"):
            clean = clean[:-len("/api/v1/traffic")].rstrip("/")
        if clean and not clean.startswith(("http://", "https://")):
            clean = f"https://{clean}"
        return clean or "https://thirdwave.im"

    def _get_http_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                limits=httpx.Limits(max_keepalive_connections=10, max_connections=20, keepalive_expiry=60.0),
                headers={
                    "Accept": "application/json",
                    "User-Agent": "ThirdwaveOTP/2.0",
                },
            )
        return self._client

    def update_base_url(self, new_url: str) -> str:
        self.base_url = self.clean_base_url(new_url)
        self._client = None
        return self.base_url

    def update_api_key(self, new_key: str) -> str:
        self.api_key = new_key.strip()
        self._client = None
        return self.api_key

    async def close(self):
        """Cleanly close the underlying HTTP client session."""
        if self._client is not None and not self._client.is_closed:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    async def fetch_incoming_messages(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Fetch most recent incoming messages from Thirdwave API.
        Respects the 30 req/min rate limit and query param api_key authentication.
        """
        now = time.time()
        if now < self.cooldown_until:
            wait_rem = self.cooldown_until - now
            logger.info(f"⏳ Thirdwave API cooldown active ({wait_rem:.1f}s remaining)...")
            return []

        if not self.api_key:
            logger.warning("⚠️ Thirdwave API key is not configured.")
            return []

        endpoint = f"{self.base_url}/api/v1/traffic"
        safe_limit = min(max(1, limit), 60)
        params = {
            "api_key": self.api_key,
            "limit": safe_limit,
        }

        try:
            client = self._get_http_client()
            res = await client.get(endpoint, params=params)

            if res.is_success:
                data = res.json()
                raw_items = []
                if isinstance(data, dict):
                    raw_items = data.get("items") or data.get("rows") or []
                elif isinstance(data, list):
                    raw_items = data

                seen = set()
                out: List[Dict[str, Any]] = []

                for r in raw_items:
                    if not isinstance(r, dict):
                        continue

                    # Extract fields according to Thirdwave specs
                    item_num = str(r.get("number") or r.get("destinationNumber") or "").strip()
                    item_sender = str(r.get("sender_id") or r.get("sourceAddress") or r.get("source") or r.get("caller") or r.get("sender") or "").strip()
                    item_sms = str(r.get("sms") or r.get("messageBody") or r.get("message") or r.get("text") or r.get("body") or r.get("otp") or "").strip()
                    item_recv = str(r.get("received_at") or r.get("receivedAt") or r.get("messageTime") or r.get("createdAt") or "").strip()
                    item_range = str(r.get("range_name") or r.get("rangeName") or r.get("destinationName") or "").strip()
                    item_tmpl = str(r.get("range_template") or r.get("rangeTemplate") or "").strip()
                    item_rate = str(r.get("rate") or "").strip()

                    # Deterministic unique ID if not present in item
                    mid = str(r.get("id") or "").strip()
                    if not mid:
                        sig = f"{item_num}|{item_recv}|{item_sender}|{item_sms}"
                        mid = hashlib.sha256(sig.encode("utf-8")).hexdigest()[:20]

                    if mid in seen:
                        continue
                    seen.add(mid)

                    item_dict = dict(r)
                    item_dict["id"] = mid
                    item_dict["number"] = item_num
                    item_dict["destinationNumber"] = item_num
                    item_dict["sender_id"] = item_sender
                    item_dict["sourceAddress"] = item_sender
                    item_dict["source"] = item_sender
                    item_dict["sms"] = item_sms
                    item_dict["messageBody"] = item_sms
                    item_dict["message"] = item_sms
                    item_dict["received_at"] = item_recv
                    item_dict["receivedAt"] = item_recv
                    item_dict["range_name"] = item_range
                    item_dict["rangeName"] = item_range
                    item_dict["destinationName"] = item_range
                    item_dict["range_template"] = item_tmpl
                    item_dict["rangeTemplate"] = item_tmpl
                    item_dict["rate"] = item_rate

                    out.append(item_dict)

                return out

            elif res.status_code == 429:
                retry_header = res.headers.get("Retry-After", "")
                try:
                    retry_wait = max(5.0, min(float(retry_header), 60.0)) if retry_header else 10.0
                except Exception:
                    retry_wait = 10.0
                self.cooldown_until = time.time() + retry_wait
                logger.warning(f"⚠️ Thirdwave 429 Rate Limit Exceeded. Backing off {retry_wait:.1f}s...")
                return []

            elif res.status_code in (401, 403):
                err_code, err_msg = self._extract_error(res)
                logger.warning(f"❌ Thirdwave Auth Error (HTTP {res.status_code} - {err_code}): {err_msg}")
                return []

            elif res.status_code == 400:
                err_code, err_msg = self._extract_error(res)
                logger.warning(f"⚠️ Thirdwave Parameter Error (HTTP 400 - {err_code}): {err_msg}")
                return []

            elif res.status_code == 503:
                self.cooldown_until = time.time() + 5.0
                logger.warning("⚠️ Thirdwave API temporarily unavailable (HTTP 503). Retrying in 5s...")
                return []

            else:
                logger.warning(f"⚠️ Thirdwave API returned HTTP {res.status_code}: {res.text[:120]}")
                return []

        except httpx.ConnectTimeout:
            logger.warning("⚠️ Thirdwave API connection timed out. Retrying in next polling cycle...")
        except httpx.NetworkError as ne:
            logger.warning(f"⚠️ Thirdwave API network error: {ne}")
        except Exception as e:
            logger.warning(f"⚠️ Thirdwave API fetch error: {e}")

        return []

    async def fetch_traffic_page(self, page: int = 1, page_size: int = 50) -> Tuple[List[Dict[str, Any]], int]:
        # Backward-compatibility alias
        items = await self.fetch_incoming_messages(limit=page_size)
        return items, len(items)

    @staticmethod
    def _extract_error(res: httpx.Response) -> Tuple[str, str]:
        code = "error"
        msg = res.text[:120]
        try:
            ej = res.json()
            if isinstance(ej, dict) and "error" in ej:
                err = ej["error"]
                if isinstance(err, dict):
                    code = str(err.get("code", code))
                    msg = str(err.get("message", msg))
                elif isinstance(err, str):
                    msg = err
        except Exception:
            pass
        return code, msg

# ==========================================
# 9. Country ISO Alpha-2 Lookup & Formatting
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

    # Try range_name / rangeName / destinationName text
    raw_range = str(item.get("range_name") or item.get("rangeName") or item.get("destinationName") or "").strip()
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
        item.get("number") or item.get("destinationNumber") or
        item.get("range_template") or item.get("rangeTemplate") or
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
    clean = str(num_str).strip().replace(" ", "").replace("\u00a0", "")
    if "*" in clean:
        # Standardize existing masks (e.g. 0*** or *** -> ****)
        masked = re.sub(r"\d?\*{2,}", "****", clean)
        masked = re.sub(r"\*+", "****", masked)
        if not masked.startswith("+"):
            masked = f"+{masked}"
        return masked
    digits = re.sub(r"\D", "", clean)
    if not digits:
        return clean

    # Match country calling code prefix from PREFIX_ISO_MAP (lengths 4, 3, 2, 1)
    found_prefix = ""
    for p_len in [4, 3, 2, 1]:
        cand = digits[:p_len]
        if cand in PREFIX_ISO_MAP and len(digits) > p_len:
            found_prefix = cand
            break

    if found_prefix:
        rest = digits[len(found_prefix):]
        if len(rest) >= 6:
            return f"+{found_prefix}{rest[:3]}****{rest[-3:]}"
        elif len(rest) >= 4:
            return f"+{found_prefix}{rest[:2]}****{rest[-2:]}"
        elif len(rest) > 2:
            return f"+{found_prefix}{rest[:1]}****{rest[-1:]}"
        return f"+{found_prefix}{rest}"

    if len(digits) >= 8:
        return f"+{digits[:4]}****{digits[-3:]}"
    elif len(digits) >= 5:
        return f"+{digits[:2]}****{digits[-2:]}"
    return f"+{digits}"

def extract_otp_code(text: str) -> str:
    if not text:
        return ""
    # 1. Google style format: G-123456
    g_match = re.search(r"\b([Gg]-\d{4,8})\b", text)
    if g_match:
        return g_match.group(1).upper()

    # 2. Pattern: "<CODE> is your <service> verification code/otp/login code"
    pre_match = re.search(r"\b([A-Za-z0-9\-]{3,10})\s+is\s+your\b", text, re.IGNORECASE)
    if pre_match:
        cand = pre_match.group(1).strip()
        if any(c.isdigit() for c in cand):
            return cand

    # 3. Keyword followed by code: "code: 123456", "otp is 1234", "pin - 123456"
    kw_match = re.search(
        r"(?:code|otp|pin|passcode|secret|verif\w*|kod\w*|codigo|clave|is)[:\s\-]+([A-Za-z0-9\-]{3,10})\b",
        text, re.IGNORECASE,
    )
    if kw_match:
        code = kw_match.group(1).strip()
        if any(c.isdigit() for c in code):
            return code

    # 4. Hyphenated codes: 123-456, 1234-5678
    hyphen_match = re.findall(r"\b\d{3}-\d{3}\b|\b\d{3}-\d{4}\b|\b\d{4}-\d{4}\b", text)
    if hyphen_match:
        return hyphen_match[0]

    # 5. Standalone 4-8 digit numbers
    digits_match = re.findall(r"\b[0-9]{4,8}\b", text)
    if digits_match:
        return digits_match[0]
    return ""

def parse_message_timestamp(time_str: str) -> float:
    if not time_str:
        return 0.0
    clean = str(time_str).strip()
    try:
        # Numeric epoch string (seconds or milliseconds)
        if clean.replace(".", "", 1).isdigit():
            val = float(clean)
            return (val / 1000.0) if val > 1e11 else val
        if "T" in clean:
            clean_iso = clean.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(clean[:19], fmt[:len(clean[:19])]).replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except ValueError:
                continue
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0

ISO_TO_COUNTRY_NAME: Dict[str, str] = {
    "AD": "Andorra", "AE": "UAE", "AF": "Afghanistan", "AL": "Albania",
    "AM": "Armenia", "AO": "Angola", "AR": "Argentina", "AT": "Austria", "AU": "Australia",
    "AZ": "Azerbaijan", "BA": "Bosnia", "BD": "Bangladesh", "BE": "Belgium",
    "BF": "Burkina Faso", "BG": "Bulgaria", "BH": "Bahrain", "BI": "Burundi", "BJ": "Benin",
    "BO": "Bolivia", "BR": "Brazil", "BT": "Bhutan", "BW": "Botswana", "BY": "Belarus",
    "BZ": "Belize", "CA": "Canada", "CD": "DR Congo", "CF": "Central Africa",
    "CG": "Congo", "CH": "Switzerland", "CI": "Ivory Coast", "CL": "Chile", "CM": "Cameroon",
    "CN": "China", "CO": "Colombia", "CR": "Costa Rica", "CU": "Cuba", "CV": "Cape Verde",
    "CY": "Cyprus", "CZ": "Czech Republic", "DE": "Germany", "DJ": "Djibouti", "DK": "Denmark",
    "DO": "Dominican Rep.", "DZ": "Algeria", "EC": "Ecuador", "EE": "Estonia", "EG": "Egypt",
    "ES": "Spain", "ET": "Ethiopia", "FI": "Finland", "FJ": "Fiji", "FR": "France",
    "GA": "Gabon", "GB": "United Kingdom", "GE": "Georgia", "GH": "Ghana", "GI": "Gibraltar",
    "GM": "Gambia", "GN": "Guinea", "GQ": "Eq. Guinea", "GR": "Greece", "GT": "Guatemala",
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
    "PA": "Panama", "PE": "Peru", "PG": "Papua NG", "PH": "Philippines", "PK": "Pakistan",
    "PL": "Poland", "PS": "Palestine", "PT": "Portugal", "PY": "Paraguay", "QA": "Qatar",
    "RO": "Romania", "RS": "Serbia", "RU": "Russia", "RW": "Rwanda", "SA": "Saudi Arabia",
    "SC": "Seychelles", "SD": "Sudan", "SE": "Sweden", "SG": "Singapore", "SI": "Slovenia",
    "SK": "Slovakia", "SL": "Sierra Leone", "SN": "Senegal", "SO": "Somalia", "SR": "Suriname",
    "ST": "Sao Tome", "SV": "El Salvador", "SY": "Syria", "SZ": "Eswatini", "TD": "Chad",
    "TG": "Togo", "TH": "Thailand", "TJ": "Tajikistan", "TL": "Timor-Leste", "TM": "Turkmenistan",
    "TN": "Tunisia", "TO": "Tonga", "TR": "Turkey", "TT": "Trinidad", "TW": "Taiwan",
    "TZ": "Tanzania", "UA": "Ukraine", "UG": "Uganda", "US": "United States", "UY": "Uruguay",
    "UZ": "Uzbekistan", "VC": "Saint Vincent", "VE": "Venezuela", "VN": "Vietnam", "VU": "Vanuatu",
    "WS": "Samoa", "XK": "Kosovo", "YE": "Yemen", "ZA": "South Africa", "ZM": "Zambia", "ZW": "Zimbabwe"
}

def get_country_full_name(iso_code: str, item: Optional[Dict[str, Any]] = None) -> str:
    iso = (iso_code or "").strip().upper()
    if iso in ISO_TO_COUNTRY_NAME:
        return ISO_TO_COUNTRY_NAME[iso]
    if item:
        raw_range = str(item.get("range_name") or item.get("rangeName") or item.get("destinationName") or "").strip()
        if raw_range:
            parts = re.split(r"[-–—,:/]", raw_range)
            cand = parts[0].strip().title()
            if cand and len(cand) > 2:
                if len(cand) > 15:
                    cand = cand[:14].rstrip() + "."
                return cand
    return "Global"

# ==========================================
# 10. Clean OTP Notification Formatter (No Language)
# Professional label matching the card's icon alignment (column 0)
EXPANDABLE_SMS_LABEL = "📩 <b>View Full SMS:</b>"
# Centered collapsed hint without icon (calibrated to exact optical center between left bar and quote mark)
COLLAPSED_SMS_HINT = "\u2800" * 7 + "  " + "<b>Tap to read...</b>"
# Symmetrical card divider (26 characters) to ensure cards have unified horizontal width across devices
CARD_DIVIDER = "━" * 20
QUOTE_DIVIDER = "━" * 18

def style_sms_with_copyable_otp(msg: str, otp: str, width: int = 22) -> str:
    """
    Wraps SMS lines cleanly and highlights ONLY the OTP code inside a copyable <code> tag,
    leaving the rest of the SMS as plain text so tapping copies ONLY the OTP code.
    Handles exact match, hyphenated variations (e.g. 492-810 vs 492810), and spaced variations.
    """
    if not msg:
        return ""
    lines = []
    for paragraph in str(msg).splitlines():
        p = paragraph.strip()
        if p:
            lines.extend(textwrap.wrap(p, width=width, break_long_words=True))
        else:
            lines.append("")
    wrapped = "\n".join(lines)
    escaped = html.escape(wrapped, quote=False)
    if otp:
        raw_otp = otp.strip()
        esc_otp = html.escape(raw_otp, quote=False)
        if esc_otp and esc_otp in escaped:
            escaped = escaped.replace(esc_otp, f"<code>{esc_otp}</code>", 1)
        else:
            digits = re.sub(r'\D', '', raw_otp)
            if digits and len(digits) >= 3:
                pattern = re.compile(r'\b' + r'[-\s]?'.join(re.escape(d) for d in digits) + r'\b')
                m = pattern.search(escaped)
                if m:
                    matched_str = m.group(0)
                    escaped = escaped[:m.start()] + f"<code>{matched_str}</code>" + escaped[m.end():]
                elif esc_otp.replace("-", "") in escaped:
                    raw_c = esc_otp.replace("-", "")
                    escaped = escaped.replace(raw_c, f"<code>{raw_c}</code>", 1)
    return escaped

def wrap_sms_for_card(text: str, width: int = 22) -> str:
    """Wraps SMS text to a compact line length (<=22 chars) to ensure cards don't stretch horizontally."""
    if not text:
        return ""
    lines = []
    for paragraph in str(text).splitlines():
        p = paragraph.strip()
        if p:
            lines.extend(textwrap.wrap(p, width=width, break_long_words=True))
        else:
            lines.append("")
    return "\n".join(lines)

def format_otp_notification(item: Dict[str, Any], sms_format: Optional[str] = None) -> tuple:
    """Returns (text, otp_code, raw_message) with a fully professional layout."""
    if not sms_format:
        try:
            sms_format = load_stored_data().get("sms_format", "long")
        except Exception:
            sms_format = "long"
    sms_format = str(sms_format).lower().strip()

    raw_source    = str(item.get("sender_id") or item.get("sourceAddress") or item.get("source") or item.get("caller") or item.get("sender") or "SMS Service").strip()
    raw_number    = str(item.get("number") or item.get("destinationNumber") or item.get("range_template") or item.get("rangeTemplate") or item.get("template") or item.get("dst") or "")
    masked_number = html.escape(mask_phone_number(raw_number)) if raw_number else ""
    raw_message   = str(item.get("sms") or item.get("messageBody") or item.get("message") or item.get("text") or item.get("body") or item.get("otp") or "")
    otp_code      = extract_otp_code(raw_message)

    country_iso   = get_country_iso_display(item).strip()
    parts = country_iso.split(maxsplit=1)
    flag  = parts[0] if parts else "🌐"
    iso   = parts[1] if len(parts) > 1 else "XX"
    country_name  = get_country_full_name(iso, item)

    if iso != "XX" and country_name and country_name != "Global":
        country_display = f"{flag} <b>{html.escape(country_name)} ({iso})</b>"
    elif country_name and country_name != "Global":
        country_display = f"{flag} <b>{html.escape(country_name)}</b>"
    elif iso != "XX":
        country_display = f"{flag} <b>{iso}</b>"
    else:
        country_display = f"{flag} <b>Global</b>"

    # Smart service detection
    low_source = raw_source.lower()
    low_msg    = raw_message.lower()
    wa_keywords = ["whatsapp", "‏واتساب‏", "واتساب", "ватсап", "wa code", "wa.me"]
    tg_keywords = ["telegram", "telegram code", "t.me"]
    if "whatsapp" in low_source or any(k in low_msg for k in wa_keywords):
        source = "WhatsApp"
    elif "telegram" in low_source or any(k in low_msg for k in tg_keywords):
        source = "Telegram"
    elif "google" in low_source or "google" in low_msg or raw_message.startswith("G-"):
        source = "Google"
    else:
        source = html.escape(raw_source)

    # Display number / fallback to country name (safely HTML escaped)
    num_plain     = masked_number if masked_number else html.escape(country_name)
    country_plain = re.sub(r'<[^>]+>', '', country_display).strip()
    source_plain  = source  # Already safely escaped / canonical name

    clean_msg = raw_message.strip()

    # Expandable quote block: only included in Long format
    quote_block = ""
    if sms_format != "short" and clean_msg:
        # Cap SMS display length to 3000 chars to strictly stay within Telegram's 4096 limit
        display_sms = clean_msg if len(clean_msg) <= 3000 else (clean_msg[:3000] + "... [truncated]")
        styled_sms = style_sms_with_copyable_otp(display_sms, otp_code, width=22)
        quote_block = (
            f"\n\n{EXPANDABLE_SMS_LABEL}\n"
            f"<blockquote expandable>⠀\n"
            f"{COLLAPSED_SMS_HINT}\n"
            f"⠀\n"
            f"💬 <b>SMS Content:</b>\n"
            f"{styled_sms}</blockquote>"
        )

    if otp_code:
        full_text = (
            f"🎟️ <b>VERIFICATION PASS</b>\n"
            f"{CARD_DIVIDER}\n"
            f"🏷️ <b>Service:</b> {source_plain}\n"
            f"🌍 <b>Country:</b> {country_plain}\n"
            f"📱 <b>Number:</b> {num_plain}\n"
            f"{CARD_DIVIDER}\n"
            f"🔑 <b>PASSCODE:</b> <code>{html.escape(otp_code)}</code>"
            f"{quote_block}"
        )
    elif clean_msg:
        if sms_format == "short":
            full_text = (
                f"🎟️ <b>VERIFICATION PASS</b>\n"
                f"{CARD_DIVIDER}\n"
                f"🏷️ <b>Service:</b> {source_plain}\n"
                f"🌍 <b>Country:</b> {country_plain}\n"
                f"📱 <b>Number:</b> {num_plain}\n"
                f"{CARD_DIVIDER}\n"
                f"🔑 <b>PASSCODE:</b> <i>See full SMS</i>"
            )
        else:
            full_text = (
                f"🎟️ <b>VERIFICATION PASS</b>\n"
                f"{CARD_DIVIDER}\n"
                f"🏷️ <b>Service:</b> {source_plain}\n"
                f"🌍 <b>Country:</b> {country_plain}\n"
                f"📱 <b>Number:</b> {num_plain}\n"
                f"{CARD_DIVIDER}"
                f"{quote_block}"
            )
    else:
        full_text = (
            f"🎟️ <b>VERIFICATION PASS</b>\n"
            f"{CARD_DIVIDER}\n"
            f"🏷️ <b>Service:</b> {source_plain}\n"
            f"🌍 <b>Country:</b> {country_plain}\n"
            f"📱 <b>Number:</b> {num_plain}"
        )

    return full_text, otp_code, raw_message

# ==========================================
# 11. Telegram Bot Engine & Polling Loop
# ==========================================
def get_effective_base_url() -> str:
    # 1. First prioritize custom URL configured by admin in SQLite or bot_data so it never reverts
    db_custom = get_bot_setting("thirdwave_base_url", "")
    if db_custom and db_custom.strip():
        return ThirdwaveClient.clean_base_url(db_custom.strip())
    try:
        data = load_stored_data()
        stored = data.get("thirdwave_base_url") or data.get("base_url")
        if stored and str(stored).strip():
            return ThirdwaveClient.clean_base_url(str(stored).strip())
    except Exception:
        pass
    # 2. Fall back to environment variable / GitHub Secret or default
    raw = os.getenv("THIRDWAVE_BASE_URL", "").strip() or THIRDWAVE_BASE_URL or "https://thirdwave.im"
    return ThirdwaveClient.clean_base_url(raw)

def get_effective_api_key() -> str:
    # 1. First prioritize custom API key configured by admin in SQLite or bot_data so it never reverts
    db_custom = get_bot_setting("thirdwave_api_key", "")
    if db_custom and db_custom.strip():
        return db_custom.strip()
    try:
        data = load_stored_data()
        stored = data.get("thirdwave_api_key") or data.get("api_key")
        if stored and str(stored).strip():
            return str(stored).strip()
    except Exception:
        pass
    # 2. Fall back to environment variable / GitHub Secret
    raw = os.getenv("THIRDWAVE_API_KEY", "").strip() or THIRDWAVE_API_KEY
    return raw.strip()

total_forwarded_count: int = 0
country_forwarded_counts: Dict[str, int] = {}  # tracks per-country OTP count (ISO display string -> count)
client = ThirdwaveClient(
    base_url=get_effective_base_url(),
    api_key=get_effective_api_key(),
    timeout=20.0,
)

async def send_with_retry(bot: Bot, chat_id: int, text: str, max_retries: int = 3,
                          reply_markup=None) -> bool:
    safe_text = text if len(text) <= 4090 else (text[:4080] + "...")
    for attempt in range(1, max_retries + 1):
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=safe_text,
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
        except BadRequest as b_err:
            # HTML parse error or entity mismatch: fall back to sending plain text so OTP is never lost!
            logger.warning(f"⚠️ Telegram BadRequest ({b_err}) for {chat_id}. Falling back to plain text delivery...")
            try:
                plain_text = re.sub(r'<[^>]+>', '', safe_text)
                await bot.send_message(
                    chat_id=chat_id,
                    text=plain_text,
                    reply_markup=reply_markup,
                    read_timeout=30.0,
                    write_timeout=30.0,
                    connect_timeout=30.0,
                )
                return True
            except Exception as fb_err:
                logger.error(f"❌ Plain text fallback also failed for {chat_id}: {fb_err}")
                return False
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
    """
    Formats and delivers one OTP item to Telegram groups.
    If OTP Forwarding is turned OFF by admin:
      - Saves the record into SQLite database (`processed_otps`) and 28h rolling retention
      - Updates receiving history, today's count, and country statistics
      - BUT does NOT send any message to Telegram groups!
    If OTP Forwarding is ON:
      - Formats and sends live OTP card to Telegram groups.
    """
    mid         = str(item.get("id") or "").strip()
    data        = load_stored_data()
    sms_format  = data.get("sms_format", "long")
    formatted_text, otp, raw_msg = format_otp_notification(item, sms_format=sms_format)

    country     = extract_country_name(str(item.get("range_name") or item.get("rangeName") or item.get("destinationName") or ""))
    raw_num     = str(item.get("number") or item.get("destinationNumber") or item.get("range_template") or item.get("rangeTemplate") or item.get("template") or item.get("dst") or "")
    num         = mask_phone_number(raw_num)
    raw_msg_str = str(item.get("sms") or item.get("messageBody") or item.get("message") or item.get("text") or item.get("body") or item.get("otp") or "")
    otp_code    = extract_otp_code(raw_msg_str)
    gid         = get_linked_group_chat_id()
    country_iso = get_country_iso_display(item)

    # Fast cache raw SMS for popup
    _cache_sms_message(mid, raw_msg_str)

    # Check whether live forwarding to groups is enabled or turned OFF
    forwarding_enabled = is_otp_forwarding_enabled()

    # Determine message arrival timestamp
    raw_time_str = str(item.get("received_at") or item.get("receivedAt") or item.get("messageTime") or item.get("createdAt") or "")
    msg_ts = parse_message_timestamp(raw_time_str)
    resumed_epoch = get_forwarding_resumed_epoch()

    # ── GUARANTEE: Never forward old OTPs that arrived while forwarding was turned OFF! ──
    # If forwarding was resumed, any OTP whose arrival time is BEFORE forwarding was turned ON
    # is recorded into SQLite & History only, and NEVER sent to the group!
    if forwarding_enabled and resumed_epoch > 0 and msg_ts > 0 and msg_ts < (resumed_epoch - 3.0):
        save_processed_message(item, gid or 0, country, num, otp_code, status="blocked_pre_resume")
        country_forwarded_counts[country_iso] = country_forwarded_counts.get(country_iso, 0) + 1
        update_last_active_timestamp()
        logger.info(
            f"🛡️ [PRE-RESUME OTP BLOCKED] Live OTP {mid} ({country_iso}) arrived during OFF period. "
            f"Saved to Database & History only (not forwarded to group)."
        )
        return True

    if not forwarding_enabled:
        # ── Group forwarding is turned OFF by admin (Silent Mode) ──────────────────
        # Per user requirement: DO NOT forward the OTP to groups!
        # BUT fully record into the database, receiving history, and 28h retention!
        save_processed_message(item, gid or 0, country, num, otp_code, status="silent_recorded")
        country_forwarded_counts[country_iso] = country_forwarded_counts.get(country_iso, 0) + 1
        update_last_active_timestamp()
        logger.info(
            f"🔕 [FORWARDING TURNED OFF] Recorded Live OTP (ID: {mid}) into Database & History [{country_iso}] "
            f"(Group sending turned OFF by admin)"
        )
        return True

    # ── Group forwarding is ON: Deliver to groups ─────────────────────────────
    markup = None
    buttons = []
    try:
        custom_buttons = get_custom_link_buttons()
        custom_btns = []
        if custom_buttons:
            for cb in custom_buttons:
                b_text = str(cb.get("text", "")).strip()
                raw_u  = str(cb.get("url", "")).strip()
                norm_u = normalize_button_url(raw_u) or raw_u
                if b_text and norm_u.startswith(("http://", "https://", "tg://")):
                    custom_btns.append(InlineKeyboardButton(b_text, url=norm_u))

        otp_btn = InlineKeyboardButton(otp, copy_text=CopyTextButton(text=otp)) if otp else None

        if otp_btn and custom_btns:
            # Show OTP button side-by-side with custom link button(s) in the same row!
            if len(custom_btns) <= 2:
                # 1 OTP + up to 2 custom buttons: all in 1 row side-by-side!
                buttons.append([otp_btn] + custom_btns)
            else:
                # 1 OTP + 1st custom button on row 1, rest in pairs below
                buttons.append([otp_btn, custom_btns[0]])
                for i in range(1, len(custom_btns), 2):
                    buttons.append(custom_btns[i:i+2])
        elif otp_btn:
            buttons.append([otp_btn])
        elif custom_btns:
            if len(custom_btns) <= 3:
                buttons.append(custom_btns)
            else:
                for i in range(0, len(custom_btns), 2):
                    buttons.append(custom_btns[i:i+2])

        if buttons:
            markup = InlineKeyboardMarkup(buttons)
    except Exception as e:
        logger.warning(f"Error building OTP message buttons: {e}")
        markup = None

    sent_to_any = False
    for cid in dest_ids:
        try:
            ok = await send_with_retry(bot, cid, formatted_text, reply_markup=markup)
            if ok:
                sent_to_any = True
        except Exception as e:
            logger.warning(f"Delivery error to {cid}: {e}")

    if sent_to_any:
        save_processed_message(item, gid or 0, country, num, otp_code, status="forwarded")
        country_forwarded_counts[country_iso] = country_forwarded_counts.get(country_iso, 0) + 1
        update_last_active_timestamp()
        logger.info(f"✅ Forwarded Live OTP (ID: {mid}) to Group [{country_iso}]")
    else:
        # Save to DB so failure doesn't loop infinitely
        save_processed_message(item, gid or 0, country, num, otp_code, status="failed_delivery")
        logger.error(f"❌ Failed to deliver OTP (ID: {mid}) to group, saved to DB.")

    return sent_to_any or True

async def run_startup_reconciliation(
    client: ThirdwaveClient,
    application: Application,
    dest_ids: List[int],
    gist_storage: Any,
    seen_message_ids: Set[str],
    seen_timestamps: Dict[str, float],
    bot_start_time: float,
    gist_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, int]:
    """
    Professional Startup Reconciliation Engine:
    - Scans the Thirdwave API traffic ledger across all available pages.
    - Strictly matches database records with API to guarantee NO duplicate forwards on session handover or git push.
    - Detects genuine missed OTPs that arrived during the restart transition gap (<= 120s) and forwards them.
    - Stores all existing API messages into SQLite database to align DB records with the API without duplicate spam.
    - Obeys the Thirdwave API rate limit of 30 req/min (2.1s delay between page fetches).
    """
    global total_forwarded_count, _gist_dirty
    now_ts = datetime.now(timezone.utc).timestamp()
    last_known_active = get_previous_session_last_epoch(gist_state)

    logger.info("=" * 70)
    logger.info("🔄 INITIATING PROFESSIONAL STARTUP RECONCILIATION")
    logger.info(f"   • Previous Session Active Epoch: {last_known_active:.0f} (gap: {now_ts - last_known_active:.1f}s)")
    logger.info("   • Rate Limit Protection: 30 req/min (2.1s inter-page throttling)")
    logger.info("   • Duplicate Guard: Ironclad multi-check (ID, Phone+OTP, Phone+Body, & Active Epoch)")
    logger.info("=" * 70)

    # ── STEP 1: Query recent traffic feed from Thirdwave API ───────────────────
    logger.info("📡 [API Scan] Fetching recent traffic ledger from Thirdwave API (limit=60)...")
    all_api_messages = await client.fetch_incoming_messages(limit=60)
    logger.info(f"📥 [API Scan] Retrieved {len(all_api_messages)} recent messages from Thirdwave API.")

    # ── STEP 2: Cross-reference with database & sort chronologically ───────────
    def _extract_ts(m: Dict[str, Any]) -> float:
        return parse_message_timestamp(
            str(m.get("received_at") or m.get("receivedAt") or
                m.get("messageTime") or m.get("createdAt") or "")
        )

    # Process oldest messages first so they are stored / delivered in chronological order
    all_api_messages.sort(key=_extract_ts)

    already_in_db_count   = 0
    fresh_forwarded_count = 0
    stale_archived_count  = 0

    gid = get_linked_group_chat_id() or 0

    for item in all_api_messages:
        mid = str(item.get("id") or "").strip()
        if not mid:
            continue

        # Ironclad duplicate check: ID match, (number + OTP) match, or (number + SMS body) match
        if is_message_duplicate(mid, item):
            seen_message_ids.add(mid)
            already_in_db_count += 1
            continue

        # Message is NOT in DB -> Evaluate age
        msg_ts = _extract_ts(item)
        cutoff_28h = now_ts - (28 * 3600)
        if msg_ts > 0 and msg_ts < cutoff_28h:
            # Older than 28 hours -> ignore completely
            seen_message_ids.add(mid)
            seen_timestamps[mid] = msg_ts
            continue

        country = extract_country_name(str(item.get("range_name") or item.get("rangeName") or item.get("destinationName") or ""))
        raw_num = str(item.get("number") or item.get("destinationNumber") or item.get("range_template") or item.get("rangeTemplate") or item.get("dst") or "")
        num     = mask_phone_number(raw_num)
        raw_msg = str(item.get("sms") or item.get("messageBody") or item.get("message") or item.get("text") or item.get("body") or item.get("otp") or "")
        otp     = extract_otp_code(raw_msg)

        # Check 2: Evaluate whether this message is genuinely MISSED during restart transition
        # A message is genuinely missed ONLY if:
        # 1. It arrived AFTER the previous session last forwarded (msg_ts > last_known_active), AND
        # 2. It arrived recently within the restart transition gap (<= 120s old)
        is_genuinely_missed = False
        if last_known_active > 0:
            if msg_ts > last_known_active and (now_ts - msg_ts) <= 120:
                is_genuinely_missed = True
        else:
            # Fresh initial start or completely wiped cache:
            # Only forward messages that arrived in the very last 45 seconds
            if msg_ts > 0 and (now_ts - msg_ts) <= 45:
                is_genuinely_missed = True

        if is_genuinely_missed and (dest_ids or not is_otp_forwarding_enabled()):
            # Deliver genuinely missed OTP (or record silently if forwarding is off)
            ok = await _deliver_item(application.bot, item, dest_ids)
            if ok:
                total_forwarded_count += 1
                fresh_forwarded_count += 1
                seen_message_ids.add(mid)
                seen_timestamps[mid] = now_ts
                _gist_dirty = True
                if is_otp_forwarding_enabled():
                    logger.info(
                        f"📬 [RECOVERY FORWARDED] Genuinely missed OTP {mid} ({int(now_ts - msg_ts)}s old) "
                        f"forwarded to Telegram group."
                    )
                else:
                    logger.info(
                        f"🔕 [RECOVERY SILENT] Recorded missed OTP {mid} into DB & History (Group forwarding turned OFF)."
                    )
                await asyncio.sleep(0.5)
        else:
            # Already forwarded previously or older than restart gap:
            # Match database with API by recording into SQLite without re-sending!
            save_processed_message(item, gid, country, num, otp, status="matched_existing")
            seen_message_ids.add(mid)
            seen_timestamps[mid] = msg_ts if msg_ts > 0 else now_ts
            _gist_dirty = True
            stale_archived_count += 1
            logger.info(
                f"🗄️ [DB MATCHED] Synchronized API message {mid} with database (duplicate avoided)."
            )

    # ── STEP 3: Enterprise-grade Audit Report ─────────────────────────────────
    logger.info("=" * 70)
    logger.info("📊 STARTUP RECONCILIATION AUDIT REPORT")
    logger.info("=" * 70)
    logger.info(f"  • Total SMS on API Feed:   {len(all_api_messages)}")
    logger.info(f"  • Already In Database:     {already_in_db_count} (Skipped, duplicate guard)")
    logger.info(f"  • Fresh Missed Forwarded:  {fresh_forwarded_count} (Delivered to group)")
    logger.info(f"  • Matched with DB Only:    {stale_archived_count} (Saved to DB only, duplicates avoided)")
    logger.info("=" * 70)

    if (fresh_forwarded_count > 0 or stale_archived_count > 0) and gist_storage.enabled:
        try:
            await gist_storage.save_state(
                seen_dict=seen_timestamps,
                is_handover=False,
                total_forwarded=total_forwarded_count,
                country_counts=country_forwarded_counts,
                base_url=client.base_url,
            )
        except Exception as ge:
            logger.warning(f"Gist save error after reconciliation: {ge}")

    return {
        "total_api": len(all_api_messages),
        "already_in_db": already_in_db_count,
        "forwarded": fresh_forwarded_count,
        "archived": stale_archived_count,
    }

async def poll_incoming_messages(application: Application):
    global total_forwarded_count, _gist_dirty, _is_handover, _handover_epoch
    init_db()
    bot_start_time = datetime.now(timezone.utc).timestamp()
    logger.info(f"🚀 Thirdwave SMS Wave OTP Bot polling engine started at epoch {bot_start_time:.0f}.")

    gist_state = {}
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
            logger.info(
                f"🔄 Zero-Restart Handover Active: {len(gist_seen)} messages restored, "
                f"last epoch {_handover_epoch:.0f}."
            )

        # Restore bot configuration settings secretly from cloud database
        saved_bot_data = gist_state.get("bot_data")
        if isinstance(saved_bot_data, dict) and saved_bot_data:
            local_data = load_stored_data()
            if saved_bot_data.get("custom_link_buttons_cleared") or saved_bot_data.get("custom_link_buttons") == []:
                local_data["custom_link_buttons"] = []
                local_data["custom_link_buttons_cleared"] = True
                local_data.pop("custom_link_button", None)
                delete_bot_setting("custom_link_button")
                delete_bot_setting("custom_link_button_url")
                delete_bot_setting("custom_link_button_text")
                set_bot_setting("custom_link_buttons", "[]")
                set_bot_setting("custom_link_buttons_cleared", "1")
            # Restore custom API URL and Key if configured by admin
            custom_url = saved_bot_data.get("thirdwave_base_url") or saved_bot_data.get("base_url")
            if custom_url and "client" in globals():
                client.update_base_url(custom_url)
                set_bot_setting("thirdwave_base_url", custom_url)
            custom_key = saved_bot_data.get("thirdwave_api_key") or saved_bot_data.get("api_key")
            if custom_key and "client" in globals():
                client.update_api_key(custom_key)
                set_bot_setting("thirdwave_api_key", custom_key)
            local_data.update(saved_bot_data)
            save_stored_data(local_data)
            custom_name = local_data.get("bot_name") or local_data.get("default_bot_name")
            if custom_name:
                try:
                    await application.bot.set_my_name(name=custom_name)
                    logger.info(f"🤖 Applied custom bot name from secret database: {custom_name}")
                except Exception:
                    pass

    # 2. Preload seen IDs and seed country stats from local DB (28h rolling)
    try:
        cleanup_old_messages(max_age_hours=28)
        cutoff_epoch = datetime.now(timezone.utc).timestamp() - (28 * 3600)
        cutoff_iso = datetime.fromtimestamp(cutoff_epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with get_db_connection() as conn:
            for row in conn.execute(
                """SELECT id, number, otp_code, raw_message, received_at_epoch, forwarded_at 
                   FROM processed_otps 
                   WHERE datetime(forwarded_at) >= datetime(?) 
                      OR (expires_at_epoch IS NOT NULL AND expires_at_epoch > strftime('%s', 'now'))
                   ORDER BY forwarded_at DESC LIMIT 10000;""",
                (cutoff_iso,)
            ):
                mid = str(row["id"])
                seen_message_ids.add(mid)
                fwd_str = str(row["forwarded_at"] or "")
                t_val = float(row["received_at_epoch"] or 0.0) or parse_message_timestamp(fwd_str) or bot_start_time
                if mid not in seen_timestamps:
                    seen_timestamps[mid] = t_val

                num_digits = re.sub(r"\D", "", str(row["number"] or ""))
                otp_c = str(row["otp_code"] or "")
                raw_m = str(row["raw_message"] or "").strip()
                if num_digits and otp_c:
                    seen_content_signatures[f"otp:{num_digits}:{otp_c}"] = t_val
                if num_digits and raw_m:
                    clean_b = re.sub(r"\s+", " ", raw_m).strip()
                    seen_content_signatures[f"body:{num_digits}:{clean_b}"] = t_val

            # Seed active country counts from database
            try:
                today_total, total_28h, active_countries = get_dashboard_stats()
                total_forwarded_count = today_total
                country_forwarded_counts.clear()
                country_forwarded_counts.update(active_countries)
            except Exception as e:
                logger.debug(f"Country seeding note: {e}")
        logger.info(f"Preloaded {len(seen_message_ids)} total seen message keys & {len(seen_content_signatures)} signatures from persistent database.")
    except Exception as e:
        logger.warning(f"Preload error: {e}")

    # 3. Startup pass: Professional full API reconciliation with rate-limit compliance
    try:
        dest_ids = _get_otp_dest_ids()
        await run_startup_reconciliation(
            client=client,
            application=application,
            dest_ids=dest_ids,
            gist_storage=gist_storage,
            seen_message_ids=seen_message_ids,
            seen_timestamps=seen_timestamps,
            bot_start_time=bot_start_time,
            gist_state=gist_state,
        )
    except Exception as e:
        logger.warning(f"Startup reconciliation error: {e}")

    # ── LIVE POLLING LOOP ── runs forever, never crashes ──────────────────────
    while True:
        try:
            dest_ids = _get_otp_dest_ids()
            # Fetch incoming traffic continuously to keep database & receiving history 100% updated
            messages  = await client.fetch_incoming_messages()
            new_items = []
            for m in messages:
                mid = str(m.get("id") or "").strip()
                if not mid or is_message_duplicate(mid, m):
                    continue
                # Timestamp check: ignore any SMS older than bot startup time or older than 28 hours
                msg_ts = parse_message_timestamp(str(m.get("received_at") or m.get("receivedAt") or m.get("messageTime") or m.get("createdAt") or ""))
                cutoff_28h = datetime.now(timezone.utc).timestamp() - (28 * 3600)
                if msg_ts > 0 and (msg_ts < (bot_start_time - 15.0) or msg_ts < cutoff_28h):
                    seen_message_ids.add(mid)
                    seen_timestamps[mid] = msg_ts
                    continue
                new_items.append(m)
                seen_message_ids.add(mid)
                ts_seen = msg_ts if msg_ts > 0 else datetime.now(timezone.utc).timestamp()
                seen_timestamps[mid] = ts_seen
                raw_n = str(m.get("number") or m.get("destinationNumber") or m.get("range_template") or m.get("rangeTemplate") or m.get("dst") or "")
                dig = re.sub(r"\D", "", raw_n)
                raw_m_text = str(m.get("sms") or m.get("messageBody") or m.get("message") or m.get("text") or m.get("body") or m.get("otp") or "").strip()
                otp_c = extract_otp_code(raw_m_text)
                if dig and otp_c:
                    seen_content_signatures[f"otp:{dig}:{otp_c}"] = ts_seen
                if dig and raw_m_text:
                    clean_b = re.sub(r"\s+", " ", raw_m_text).strip()
                    seen_content_signatures[f"body:{dig}:{clean_b}"] = ts_seen
                _gist_dirty = True

            if new_items:
                logger.info(f"🔔 {len(new_items)} new SMS/OTP(s) detected!")
                for item in reversed(new_items):  # oldest first
                    ok = await _deliver_item(application.bot, item, dest_ids)
                    if ok:
                        total_forwarded_count += 1
                    # Ultra-fast micro-pause so each OTP is sent as a clearly separate new message
                    await asyncio.sleep(0.05)

        except (TimedOut, NetworkError) as net_err:
            logger.warning(f"⚠️ Network hiccup: {net_err}. Retrying in 2s...")
            await asyncio.sleep(2.0)
        except Exception as e:
            logger.error(f"⚠️ Polling error: {e}. Continuing in 2s...")
            await asyncio.sleep(2.0)

        await asyncio.sleep(max(9.0, POLL_INTERVAL_SECONDS))

# ==========================================
# 12. Bot Commands & Admin Control Panel
# ==========================================
_TELEGRAM_BOT_NAME: str = "SMS WAVE"

def set_cached_telegram_name(name: str):
    global _TELEGRAM_BOT_NAME
    if name and isinstance(name, str) and name.strip():
        _TELEGRAM_BOT_NAME = name.strip()[:64]

def get_default_bot_name() -> str:
    data = load_stored_data()
    val = data.get("default_bot_name")
    if val and isinstance(val, str) and val.strip():
        return val.strip()[:64]
    env_name = os.getenv("BOT_NAME", "").strip()
    if env_name:
        return env_name[:64]
    if _TELEGRAM_BOT_NAME:
        return _TELEGRAM_BOT_NAME[:64]
    return "SMS WAVE"

def get_bot_name() -> str:
    data = load_stored_data()
    val = data.get("bot_name")
    if val and isinstance(val, str) and val.strip():
        return val.strip()[:64]
    return get_default_bot_name()

def build_status_dashboard() -> Tuple[str, InlineKeyboardMarkup]:
    group_ids = get_target_group_chat_ids()
    total_today, total_28h, country_counts_28h = get_dashboard_stats()
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
        handover_info = f"{h_hours}h {h_mins}m remaining (Auto-Sync 🔄)"
    else:
        handover_info = "Always-Online (Continuous)"

    # Format & Link button status
    data = load_stored_data()
    active_bot_name = get_bot_name()
    active_format = data.get("sms_format", "long")
    fmt_label = "Short (Modern ⚡)" if active_format == "short" else "Long (Detailed 📜)"

    custom_buttons = get_custom_link_buttons()
    if custom_buttons:
        link_items = [html.escape(b['text']) for b in custom_buttons]
        link_str = f"<b>{len(custom_buttons)} Active:</b> " + ", ".join(link_items)
    else:
        link_str = "<i>None configured</i>"

    # Build per-country breakdown (sorted by count desc, top 15) strictly from active 28h records
    country_lines = ""
    if country_counts_28h:
        sorted_countries = sorted(country_counts_28h.items(), key=lambda x: x[1], reverse=True)
        country_lines = "\n🌍 <b>OTPs by Country (28h Rolling):</b>\n"
        for idx, (iso_display, cnt) in enumerate(sorted_countries[:15], 1):
            country_lines += f"  {idx}. {iso_display} — {cnt}\n"
        country_lines += "━━━━━━━━━━━━━━━━━━━━\n"

    gist_status = f"Encrypted Cloud DB ({GIST_ID[:8]}...) 🔒" if gist_storage.enabled else "Local Storage"
    masked_key = f"{client.api_key[:8]}...{client.api_key[-4:]}" if len(client.api_key) > 12 else (client.api_key or "<i>Missing</i>")
    effective_poll = max(9.0, POLL_INTERVAL_SECONDS)

    fwd_active = is_otp_forwarding_enabled()
    fwd_label = "🟢 ACTIVE (Live Mode)" if fwd_active else "⏸️ PAUSED (Silent Mode • DB & History Only)"
    bottom_note = "<i>🔔 Real-time forwarding active — zero restarts, always on.</i>" if fwd_active else "<i>🔕 Group forwarding is paused by admin — recording OTPs to DB & history silently.</i>"

    msg = (
        f"⚡ <b>{html.escape(active_bot_name)} • LIVE & ONLINE</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 <b>Engine:</b>      100% Online ✅\n"
        f"⏱️ <b>Uptime:</b>      {uptime_str}\n"
        f"🔄 <b>Handover:</b>    {handover_info}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 <b>API Base:</b>    <code>{html.escape(client.base_url)}</code>\n"
        f"🔑 <b>API Key:</b>     {masked_key}\n"
        f"🗄️ <b>Database:</b>   {gist_status}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>Groups:</b>      {group_text}\n"
        f"👑 <b>Admins:</b>      {len(get_all_admin_ids())} active\n"
        f"⚡ <b>Forwarding:</b>  {fwd_label}\n"
        f"📨 <b>OTPs Today:</b>  {total_today} received\n"
        f"📦 <b>DB Records:</b>  {total_28h} (28h rolling)\n"
        f"⚡ <b>Poll Rate:</b>   every {effective_poll:.1f}s\n"
        f"📋 <b>SMS Format:</b>  {fmt_label}\n"
        f"🔗 <b>Buttons:</b>     {link_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{country_lines}"
        f"{bottom_note}"
    )

    keyboard = [
        [InlineKeyboardButton("🔄 Refresh Status", callback_data="refresh_dash")],
    ]
    return msg, InlineKeyboardMarkup(keyboard)

def get_admin_reply_keyboard() -> ReplyKeyboardMarkup:
    """
    Bottom persistent reply keyboard for admins.
    Row 1: 📊 Status | ⚡ OTP Forwarding
    Row 2: 📋 SMS Format | 👥 Linked Groups
    Row 3: 🌐 API URL | 🔑 API Key
    Row 4: ✏️ Bot Name | 🔗 Link Button
    Row 5: 👑 Admins | 🔄 Restart
    """
    keyboard = [
        [KeyboardButton("📊 Status"), KeyboardButton("⚡ OTP Forwarding")],
        [KeyboardButton("📋 SMS Format"), KeyboardButton("👥 Linked Groups")],
        [KeyboardButton("🌐 API URL"), KeyboardButton("🔑 API Key")],
        [KeyboardButton("✏️ Bot Name"), KeyboardButton("🔗 Link Button")],
        [KeyboardButton("👑 Admins"), KeyboardButton("🔄 Restart")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)

def get_bot_commands() -> List[BotCommand]:
    """Returns the slash commands for the Telegram [≡ Menu] command button."""
    return [
        BotCommand("start", "🚀 Admin Dashboard"),
    ]

async def setup_admin_menu_for_chat(bot: Bot, chat_id: int):
    """Locks [≡ Menu] button and registers slash commands for an admin private chat."""
    if not chat_id or chat_id <= 0:
        return
    try:
        cmds = get_bot_commands()
        await bot.set_my_commands(cmds, scope=BotCommandScopeChat(chat_id))
        await bot.set_chat_menu_button(chat_id=chat_id, menu_button=MenuButtonCommands())
    except Exception as e:
        logger.debug(f"Menu setup for chat {chat_id} notice: {e}")

def build_forwarding_control_view() -> Tuple[str, InlineKeyboardMarkup]:
    """Builds interactive configuration view for OTP group forwarding."""
    is_fwd = is_otp_forwarding_enabled()
    if is_fwd:
        msg = (
            "⚡ <b>OTP Group Forwarding Control</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "• <b>Status:</b> 🟢 ACTIVE (Live Mode)\n"
            "• <b>Group Delivery:</b> ON (Live Delivery)\n"
            "• <b>Database & Retention:</b> 100% Synced (28h)\n"
            "• <b>Synced:</b> Cloud DB 🔒\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Tap below to pause group forwarding:</i>"
        )
        toggle_btn = InlineKeyboardButton("⏸️ Pause Forwarding", callback_data="fwd_ask_off")
    else:
        msg = (
            "⚡ <b>OTP Group Forwarding Control</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "• <b>Status:</b> ⏸️ PAUSED (Silent Mode)\n"
            "• <b>Group Delivery:</b> PAUSED (No messages sent)\n"
            "• <b>Database & Retention:</b> 100% Active (Recording silently)\n"
            "• <b>Synced:</b> Cloud DB 🔒\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Tap below to resume group forwarding:</i>"
        )
        toggle_btn = InlineKeyboardButton("▶️ Resume Forwarding", callback_data="fwd_ask_on")

    return msg, InlineKeyboardMarkup([[toggle_btn]])

def build_forwarding_updated_view(action: str) -> Tuple[str, InlineKeyboardMarkup]:
    """Builds professional status update confirmation card after forwarding state is changed."""
    if action == "off":
        msg = (
            "⏸️ <b>OTP Forwarding: Paused</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "• <b>Status:</b> ⏸️ PAUSED (Silent Mode)\n"
            "• <b>Group Delivery:</b> PAUSED (No messages sent to groups)\n"
            "• <b>Database & Retention:</b> 100% Active (Recording silently)\n"
            "• <b>Synced:</b> Cloud DB 🔒\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Incoming OTPs continue recording silently in SQLite & 28h history. Groups will not receive forwards until resumed.</i>"
        )
        btn = InlineKeyboardButton("▶️ Resume Forwarding", callback_data="fwd_ask_on")
    else:
        msg = (
            "🟢 <b>OTP Forwarding: Resumed</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "• <b>Status:</b> 🟢 ACTIVE (Live Mode)\n"
            "• <b>Group Delivery:</b> ACTIVE (Delivered live to groups)\n"
            "• <b>Database & Retention:</b> 100% Synced (28h)\n"
            "• <b>Zero Spam Shield:</b> Active (Pause OTPs suppressed)\n"
            "• <b>Synced:</b> Cloud DB 🔒\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Live forwarding is active. Every new incoming OTP is delivered to your group(s) in real time.</i>"
        )
        btn = InlineKeyboardButton("⏸️ Pause Forwarding", callback_data="fwd_ask_off")
    return msg, InlineKeyboardMarkup([[btn]])

async def toggle_forwarding_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command with confirmation prompt to toggle OTP group forwarding ON or OFF, matching restart style."""
    chat = update.effective_chat
    user = update.effective_user
    msg_obj = update.effective_message
    if not chat or not msg_obj:
        return
    user_id = user.id if user else 0
    if not is_user_authorized(user_id):
        await msg_obj.reply_text("⛔ <b>Access Restricted</b>: Admins only.", parse_mode=ParseMode.HTML)
        return
    clear_all_admin_states(context)

    # Direct force toggle if argument passed: /forwarding force
    args = context.args if context and context.args else []
    if args and args[0].lower() in ("force", "yes", "now"):
        curr = is_otp_forwarding_enabled()
        set_otp_forwarding_enabled(not curr)
        asyncio.create_task(sync_data_to_secret_db())
        msg = (
            "🟢 <b>OTP Forwarding Resumed</b>\n━━━━━━━━━━━━━━━━━━━━\nLive OTP group forwarding is active."
            if not curr else
            "⏸️ <b>OTP Forwarding Paused</b>\n━━━━━━━━━━━━━━━━━━━━\nGroup delivery is paused (recording to DB silently)."
        )
        await msg_obj.reply_text(msg, parse_mode=ParseMode.HTML)
        return

    is_fwd = is_otp_forwarding_enabled()
    if is_fwd:
        confirm_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⏸️ Pause Forwarding", callback_data="confirm_fwd_off"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_fwd"),
            ]
        ])
        await msg_obj.reply_text(
            "⏸️ <b>Pause OTP Group Forwarding</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Are you sure you want to pause OTP forwarding to Telegram groups?\n\n"
            "• Delivery to Telegram groups will be paused.\n"
            "• Bot will continue recording all incoming OTPs silently into SQLite database & 28h history.",
            parse_mode=ParseMode.HTML,
            reply_markup=confirm_kb,
        )
    else:
        confirm_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("▶️ Resume Forwarding", callback_data="confirm_fwd_on"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_fwd"),
            ]
        ])
        await msg_obj.reply_text(
            "▶️ <b>Resume OTP Group Forwarding</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Are you sure you want to resume OTP forwarding to Telegram groups?\n\n"
            "• Delivery to Telegram groups will resume in real-time.\n"
            "• Zero Spam: Only new OTPs arriving from now on will be forwarded.",
            parse_mode=ParseMode.HTML,
            reply_markup=confirm_kb,
        )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Directly displays the live system status dashboard without sending a welcome greeting."""
    chat    = update.effective_chat
    user    = update.effective_user
    msg_obj = update.effective_message
    if not chat or not msg_obj:
        return
    user_id = user.id if user else 0
    if not is_user_authorized(user_id):
        await msg_obj.reply_text("⛔ <b>Access Restricted</b>: Admins only.", parse_mode=ParseMode.HTML)
        return
    clear_all_admin_states(context)
    # Ensure command menu button and slash commands are permanently active
    asyncio.create_task(setup_admin_menu_for_chat(context.bot, chat.id))
    msg, markup = build_status_dashboard()
    await msg_obj.reply_text(
        msg,
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
        disable_web_page_preview=True,
    )

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

    # Lock Telegram Menu Button and slash commands for admin
    asyncio.create_task(setup_admin_menu_for_chat(context.bot, chat.id))

    bot_name = get_bot_name()
    # Show welcome greeting with the persistent admin keyboard on /start
    await msg_obj.reply_text(
        f"👑 <b>Welcome, Admin!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Logged in to <b>{html.escape(bot_name)}</b> Control Panel.\n"
        f"Use the menu buttons below to manage the bot.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_admin_reply_keyboard(),
    )

def clear_all_admin_states(context: ContextTypes.DEFAULT_TYPE):
    """Clears all awaiting admin states from user_data."""
    if not context or not hasattr(context, "user_data") or not isinstance(context.user_data, dict):
        return
    context.user_data.pop("awaiting_api_url", None)
    context.user_data.pop("awaiting_api_key", None)
    context.user_data.pop("awaiting_bot_name", None)
    context.user_data.pop("awaiting_btn_action", None)
    context.user_data.pop("awaiting_grp_action", None)
    context.user_data.pop("awaiting_action", None)
    context.user_data.pop("awaiting_add_admin", None)
    context.user_data.pop("prompt_msg_id", None)
    context.user_data.pop("prompt_chat_id", None)

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to cancel any pending interactive action."""
    chat = update.effective_chat
    user = update.effective_user
    msg_obj = update.effective_message
    if not chat or not msg_obj:
        return
    if not is_user_authorized(user.id if user else 0):
        return
    clear_all_admin_states(context)
    await msg_obj.reply_text(
        "❌ <b>Operation Cancelled.</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "No changes were made. All current settings remain active.",
        parse_mode=ParseMode.HTML,
        reply_markup=get_admin_reply_keyboard(),
    )

def build_sms_format_view(preview_mode: Optional[str] = None) -> Tuple[str, InlineKeyboardMarkup]:
    """Builds interactive configuration & live preview panel for SMS delivery format."""
    data = load_stored_data()
    curr_fmt = data.get("sms_format", "long")

    short_active = "✅ (Active)" if curr_fmt == "short" else "⚪"
    long_active  = "✅ (Active)" if curr_fmt == "long" else "⚪"

    msg = (
        "📋 <b>SMS Format Configuration</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 <b>Active Mode:</b>   {'Short ⚡' if curr_fmt == 'short' else 'Long 📜'}\n\n"
        "<b>Available Modes:</b>\n"
        f"• <b>Short</b> {short_active}\n"
        "  Compact card with From / To / OTP highlighted.\n\n"
        f"• <b>Long</b> {long_active}\n"
        "  Full card with expandable '📩 View Full SMS' on the card.\n"
    )

    if preview_mode == "short":
        msg += (
            f"\n{CARD_DIVIDER}\n"
            "<b>📺 Live Preview — Short:</b>\n\n"
            "🎟️ <b>VERIFICATION PASS</b>\n"
            f"{CARD_DIVIDER}\n"
            "🏷️ <b>Service:</b> WhatsApp\n"
            "🌍 <b>Country:</b> 🇳🇵 Nepal (NP)\n"
            "📱 <b>Number:</b> +977980****456\n"
            f"{CARD_DIVIDER}\n"
            "🔑 <b>PASSCODE:</b> <code>492810</code>\n\n"
            "<i>Buttons: [ 492810 ] [ GET NUMBER ]</i>"
        )
    elif preview_mode == "long":
        sample_sms = "Your WhatsApp verification code is 492-810. Do not share this code with anyone."
        styled_sample = style_sms_with_copyable_otp(sample_sms, "492-810", width=28)
        msg += (
            f"\n{CARD_DIVIDER}\n"
            "<b>📺 Live Preview — Long:</b>\n\n"
            "🎟️ <b>VERIFICATION PASS</b>\n"
            f"{CARD_DIVIDER}\n"
            "🏷️ <b>Service:</b> WhatsApp\n"
            "🌍 <b>Country:</b> 🇳🇵 Nepal (NP)\n"
            "📱 <b>Number:</b> +977980****456\n"
            f"{CARD_DIVIDER}\n"
            "🔑 <b>PASSCODE:</b> <code>492810</code>\n\n"
            f"{EXPANDABLE_SMS_LABEL}\n"
            f"<blockquote expandable>⠀\n"
            f"{COLLAPSED_SMS_HINT}\n"
            f"⠀\n"
            f"{CARD_DIVIDER}\n"
            f"💬 <b>SMS Content:</b>\n"
            f"{styled_sample}</blockquote>\n\n"
            "<i>Buttons: [ 492810 ] [ GET NUMBER ]</i>"
        )
    else:
        msg += "\n<i>Choose an option below to set the format or preview it live:</i>"

    kb = [
        [
            InlineKeyboardButton(f"{'✅ ' if curr_fmt == 'short' else ''}⚡ Short Format", callback_data="fmt_set_short"),
            InlineKeyboardButton(f"{'✅ ' if curr_fmt == 'long' else ''}📜 Long Format", callback_data="fmt_set_long"),
        ],
        [
            InlineKeyboardButton("📺 Preview Short", callback_data="fmt_preview_short"),
            InlineKeyboardButton("📺 Preview Long", callback_data="fmt_preview_long"),
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_admin_action")
        ]
    ]
    return msg, InlineKeyboardMarkup(kb)

def build_api_url_view() -> Tuple[str, InlineKeyboardMarkup]:
    """Builds interactive configuration panel for Thirdwave API Base URL."""
    masked_url = client.base_url
    msg = (
        "🌐 <b>Thirdwave API URL Configuration</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Active Base URL:</b> <code>{html.escape(masked_url)}</code>\n"
        f"• <b>Traffic Endpoint:</b> <code>{html.escape(masked_url)}/api/v1/traffic</code>\n"
        f"• <b>Default System URL:</b> <code>https://thirdwave.im</code>\n"
        f"• <b>Cloud Database:</b> Synced & Encrypted 🔒\n\n"
        "💬 <b>How to update:</b>\n"
        "• Click <b>✏️ Enter New URL</b> below to type in chat.\n"
        "• Or send: <code>/seturl &lt;new_url&gt;</code>\n"
        "<i>Example:</i> <code>/seturl https://thirdwave.im</code>"
    )
    kb = [
        [
            InlineKeyboardButton("✏️ Enter New URL", callback_data="prompt_set_url"),
            InlineKeyboardButton("🔄 Reset to Default", callback_data="reset_url_default"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_admin_action")]
    ]
    return msg, InlineKeyboardMarkup(kb)

def build_api_key_view() -> Tuple[str, InlineKeyboardMarkup]:
    """Builds interactive configuration panel for Thirdwave API Key."""
    current_key = client.api_key
    masked_key = f"{current_key[:8]}...{current_key[-4:]}" if len(current_key) > 12 else (current_key or "<i>None</i>")
    msg = (
        "🔑 <b>Thirdwave API Key Configuration</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Active API Key:</b> <code>{masked_key}</code>\n"
        f"• <b>Status:</b> {'Configured ✅' if current_key else 'Missing ⚠️'}\n"
        f"• <b>Cloud Database:</b> Synced & Encrypted 🔒\n\n"
        "💬 <b>How to update:</b>\n"
        "• Tap <b>✏️ Enter New Key</b> below to send your new API key in chat."
    )
    kb = [
        [InlineKeyboardButton("✏️ Enter New Key", callback_data="prompt_set_key")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_admin_action")]
    ]
    return msg, InlineKeyboardMarkup(kb)

def build_bot_name_view() -> Tuple[str, InlineKeyboardMarkup]:
    """Builds interactive configuration panel for Bot Display Name."""
    curr_name = get_bot_name()
    default_name = get_default_bot_name()
    msg = (
        "✏️ <b>Bot Display Name Configuration</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Current Active Name:</b> <code>{html.escape(curr_name)}</code>\n"
        f"• <b>Default System Name:</b> <code>{html.escape(default_name)}</code>\n"
        f"• <b>Telegram Bot API:</b> Official Name Sync ✅\n"
        f"• <b>Cloud Database:</b> Synced & Encrypted 🔒\n\n"
        "💬 <b>How to update:</b>\n"
        "• Click <b>✏️ Change Bot Name</b> below to type in chat.\n"
        "• Click <b>🔄 Reset to Default</b> to restore system name."
    )
    kb = [
        [
            InlineKeyboardButton("✏️ Change Bot Name", callback_data="prompt_set_bot_name"),
            InlineKeyboardButton("🔄 Reset to Default", callback_data="reset_bot_name_default"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_admin_action")]
    ]
    return msg, InlineKeyboardMarkup(kb)

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

    # If called with an argument e.g. /format short or /format long:
    if context and context.args:
        arg = context.args[0].strip().lower()
        if arg in ("short", "long"):
            data = load_stored_data()
            data["sms_format"] = arg
            save_stored_data(data)
            asyncio.create_task(sync_data_to_secret_db())
            mode_label = "Long (Detailed with SMS Content 📜)" if arg == "long" else "Short (Modern Bullet ⚡)"
            await msg_obj.reply_text(
                f"✅ <b>SMS Delivery Format Updated!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"• <b>Active Mode:</b> <code>{mode_label}</code>\n"
                f"• <b>Format Setting:</b> <code>{html.escape(arg)}</code>\n"
                f"• <b>Database:</b> <code>Saved Secretly to Cloud Store 🔒</code>",
                parse_mode=ParseMode.HTML
            )
            return

    # Otherwise show interactive professional menu
    clear_all_admin_states(context)
    text, markup = build_sms_format_view()
    await msg_obj.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)



def build_buttons_list_view() -> Tuple[str, InlineKeyboardMarkup]:
    """Builds the main custom link buttons management menu view."""
    buttons = get_custom_link_buttons()
    if not buttons:
        msg = (
            "🗂️ <b>Custom Link Buttons Management</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "ℹ️ <i>No custom buttons are currently configured.</i>\n\n"
            "Custom buttons appear side-by-side on all forwarded group OTP messages.\n\n"
            "Tap <b>➕ Add New Button</b> below to create one, or use:\n"
            "<code>/setlink Button Name | https://yourlink.com</code>"
        )
        kb = [
            [InlineKeyboardButton("➕ Add New Button", callback_data="link_add_prompt")],
            [InlineKeyboardButton("🔄 Refresh List", callback_data="link_list")],
        ]
        return msg, InlineKeyboardMarkup(kb)

    msg = (
        f"🗂️ <b>Custom Link Buttons Management</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Total Active Buttons:</b> <code>{len(buttons)}</code>\n"
        f"• <b>Display Mode:</b> <code>Side-by-Side in Group OTPs ⚡</code>\n\n"
        f"Select a button below to edit its name, change link, or remove it:"
    )
    kb = []
    for b in buttons:
        kb.append([InlineKeyboardButton(f"🔗 {b['text']}", callback_data=f"link_view_{b['id']}")])

    kb.append([
        InlineKeyboardButton("➕ Add New Button", callback_data="link_add_prompt"),
        InlineKeyboardButton("🗑️ Clear All", callback_data="link_clear_all_confirm"),
    ])
    kb.append([
        InlineKeyboardButton("🔄 Refresh List", callback_data="link_list"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel_admin_action"),
    ])
    return msg, InlineKeyboardMarkup(kb)

def build_button_detail_view(btn_id: str) -> Tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
    """Builds detail/edit view for a single custom link button."""
    buttons = get_custom_link_buttons()
    found = next((b for b in buttons if str(b["id"]) == str(btn_id)), None)
    if not found:
        return None, None

    msg = (
        f"⚙️ <b>Manage Custom Button #{found['id']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Button Label:</b> <code>{html.escape(found['text'])}</code>\n"
        f"• <b>Target URL:</b> <code>{html.escape(found['url'])}</code>\n\n"
        f"Choose an option below to modify or remove this button:"
    )
    kb = [
        [
            InlineKeyboardButton("✏️ Change Name", callback_data=f"link_rename_{found['id']}"),
            InlineKeyboardButton("🔗 Change Link", callback_data=f"link_reurl_{found['id']}"),
        ],
        [
            InlineKeyboardButton("🗑️ Remove Button", callback_data=f"link_del_confirm_{found['id']}"),
        ],
        [
            InlineKeyboardButton("◀️ Back to List", callback_data="link_list"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_admin_action"),
        ]
    ]
    return msg, InlineKeyboardMarkup(kb)

def build_button_delete_confirm_view(btn_id: str) -> Tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
    """Builds delete confirmation view for a single custom link button."""
    buttons = get_custom_link_buttons()
    found = next((b for b in buttons if str(b["id"]) == str(btn_id)), None)
    if not found:
        return None, None

    msg = (
        f"⚠️ <b>Confirm Button Removal</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Are you sure you want to remove this button?\n\n"
        f"• <b>Button:</b> <code>{html.escape(found['text'])}</code>\n"
        f"• <b>URL:</b> <code>{html.escape(found['url'])}</code>\n\n"
        f"It will be removed immediately from all group OTP messages."
    )
    kb = [
        [
            InlineKeyboardButton("✅ Yes, Delete", callback_data=f"link_del_execute_{found['id']}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"link_view_{found['id']}"),
        ]
    ]
    return msg, InlineKeyboardMarkup(kb)

def build_buttons_clear_all_confirm_view() -> Tuple[str, InlineKeyboardMarkup]:
    """Builds confirmation dialog for clearing all custom buttons."""
    buttons = get_custom_link_buttons()
    msg = (
        f"⚠️ <b>Confirm Clear All Buttons</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Are you sure you want to remove <b>ALL ({len(buttons)})</b> custom buttons?\n\n"
        f"This action cannot be undone. OTP messages will only display the OTP copy button."
    )
    kb = [
        [
            InlineKeyboardButton("⚠️ Yes, Clear All", callback_data="link_clear_all_execute"),
            InlineKeyboardButton("❌ Cancel", callback_data="link_list"),
        ]
    ]
    return msg, InlineKeyboardMarkup(kb)

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
    if not raw_args:
        text, markup = build_buttons_list_view()
        await msg_obj.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    if "|" in raw_args:
        parts = raw_args.split("|", 1)
        btn_text = parts[0].strip()
        raw_url  = parts[1].strip()
    else:
        parts = raw_args.split()
        if len(parts) >= 2:
            raw_url = parts[-1].strip()
            btn_text = " ".join(parts[:-1]).strip()
        elif len(parts) == 1:
            raw_url = parts[0].strip()
            btn_text = raw_url.lstrip("@")
        else:
            text, markup = build_buttons_list_view()
            await msg_obj.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            return

    if not btn_text:
        await msg_obj.reply_text("❌ Button text cannot be empty.", parse_mode=ParseMode.HTML)
        return
    btn_url = normalize_button_url(raw_url)
    if not btn_url:
        await msg_obj.reply_text("❌ Invalid link or bot username. Use <code>https://...</code> or <code>@bot_username</code>.", parse_mode=ParseMode.HTML)
        return

    buttons = get_custom_link_buttons()
    existing = next((b for b in buttons if b["text"].lower() == btn_text.lower()), None)
    if existing:
        existing["url"] = btn_url
        action_note = "Updated existing button URL"
        active_id = existing["id"]
    else:
        new_id = str(len(buttons) + 1)
        buttons.append({"id": new_id, "text": btn_text, "url": btn_url})
        action_note = "Added new button"
        active_id = new_id

    save_custom_link_buttons(buttons)
    _, detail_kb = build_button_detail_view(active_id)
    await msg_obj.reply_text(
        f"✅ <b>Custom Link Button Configured!</b> ({action_note})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Button Label:</b> <code>{html.escape(btn_text)}</code>\n"
        f"• <b>Target URL:</b> <code>{html.escape(btn_url)}</code>\n"
        f"• <b>Total Active Buttons:</b> <code>{len(buttons)}</code> (displayed side-by-side in group OTPs)\n"
        f"• <b>Database:</b> <code>Saved Secretly to Cloud Store 🔒</code>\n\n"
        f"Every forwarded SMS will now include all active buttons side-by-side.",
        parse_mode=ParseMode.HTML,
        reply_markup=detail_kb
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

    raw_arg = (" ".join(context.args).strip() if context.args else "").lower()

    if raw_arg in ("all", "clear", "reset"):
        save_custom_link_buttons([])
        await msg_obj.reply_text("✅ <b>All custom link buttons removed.</b> SMS messages will no longer show extra buttons.", parse_mode=ParseMode.HTML)
        return

    if raw_arg:
        buttons = get_custom_link_buttons()
        matching = [b for b in buttons if str(b["id"]) == raw_arg or b["text"].lower() == raw_arg]
        if matching:
            target = matching[0]
            new_list = [b for b in buttons if b["id"] != target["id"]]
            save_custom_link_buttons(new_list)
            await msg_obj.reply_text(
                f"✅ <b>Removed button:</b> <code>{html.escape(target['text'])}</code>\n"
                f"Remaining buttons: <code>{len(new_list)}</code>",
                parse_mode=ParseMode.HTML
            )
            return

    # No arguments: open interactive management dashboard
    text, markup = build_buttons_list_view()
    await msg_obj.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)

def build_groups_list_view() -> Tuple[str, InlineKeyboardMarkup]:
    """Builds interactive management menu for all linked forwarding groups with strict 1-10 limits."""
    all_targets = get_target_group_chat_ids()
    dynamic_groups = get_dynamic_linked_groups()
    total_count = len(all_targets)

    # Progress capacity indicator
    filled = min(total_count, 10)
    progress_bar = "🟩" * filled + "⬜" * (10 - filled)

    msg = (
        "👥 <b>Linked Forwarding Groups Management</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Active Forwarding Groups:</b> {total_count} / {MAX_FORWARDING_GROUPS}\n"
        f"• <b>Group Limits:</b> Min: {MIN_FORWARDING_GROUPS} | Max: {MAX_FORWARDING_GROUPS}\n"
        f"• <b>Capacity:</b> [{progress_bar}]\n"
        f"• <b>Forwarding Mode:</b> Simultaneous Multi-Group ⚡\n\n"
        "<b>Currently Active Forwarding Groups:</b>\n"
    )
    if all_targets:
        for idx, gid in enumerate(all_targets, 1):
            is_dyn = gid in dynamic_groups
            tag = "Admin Added ⚡" if is_dyn else "Server Secret 🔒"
            msg += f"  <b>{idx}.</b> <code>{gid}</code> ({tag})\n"
    else:
        msg += "  <i>⚠️ No groups configured. Bot requires at least 1 group!</i>\n"

    msg += (
        "\n━━━━━━━━━━━━━━━━━━━━\n"
        "💬 <b>Group Forwarding Controls:</b>\n"
        "• Tap <b>➕ Link New Group</b> below to send the Telegram Group Chat ID.\n"
        "• Tap <b>🗑️ Remove</b> next to any linked group to disconnect it.\n\n"
        "<i>⚡ All OTPs are forwarded simultaneously to all active groups. Only custom link buttons appear on OTP messages (group IDs are never shown as buttons). Group chat command menus are automatically cleared (0 commands).</i>"
    )

    kb = []
    # Dynamic group remove buttons
    for gid in dynamic_groups:
        kb.append([InlineKeyboardButton(f"🗑️ Remove {gid}", callback_data=f"grp_del_confirm_{gid}")])

    bottom_row = []
    if total_count < MAX_FORWARDING_GROUPS:
        bottom_row.append(InlineKeyboardButton("➕ Link New Group", callback_data="grp_add_prompt"))
    else:
        bottom_row.append(InlineKeyboardButton("🔒 Max 10 Groups Reached", callback_data="grp_max_reached"))

    # Can only clear if remaining groups >= 1
    env_count = len([g for g in all_targets if g not in dynamic_groups])
    if dynamic_groups and env_count >= MIN_FORWARDING_GROUPS:
        bottom_row.append(InlineKeyboardButton("🗑️ Clear Added", callback_data="grp_clear_confirm"))

    if bottom_row:
        kb.append(bottom_row)
    kb.append([
        InlineKeyboardButton("🔄 Refresh Groups", callback_data="grp_list"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel_admin_action"),
    ])
    return msg, InlineKeyboardMarkup(kb)

async def addgroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    msg_obj = update.effective_message
    if not chat or not msg_obj:
        return
    if not is_user_authorized(user.id if user else 0):
        await msg_obj.reply_text("⛔ <b>Access Restricted</b>: Admins only.", parse_mode=ParseMode.HTML)
        return

    arg = context.args[0].strip() if context.args else ""
    if not arg:
        text, markup = build_groups_list_view()
        await msg_obj.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    try:
        new_gid = int(arg)
    except ValueError:
        await msg_obj.reply_text("❌ <b>Invalid Group Chat ID</b>: Must be an integer (e.g. <code>-1004473973263</code>).", parse_mode=ParseMode.HTML)
        return

    all_current = get_target_group_chat_ids()
    if len(all_current) >= MAX_FORWARDING_GROUPS:
        text, markup = build_groups_list_view()
        await msg_obj.reply_text(
            f"⚠️ <b>Maximum Group Limit Reached</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"You already have <b>{len(all_current)} / {MAX_FORWARDING_GROUPS} active groups</b>.\n"
            f"The system allows a minimum of 1 and maximum of 10 groups.\n"
            f"Please remove an existing group before adding a new one.",
            parse_mode=ParseMode.HTML,
            reply_markup=markup
        )
        return

    if new_gid in all_current:
        text, markup = build_groups_list_view()
        await msg_obj.reply_text(
            f"ℹ️ <b>Group Already Linked</b>\n"
            f"Group <code>{new_gid}</code> is already in the forwarding pool.",
            parse_mode=ParseMode.HTML,
            reply_markup=markup
        )
        return

    ok, reason = add_dynamic_linked_group(new_gid)
    if not ok:
        await msg_obj.reply_text(f"⚠️ Could not add group: {reason}", parse_mode=ParseMode.HTML)
        return

    try:
        await context.bot.delete_my_commands(scope=BotCommandScopeChat(new_gid))
    except Exception:
        pass

    text, markup = build_groups_list_view()
    new_total = len(get_target_group_chat_ids())
    await msg_obj.reply_text(
        f"✅ <b>Group Linked Successfully!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Group Chat ID:</b> <code>{new_gid}</code>\n"
        f"• <b>Active Groups:</b> <code>{new_total} / {MAX_FORWARDING_GROUPS}</code> (Limit: 1 - 10)\n"
        f"• <b>Group Commands:</b> <code>Wiped from group chat menu (0 commands) 🔒</code>\n"
        f"• <b>Forwarding:</b> <code>Real-Time Multi-Group Active ⚡</code>\n"
        f"• <b>Cloud DB:</b> <code>Synced ☁️</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=markup
    )

async def delgroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    msg_obj = update.effective_message
    if not chat or not msg_obj:
        return
    if not is_user_authorized(user.id if user else 0):
        await msg_obj.reply_text("⛔ <b>Access Restricted</b>: Admins only.", parse_mode=ParseMode.HTML)
        return

    arg = context.args[0].strip() if context.args else ""
    if not arg:
        text, markup = build_groups_list_view()
        await msg_obj.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    try:
        del_gid = int(arg)
    except ValueError:
        await msg_obj.reply_text("❌ Invalid group chat ID. Must be an integer.", parse_mode=ParseMode.HTML)
        return

    all_current = get_target_group_chat_ids()
    if len(all_current) <= MIN_FORWARDING_GROUPS:
        text, markup = build_groups_list_view()
        await msg_obj.reply_text(
            f"⚠️ <b>Minimum Group Limit Reached</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"You currently have <b>{len(all_current)} active group</b>.\n"
            f"The bot requires at least <b>1 group</b> for OTP forwarding (Limit: 1 - 10 groups).\n"
            f"You cannot remove the only remaining active group.",
            parse_mode=ParseMode.HTML,
            reply_markup=markup
        )
        return

    ok, reason = remove_dynamic_linked_group(del_gid)
    if ok:
        text, markup = build_groups_list_view()
        new_total = len(get_target_group_chat_ids())
        await msg_obj.reply_text(
            f"✅ <b>Group unlinked:</b> <code>{del_gid}</code>\n"
            f"• <b>Remaining Groups:</b> <code>{new_total} / {MAX_FORWARDING_GROUPS}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=markup
        )
    elif reason == "MIN_LIMIT_REACHED":
        await msg_obj.reply_text(
            "⚠️ <b>Minimum Group Limit</b>: You must keep at least 1 group active (Limit: 1 - 10 groups).",
            parse_mode=ParseMode.HTML
        )
    elif reason == "ENV_PROTECTED":
        await msg_obj.reply_text(
            f"🔒 Group <code>{del_gid}</code> is configured in the server environment secret.\n"
            f"To replace it, add your new group first, or update your server settings.",
            parse_mode=ParseMode.HTML
        )
    else:
        await msg_obj.reply_text(f"ℹ️ Group <code>{del_gid}</code> was not found in dynamically added groups.", parse_mode=ParseMode.HTML)

async def groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    msg_obj = update.effective_message
    if not chat or not msg_obj:
        return
    if not is_user_authorized(user.id if user else 0):
        await msg_obj.reply_text("⛔ <b>Access Restricted</b>: Admins only.", parse_mode=ParseMode.HTML)
        return
    text, markup = build_groups_list_view()
    await msg_obj.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)

async def setbtnname_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat    = update.effective_chat
    user    = update.effective_user
    msg_obj = update.effective_message
    if not chat or not msg_obj:
        return
    user_id = user.id if user else 0
    if not is_user_authorized(user_id):
        await msg_obj.reply_text("⛔ <b>Access Restricted</b>: Admins only.", parse_mode=ParseMode.HTML)
        return

    if not context.args or len(context.args) < 2:
        await msg_obj.reply_text(
            "⚠️ <b>Usage:</b> Tap <b>✏️ Rename Button</b> in the button menu to update the button name.",
            parse_mode=ParseMode.HTML
        )
        return

    target_id = context.args[0].strip()
    new_name = " ".join(context.args[1:]).strip()
    buttons = get_custom_link_buttons()
    found = next((b for b in buttons if str(b["id"]) == target_id), None)
    if not found:
        await msg_obj.reply_text(f"❌ Button ID <code>#{target_id}</code> was not found.", parse_mode=ParseMode.HTML)
        return

    old_name = found["text"]
    found["text"] = new_name
    save_custom_link_buttons(buttons)
    _, detail_kb = build_button_detail_view(found["id"])
    await msg_obj.reply_text(
        f"✅ <b>Button Renamed!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Old Name:</b> <code>{html.escape(old_name)}</code>\n"
        f"• <b>New Name:</b> <code>{html.escape(new_name)}</code>\n"
        f"• <b>URL:</b> <code>{html.escape(found['url'])}</code>\n"
        f"• <b>Synced:</b> <code>Cloud DB 🔒</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=detail_kb
    )

async def setbtnurl_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat    = update.effective_chat
    user    = update.effective_user
    msg_obj = update.effective_message
    if not chat or not msg_obj:
        return
    user_id = user.id if user else 0
    if not is_user_authorized(user_id):
        await msg_obj.reply_text("⛔ <b>Access Restricted</b>: Admins only.", parse_mode=ParseMode.HTML)
        return

    if not context.args or len(context.args) < 2:
        await msg_obj.reply_text(
            "🔗 <b>Usage:</b> <code>/setbtnurl &lt;Button ID&gt; &lt;New URL or @bot_username&gt;</code>\n"
            "<i>Example:</i> <code>/setbtnurl 1 @my_bot</code> or <code>/setbtnurl 1 https://t.me/newchannel</code>",
            parse_mode=ParseMode.HTML
        )
        return

    target_id = context.args[0].strip()
    raw_url = context.args[1].strip()
    clean_url = normalize_button_url(raw_url)
    if not clean_url:
        await msg_obj.reply_text("❌ Invalid link or bot username. Use <code>https://...</code> or <code>@bot_username</code>.", parse_mode=ParseMode.HTML)
        return
    new_url = clean_url

    buttons = get_custom_link_buttons()
    found = next((b for b in buttons if str(b["id"]) == target_id), None)
    if not found:
        await msg_obj.reply_text(f"❌ Button ID <code>#{target_id}</code> was not found.", parse_mode=ParseMode.HTML)
        return

    old_url = found["url"]
    found["url"] = new_url
    save_custom_link_buttons(buttons)
    _, detail_kb = build_button_detail_view(found["id"])
    await msg_obj.reply_text(
        f"✅ <b>Button URL Updated!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Button:</b> <code>{html.escape(found['text'])}</code>\n"
        f"• <b>Old URL:</b> <code>{html.escape(old_url)}</code>\n"
        f"• <b>New URL:</b> <code>{html.escape(new_url)}</code>\n"
        f"• <b>Synced:</b> <code>Cloud DB 🔒</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=detail_kb
    )

async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    # 1. Public Interactive Action: View Full SMS Popup (accessible to all group/channel members)
    if query.data and query.data.startswith("sms_"):
        sms_key = query.data[4:]
        if sms_key == "preview":
            await query.answer(
                "📩 Full SMS Content:\n\nYour WhatsApp verification code is 492-810. Do not share this code with anyone.",
                show_alert=True
            )
            return

        raw_msg = get_cached_sms_message(sms_key)
        if raw_msg:
            await query.answer(f"📩 Full SMS Content:\n\n{raw_msg}", show_alert=True)
        else:
            await query.answer("📩 Full SMS is not available for this message.", show_alert=True)
        return

    user_id = query.from_user.id if query.from_user else 0
    if not is_user_authorized(user_id):
        try:
            await query.answer("⛔ Access Denied: Administrator privileges revoked.", show_alert=True)
        except Exception:
            pass

        bot_name = get_bot_name()
        revoked_msg = (
            "⛔ <b>Administrator Privileges Revoked</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Your administrator access on <b>{html.escape(bot_name)}</b> has been revoked.\n"
            "You no longer have permission to view or manage the OTP Bot Dashboard.\n\n"
            "• <b>Account Status:</b> <code>Standard User</code>\n"
            "• <b>Admin Privileges:</b> <code>Disabled</code>\n"
            "• <b>Dashboard:</b> <code>Cleared & Deactivated</code>"
        )
        try:
            await query.edit_message_text(revoked_msg, parse_mode=ParseMode.HTML)
        except Exception:
            pass

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="🔄 <i>Your menu has been updated. Admin privileges removed.</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=ReplyKeyboardRemove()
            )
        except Exception:
            pass
        return

    # Acknowledge callback unless it has a dedicated toast notification
    if query.data not in (
        "toggle_format", "bot_name_help", "link_list", "link_help", "confirm_restart", "cancel_restart"
    ):
        try:
            await query.answer()
        except Exception:
            pass

    if query.data == "toggle_format":
        data = load_stored_data()
        curr = str(data.get("sms_format", "long")).lower().strip()
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
            await query.edit_message_text(text=msg_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup, disable_web_page_preview=True)
        except Exception as e:
            logger.warning(f"Could not edit dashboard message on toggle: {e}")

    elif query.data in ("fwd_ask_off", "fwd_turn_off"):
        confirm_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⏸️ Pause Forwarding", callback_data="confirm_fwd_off"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_fwd"),
            ]
        ])
        ask_text = (
            "⏸️ <b>Pause OTP Group Forwarding</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Are you sure you want to pause OTP forwarding to Telegram groups?\n\n"
            "• Delivery to Telegram groups will be paused.\n"
            "• Bot will continue recording all incoming OTPs silently into SQLite database & 28h history."
        )
        try:
            await query.edit_message_text(ask_text, parse_mode=ParseMode.HTML, reply_markup=confirm_kb)
        except Exception as e:
            logger.warning(f"Error displaying fwd_ask_off edit: {e}")
            try:
                if query.message:
                    await query.message.reply_text(ask_text, parse_mode=ParseMode.HTML, reply_markup=confirm_kb)
            except Exception as e2:
                logger.warning(f"Fallback reply for fwd_ask_off failed: {e2}")

    elif query.data in ("fwd_ask_on", "fwd_turn_on"):
        confirm_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("▶️ Resume Forwarding", callback_data="confirm_fwd_on"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_fwd"),
            ]
        ])
        ask_text = (
            "▶️ <b>Resume OTP Group Forwarding</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Are you sure you want to resume OTP forwarding to Telegram groups?\n\n"
            "• Delivery to Telegram groups will resume in real-time.\n"
            "• Zero Spam: Only new OTPs arriving from now on will be forwarded."
        )
        try:
            await query.edit_message_text(ask_text, parse_mode=ParseMode.HTML, reply_markup=confirm_kb)
        except Exception as e:
            logger.warning(f"Error displaying fwd_ask_on edit: {e}")
            try:
                if query.message:
                    await query.message.reply_text(ask_text, parse_mode=ParseMode.HTML, reply_markup=confirm_kb)
            except Exception as e2:
                logger.warning(f"Fallback reply for fwd_ask_on failed: {e2}")

    elif query.data in ("confirm_fwd_off", "fwd_confirm_off", "fwd_disable"):
        set_otp_forwarding_enabled(False)
        off_text = (
            "⏸️ <b>OTP Forwarding: Paused</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "• <b>Status:</b> ⏸️ <code>PAUSED (Silent Mode)</code>\n"
            "• <b>Group Delivery:</b> <code>PAUSED (No messages sent to groups)</code>\n"
            "• <b>Database & Retention:</b> <code>100% Active (Recording silently)</code>\n"
            "• <b>Synced:</b> <code>Cloud DB 🔒</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Incoming OTPs continue recording silently in SQLite & 28h history. Groups will not receive forwards until resumed.</i>"
        )
        try:
            await query.edit_message_text(off_text, parse_mode=ParseMode.HTML, reply_markup=None)
        except Exception as e:
            logger.warning(f"Error handling confirm_fwd_off edit: {e}")
            try:
                if query.message:
                    await query.message.reply_text(off_text, parse_mode=ParseMode.HTML)
            except Exception as e2:
                logger.warning(f"Fallback reply for confirm_fwd_off failed: {e2}")
        asyncio.create_task(sync_data_to_secret_db())

    elif query.data in ("confirm_fwd_on", "fwd_confirm_on", "fwd_enable"):
        set_otp_forwarding_enabled(True)
        on_text = (
            "🟢 <b>OTP Forwarding: Resumed</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "• <b>Status:</b> 🟢 <code>ACTIVE (Live Mode)</code>\n"
            "• <b>Group Delivery:</b> <code>ACTIVE (Delivered live to groups)</code>\n"
            "• <b>Database & Retention:</b> <code>100% Synced (28h)</code>\n"
            "• <b>Zero Spam Shield:</b> <code>Active (Pause OTPs suppressed)</code>\n"
            "• <b>Synced:</b> <code>Cloud DB 🔒</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Live forwarding is active. Every new incoming OTP is delivered to your group(s) in real time.</i>"
        )
        try:
            await query.edit_message_text(on_text, parse_mode=ParseMode.HTML, reply_markup=None)
        except Exception as e:
            logger.warning(f"Error handling confirm_fwd_on edit: {e}")
            try:
                if query.message:
                    await query.message.reply_text(on_text, parse_mode=ParseMode.HTML)
            except Exception as e2:
                logger.warning(f"Fallback reply for confirm_fwd_on failed: {e2}")
        asyncio.create_task(sync_data_to_secret_db())

    elif query.data in ("cancel_fwd", "fwd_cancel"):
        is_fwd = is_otp_forwarding_enabled()
        status_text = "🟢 <b>ACTIVE (Live Mode)</b>" if is_fwd else "⏸️ <b>PAUSED (Silent Mode)</b>"
        cancel_text = (
            "❌ <b>Forwarding Change Cancelled</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Current State:</b> {status_text}\n"
            "No changes were made to forwarding settings."
        )
        try:
            await query.edit_message_text(cancel_text, parse_mode=ParseMode.HTML, reply_markup=None)
        except Exception as e:
            logger.warning(f"Error handling cancel_fwd edit: {e}")
            try:
                if query.message:
                    await query.message.reply_text(cancel_text, parse_mode=ParseMode.HTML)
            except Exception as e2:
                logger.warning(f"Fallback reply for cancel_fwd failed: {e2}")

    elif query.data == "toggle_forwarding":
        curr = is_otp_forwarding_enabled()
        new_state = not curr
        set_otp_forwarding_enabled(new_state)
        asyncio.create_task(sync_data_to_secret_db())
        action_text = "Turned ON 🟢" if new_state else "Turned OFF ⏸️"
        status_badge = "🟢 <code>ACTIVE (Live Mode)</code>" if new_state else "⏸️ <code>PAUSED (Silent Mode)</code>"
        delivery_badge = "<code>Live Delivery ON</code>" if new_state else "<code>Delivery Stopped</code>"
        try:
            await query.edit_message_text(
                f"{'🟢' if new_state else '⏸️'} <b>OTP Forwarding {action_text}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"• <b>Delivery Status:</b> {status_badge}\n"
                f"• <b>Telegram Groups:</b> {delivery_badge}\n"
                "• <b>Database & Retention:</b> <code>100% Synced (28h)</code>\n"
                "• <b>Synced:</b> <code>Cloud DB 🔒</code>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"<i>{'Live group forwarding is active.' if new_state else 'Group forwarding paused. Recording silently.'}</i>",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning(f"Error handling toggle_forwarding: {e}")

    elif query.data == "bot_name_help":
        await query.answer("ℹ️ Bot name is managed by the admin.", show_alert=True)

    elif query.data == "link_help" or query.data == "link_list":
        context.user_data.pop("awaiting_btn_action", None)
        msg_text, markup = build_buttons_list_view()
        try:
            await query.edit_message_text(text=msg_text, parse_mode=ParseMode.HTML, reply_markup=markup)
            await query.answer("✅ Refreshed")
        except Exception as e:
            err_str = str(e).lower()
            if "message is not modified" in err_str or "not modified" in err_str:
                await query.answer("✅ Already up to date")
            else:
                await query.answer()
                try:
                    await query.message.reply_text(text=msg_text, parse_mode=ParseMode.HTML, reply_markup=markup)
                except Exception:
                    pass

    elif query.data.startswith("link_view_"):
        await query.answer()
        context.user_data.pop("awaiting_btn_action", None)
        btn_id = query.data.replace("link_view_", "").strip()
        msg_text, markup = build_button_detail_view(btn_id)
        if msg_text and markup:
            try:
                await query.edit_message_text(text=msg_text, parse_mode=ParseMode.HTML, reply_markup=markup)
            except Exception:
                await query.message.reply_text(text=msg_text, parse_mode=ParseMode.HTML, reply_markup=markup)
        else:
            await query.answer("Button not found", show_alert=True)
            msg_text, markup = build_buttons_list_view()
            await query.edit_message_text(text=msg_text, parse_mode=ParseMode.HTML, reply_markup=markup)

    elif query.data.startswith("link_del_confirm_"):
        await query.answer()
        btn_id = query.data.replace("link_del_confirm_", "").strip()
        msg_text, markup = build_button_delete_confirm_view(btn_id)
        if msg_text and markup:
            try:
                await query.edit_message_text(text=msg_text, parse_mode=ParseMode.HTML, reply_markup=markup)
            except Exception:
                await query.message.reply_text(text=msg_text, parse_mode=ParseMode.HTML, reply_markup=markup)

    elif query.data.startswith("link_del_execute_"):
        btn_id = query.data.replace("link_del_execute_", "").strip()
        buttons = get_custom_link_buttons()
        found = next((b for b in buttons if str(b["id"]) == btn_id), None)
        new_list = [b for b in buttons if str(b["id"]) != btn_id]
        save_custom_link_buttons(new_list)
        await query.answer("Button removed! 🗑️", show_alert=False)
        deleted_name = html.escape(found["text"]) if found else "Button"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ Back to Buttons List", callback_data="link_list")]
        ])
        await query.edit_message_text(
            f"✅ <b>Button Removed!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<code>{deleted_name}</code> has been removed from all forwarded group OTP messages.\n\n"
            f"• <b>Remaining Active Buttons:</b> <code>{len(new_list)}</code>\n"
            f"• <b>Synced:</b> <code>Secret Cloud DB 🔒</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb
        )

    elif query.data == "link_clear_all_confirm":
        await query.answer()
        msg_text, markup = build_buttons_clear_all_confirm_view()
        try:
            await query.edit_message_text(text=msg_text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception:
            await query.message.reply_text(text=msg_text, parse_mode=ParseMode.HTML, reply_markup=markup)

    elif query.data == "link_clear_all_execute":
        save_custom_link_buttons([])
        await query.answer("All buttons cleared! 🗑️", show_alert=False)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add New Button", callback_data="link_add_prompt")],
            [InlineKeyboardButton("◀️ Back to List", callback_data="link_list")],
        ])
        await query.edit_message_text(
            "✅ <b>All Custom Buttons Cleared!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "All custom buttons have been removed. Forwarded OTP messages will only display the OTP copy button.\n\n"
            "• <b>Storage:</b> <code>Cleared and Synced to Cloud DB 🔒</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb
        )

    elif query.data.startswith("link_rename_"):
        btn_id = query.data.replace("link_rename_", "").strip()
        buttons = get_custom_link_buttons()
        found = next((b for b in buttons if str(b["id"]) == btn_id), None)
        if found:
            context.user_data["awaiting_btn_action"] = {"action": "rename", "id": found["id"]}
            context.user_data["prompt_msg_id"] = query.message.message_id
            context.user_data["prompt_chat_id"] = query.message.chat_id
            await query.answer("Send new name in chat 💬")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Cancel / Back", callback_data=f"link_view_{found['id']}")]
            ])
            await query.edit_message_text(
                f"✏️ <b>Change Name for #{found['id']}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"• <b>Current Name:</b> <code>{html.escape(found['text'])}</code>\n"
                f"• <b>Target URL:</b> <code>{html.escape(found['url'])}</code>\n\n"
                f"💬 <b>Send the new button name</b> directly in this chat:",
                parse_mode=ParseMode.HTML,
                reply_markup=kb
            )

    elif query.data.startswith("link_reurl_"):
        btn_id = query.data.replace("link_reurl_", "").strip()
        buttons = get_custom_link_buttons()
        found = next((b for b in buttons if str(b["id"]) == btn_id), None)
        if found:
            context.user_data["awaiting_btn_action"] = {"action": "reurl", "id": found["id"]}
            context.user_data["prompt_msg_id"] = query.message.message_id
            context.user_data["prompt_chat_id"] = query.message.chat_id
            await query.answer("Send new URL or bot username 💬")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Cancel / Back", callback_data=f"link_view_{found['id']}")]
            ])
            await query.edit_message_text(
                f"🔗 <b>Change URL for #{found['id']}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"• <b>Button:</b> <code>{html.escape(found['text'])}</code>\n"
                f"• <b>Current URL:</b> <code>{html.escape(found['url'])}</code>\n\n"
                f"💬 <b>Send the new link URL or bot username</b> directly in this chat:\n"
                f"<code>https://...</code> or <code>@bot_username</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=kb
            )

    elif query.data == "link_add_prompt":
        context.user_data["awaiting_btn_action"] = {"action": "add"}
        context.user_data["prompt_msg_id"] = query.message.message_id
        context.user_data["prompt_chat_id"] = query.message.chat_id
        await query.answer("Send new button details 💬")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ Cancel / Back", callback_data="link_list")]
        ])
        await query.edit_message_text(
            "➕ <b>Add New Custom Button</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Custom buttons appear side-by-side below OTP messages in groups.\n\n"
            "💬 <b>Send button name and link or bot username</b> in chat using this format:\n"
            "<code>Button Name | @bot_username</code>\n"
            "<i>or:</i> <code>Button Name | https://yourlink.com</code>\n\n"
            "<i>Examples:</i>\n"
            "• <code>🤖 Number Bot | @my_number_bot</code>\n"
            "• <code>📢 Join Channel | https://t.me/mychannel</code>\n"
            "• <code>👥 Support | @mysupport</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb
        )

    elif query.data == "grp_list":
        context.user_data.pop("awaiting_grp_action", None)
        msg_text, markup = build_groups_list_view()
        try:
            await query.edit_message_text(text=msg_text, parse_mode=ParseMode.HTML, reply_markup=markup)
            await query.answer("✅ Refreshed")
        except Exception as e:
            err_str = str(e).lower()
            if "message is not modified" in err_str or "not modified" in err_str:
                await query.answer("✅ Already up to date")
            else:
                await query.answer()
                try:
                    await query.message.reply_text(text=msg_text, parse_mode=ParseMode.HTML, reply_markup=markup)
                except Exception:
                    pass

    elif query.data == "grp_max_reached":
        await query.answer("⚠️ Maximum limit of 10 groups reached! Remove an existing group before linking a new one.", show_alert=True)

    elif query.data.startswith("grp_del_confirm_"):
        gid_str = query.data.replace("grp_del_confirm_", "").strip()
        all_current = get_target_group_chat_ids()
        if len(all_current) <= MIN_FORWARDING_GROUPS:
            await query.answer("⚠️ Minimum 1 group required! Cannot remove the only remaining active group.", show_alert=True)
            return

        await query.answer()
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Yes, Remove", callback_data=f"grp_del_exec_{gid_str}"),
                InlineKeyboardButton("❌ Cancel", callback_data="grp_list"),
            ]
        ])
        await query.edit_message_text(
            f"⚠️ <b>Confirm Group Removal</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Are you sure you want to unlink group <code>{gid_str}</code> from OTP forwarding?\n"
            f"• <b>Active Groups:</b> <code>{len(all_current)} / {MAX_FORWARDING_GROUPS}</code>\n"
            f"• <b>After Removal:</b> <code>{len(all_current) - 1} / {MAX_FORWARDING_GROUPS}</code>\n\n"
            f"OTPs will no longer be forwarded to this group.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb
        )

    elif query.data.startswith("grp_del_exec_"):
        gid_str = query.data.replace("grp_del_exec_", "").strip()
        try:
            ok, reason = remove_dynamic_linked_group(int(gid_str))
            if ok:
                await query.answer(f"Group {gid_str} removed! 🗑️")
            elif reason == "MIN_LIMIT_REACHED":
                await query.answer("⚠️ Minimum 1 group required! Cannot remove the last active group.", show_alert=True)
            elif reason == "ENV_PROTECTED":
                await query.answer("🔒 This group is configured in server secret settings.", show_alert=True)
            else:
                await query.answer("Could not remove group", show_alert=True)
        except Exception:
            await query.answer("Error removing group", show_alert=True)
        msg_text, markup = build_groups_list_view()
        await query.edit_message_text(text=msg_text, parse_mode=ParseMode.HTML, reply_markup=markup)

    elif query.data == "grp_clear_confirm":
        all_current = get_target_group_chat_ids()
        dynamic_groups = get_dynamic_linked_groups()
        env_remaining = [g for g in all_current if g not in dynamic_groups]
        if len(env_remaining) < MIN_FORWARDING_GROUPS:
            await query.answer("⚠️ Minimum 1 group required! Clearing dynamic groups would leave 0 groups.", show_alert=True)
            return

        await query.answer()
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⚠️ Yes, Clear All", callback_data="grp_clear_exec"),
                InlineKeyboardButton("❌ Cancel", callback_data="grp_list"),
            ]
        ])
        await query.edit_message_text(
            "⚠️ <b>Confirm Clear Added Groups</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Are you sure you want to remove all admin-added forwarding groups?\n"
            "(Server secret groups will remain active).",
            parse_mode=ParseMode.HTML,
            reply_markup=kb
        )

    elif query.data == "grp_clear_exec":
        ok, reason = clear_dynamic_linked_groups()
        if ok:
            await query.answer("All dynamic groups cleared! 🗑️")
        else:
            await query.answer("⚠️ Minimum 1 group required! Cannot clear.", show_alert=True)
        msg_text, markup = build_groups_list_view()
        await query.edit_message_text(text=msg_text, parse_mode=ParseMode.HTML, reply_markup=markup)

    elif query.data == "grp_add_prompt":
        all_current = get_target_group_chat_ids()
        if len(all_current) >= MAX_FORWARDING_GROUPS:
            await query.answer("⚠️ Maximum limit of 10 groups reached! Remove an existing group first.", show_alert=True)
            return
        clear_all_admin_states(context)
        context.user_data["awaiting_grp_action"] = "add"
        context.user_data["prompt_msg_id"] = query.message.message_id
        context.user_data["prompt_chat_id"] = query.message.chat_id
        await query.answer("Send group chat ID 💬")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_admin_action")]
        ])
        await query.edit_message_text(
            f"➕ <b>Link New Forwarding Group</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Active Groups:</b> <code>{len(all_current)} / {MAX_FORWARDING_GROUPS}</code> (Limit: 1 - 10)\n\n"
            f"💬 <b>Send the numerical chat ID</b> of the group below (e.g. <code>-1004473973263</code>):\n\n"
            f"<i>Note: Make sure this bot is added to the group as an admin. Commands will be automatically wiped from the group chat menu (0 commands).</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb
        )

    elif query.data == "cancel_admin_action":
        clear_all_admin_states(context)
        await query.answer("Operation Cancelled ❌")
        try:
            await query.edit_message_text(
                "❌ <b>Operation Cancelled</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "No changes were made. All current settings remain active.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📊 View Live Status", callback_data="refresh_dash_msg")]
                ])
            )
        except Exception:
            pass

    elif query.data == "prompt_set_url":
        clear_all_admin_states(context)
        context.user_data["awaiting_api_url"] = True
        context.user_data["prompt_msg_id"] = query.message.message_id
        context.user_data["prompt_chat_id"] = query.message.chat_id
        await query.answer("Send new API URL 💬")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_admin_action")]
        ])
        await query.edit_message_text(
            f"🌐 <b>Update API Base URL</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Current URL:</b> <code>{html.escape(client.base_url)}</code>\n\n"
            f"💬 <b>Please send the new API URL</b> directly in this chat:\n"
            f"<i>Example:</i> <code>https://thirdwave.im</code>\n\n"
            f"Or click <b>❌ Cancel</b> below to abort.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb
        )

    elif query.data == "reset_url_default":
        default_url = "https://thirdwave.im"
        client.update_base_url(default_url)
        data = load_stored_data()
        data["thirdwave_base_url"] = default_url
        save_stored_data(data)
        asyncio.create_task(sync_data_to_secret_db())
        await query.answer("API URL Reset to Default ✅")
        text_view, markup = build_api_url_view()
        await query.edit_message_text(text=text_view, parse_mode=ParseMode.HTML, reply_markup=markup)

    elif query.data == "api_url_menu":
        clear_all_admin_states(context)
        text_view, markup = build_api_url_view()
        await query.edit_message_text(text=text_view, parse_mode=ParseMode.HTML, reply_markup=markup)

    elif query.data == "prompt_set_key":
        clear_all_admin_states(context)
        context.user_data["awaiting_api_key"] = True
        context.user_data["prompt_msg_id"] = query.message.message_id
        context.user_data["prompt_chat_id"] = query.message.chat_id
        await query.answer("Send new API Key 💬")
        current_key = client.api_key
        masked_key = f"{current_key[:8]}...{current_key[-4:]}" if len(current_key) > 12 else (current_key or "<i>None</i>")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_admin_action")]
        ])
        await query.edit_message_text(
            f"🔑 <b>Update Thirdwave API Key</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Current Key:</b> <code>{masked_key}</code>\n\n"
            f"💬 <b>Please send your new API Key</b> directly in this chat:\n"
            f"<i>Example:</i> <code>tw_live_1234567890abcdef</code>\n\n"
            f"Or click <b>❌ Cancel</b> below to abort.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb
        )

    elif query.data == "api_key_menu":
        clear_all_admin_states(context)
        text_view, markup = build_api_key_view()
        await query.edit_message_text(text=text_view, parse_mode=ParseMode.HTML, reply_markup=markup)

    elif query.data == "prompt_set_bot_name":
        clear_all_admin_states(context)
        context.user_data["awaiting_bot_name"] = True
        context.user_data["prompt_msg_id"] = query.message.message_id
        context.user_data["prompt_chat_id"] = query.message.chat_id
        await query.answer("Send new bot name 💬")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_admin_action")]
        ])
        await query.edit_message_text(
            f"✏️ <b>Change Bot Display Name</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Current Name:</b> <code>{html.escape(get_bot_name())}</code>\n\n"
            f"💬 <b>Please send the new bot display name</b> (1-64 characters) in chat.\n"
            f"It will be updated on the Telegram Bot API and saved across reboots.\n\n"
            f"💡 <i>Tip: Send <code>reset</code> or <code>default</code> to restore default name.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb
        )

    elif query.data == "reset_bot_name_default":
        default_name = get_default_bot_name()
        try:
            await context.bot.set_my_name(name=default_name)
        except Exception as e:
            logger.warning(f"Reset bot name error: {e}")
        data = load_stored_data()
        data["bot_name"] = default_name
        data["default_bot_name"] = default_name
        save_stored_data(data)
        asyncio.create_task(sync_data_to_secret_db())
        await query.answer("Bot Name Reset to Default ✅")
        text_view, markup = build_bot_name_view()
        await query.edit_message_text(text=text_view, parse_mode=ParseMode.HTML, reply_markup=markup)

    elif query.data == "bot_name_menu":
        clear_all_admin_states(context)
        text_view, markup = build_bot_name_view()
        await query.edit_message_text(text=text_view, parse_mode=ParseMode.HTML, reply_markup=markup)

    elif query.data == "fmt_set_short":
        data = load_stored_data()
        data["sms_format"] = "short"
        save_stored_data(data)
        asyncio.create_task(sync_data_to_secret_db())
        await query.answer("SMS Format set to Short ⚡")
        text_view, markup = build_sms_format_view()
        await query.edit_message_text(text=text_view, parse_mode=ParseMode.HTML, reply_markup=markup)

    elif query.data == "fmt_set_long":
        data = load_stored_data()
        data["sms_format"] = "long"
        save_stored_data(data)
        asyncio.create_task(sync_data_to_secret_db())
        await query.answer("SMS Format set to Long 📜")
        text_view, markup = build_sms_format_view()
        await query.edit_message_text(text=text_view, parse_mode=ParseMode.HTML, reply_markup=markup)

    elif query.data == "fmt_preview_short":
        await query.answer("Previewing Short Format")
        text_view, markup = build_sms_format_view(preview_mode="short")
        await query.edit_message_text(text=text_view, parse_mode=ParseMode.HTML, reply_markup=markup)

    elif query.data == "fmt_preview_long":
        await query.answer("Previewing Long Format")
        text_view, markup = build_sms_format_view(preview_mode="long")
        await query.edit_message_text(text=text_view, parse_mode=ParseMode.HTML, reply_markup=markup)

    elif query.data in ("admin_manage_admins", "admin_panel"):
        clear_all_admin_states(context)
        text_view, markup = build_admin_manage_view() if query.data == "admin_manage_admins" else build_status_dashboard()
        try:
            await query.edit_message_text(text=text_view, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                try:
                    await query.answer("Updated")
                except Exception:
                    pass

    elif query.data == "close_admin_manage":
        clear_all_admin_states(context)
        await query.answer("Closed 🔒")
        try:
            await query.edit_message_text(
                "🔒 <b>Administrator Panel Closed</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "<i>The administrator management panel has been closed safely.</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=None
            )
        except Exception:
            pass

    elif query.data == "admin_add_admin_prompt":
        context.user_data["awaiting_add_admin"] = True
        context.user_data["prompt_msg_id"] = query.message.message_id
        context.user_data["prompt_chat_id"] = query.message.chat_id
        text = (
            "➕ <b>Add New Administrator</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Send the numerical <b>Telegram User ID</b> of the user you wish to promote to Admin.\n\n"
            "💡 <i>Tip: The user can check their ID via @userinfobot.</i>"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="admin_manage_admins")]
        ])
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    elif query.data.startswith("admin_inspect_"):
        target_id = int(query.data.split("_")[2])
        admins = get_all_admin_details()
        admin_info = next((a for a in admins if a["user_id"] == target_id), None)

        fname = (admin_info.get("first_name") if admin_info else "") or "Administrator"
        uname = (admin_info.get("username") if admin_info else "")
        u_tag = f"@{html.escape(uname)}" if uname else "<i>None</i>"
        is_super = admin_info.get("is_super", False) if admin_info else (target_id in ADMIN_USER_IDS)
        role_badge = "👑 <b>Super Administrator</b> (Configured via .env)" if is_super else "🛡️ <b>Administrator</b> (Added via Bot)"

        profile_text = (
            f"👑 <b>Administrator Profile & Access Control</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 <b>Admin ID:</b> <code>{target_id}</code>\n"
            f"📛 <b>Name:</b> <code>{html.escape(fname)}</code>\n"
            f"🔗 <b>Username:</b> {u_tag}\n"
            f"🔰 <b>Role & Tier:</b> {role_badge}\n"
            f"🛡️ <b>Account Status:</b> <code>Active & Verified</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        buttons = []
        if not is_super:
            buttons.append([InlineKeyboardButton("🗑️ Revoke Administrator Privileges", callback_data=f"admin_rm_admin_{target_id}")])
        buttons.append([InlineKeyboardButton("📋 Back to Admin List", callback_data="admin_manage_admins")])
        buttons.append([InlineKeyboardButton("❌ Close", callback_data="close_admin_manage")])
        await query.edit_message_text(profile_text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))

    elif query.data == "admin_remove_admin_menu":
        admins = get_all_admin_details()
        removable = [a for a in admins if not a["is_super"]]
        if not removable:
            await query.answer("ℹ️ No removable database admins found. Super Admins (from .env) cannot be removed via Telegram.", show_alert=True)
            return

        buttons = []
        for a in removable:
            u_tag = f"@{a['username']}" if a["username"] else a["first_name"]
            buttons.append([InlineKeyboardButton(f"🗑️ Remove {u_tag} ({a['user_id']})", callback_data=f"admin_rm_admin_{a['user_id']}")])

        buttons.append([InlineKeyboardButton("◀️ Back", callback_data="admin_manage_admins")])
        text = (
            "🗑️ <b>Remove Administrator</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Tap an admin below to revoke their administrative privileges:"
        )
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))

    elif query.data.startswith("admin_rm_admin_"):
        target_id = int(query.data.split("_")[3])
        ok = remove_admin(target_id)
        if ok:
            await query.answer(f"✅ Admin privileges revoked for {target_id}!", show_alert=True)
            asyncio.create_task(notify_admin_revocation(context.bot, target_id))
        else:
            await query.answer(f"❌ Could not remove {target_id}.", show_alert=True)

        text_view, markup = build_admin_manage_view()
        await query.edit_message_text(text=text_view, parse_mode=ParseMode.HTML, reply_markup=markup)

    elif query.data in ("refresh_dash", "refresh_dash_msg"):
        try:
            msg_text, reply_markup = build_status_dashboard()
            await query.edit_message_text(text=msg_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup, disable_web_page_preview=True)
            await query.answer("Dashboard Refreshed 🔄")
        except Exception:
            await query.answer("Refreshed")

    elif query.data == "confirm_restart":
        await query.answer("Restarting engine 🔄")
        try:
            await query.edit_message_text(
                "🔄 <b>Restarting Bot Engine...</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "⏳ <i>Saving state and rebooting cleanly...</i>\n"
                "<i>You will receive a confirmation message once online.</i>",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
        await perform_zero_restart_handover(query.message, manual_restart=True)

    elif query.data == "cancel_restart":
        await query.answer("Cancelled")
        try:
            await query.edit_message_text(
                "❌ <b>Restart Cancelled</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "The bot engine continues running normally without interruption.",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    else:
        try:
            await query.answer()
        except Exception:
            pass

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
        clear_all_admin_states(context)
        text, markup = build_api_url_view()
        await msg_obj.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    raw_input = " ".join(context.args).strip()
    new_url = client.update_base_url(raw_input)

    # Persist in bot_data.json and SQLite so it permanently survives restarts and pushes
    data = load_stored_data()
    data["thirdwave_base_url"] = new_url
    set_bot_setting("thirdwave_base_url", new_url)
    save_stored_data(data)
    if "sync_data_to_secret_db" in globals():
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(sync_data_to_secret_db())
        except (RuntimeError, Exception):
            pass

    await msg_obj.reply_text(
        f"✅ <b>API Base URL Successfully Updated!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>New Active URL:</b> <code>{html.escape(new_url)}</code>\n"
        f"• <b>Endpoint:</b> <code>{html.escape(new_url)}/api/v1/traffic</code>\n\n"
        f"The bot is now requesting OTP traffic from this URL in real-time.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 API URL Settings", callback_data="api_url_menu")],
            [InlineKeyboardButton("📊 Status", callback_data="refresh_dash_msg")]
        ])
    )

async def setkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        clear_all_admin_states(context)
        text, markup = build_api_key_view()
        await msg_obj.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    raw_input = context.args[0].strip()
    new_key = client.update_api_key(raw_input)

    data = load_stored_data()
    data["thirdwave_api_key"] = new_key
    set_bot_setting("thirdwave_api_key", new_key)
    save_stored_data(data)
    if "sync_data_to_secret_db" in globals():
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(sync_data_to_secret_db())
        except (RuntimeError, Exception):
            pass

    new_masked = f"{new_key[:8]}...{new_key[-4:]}" if len(new_key) > 12 else new_key
    await msg_obj.reply_text(
        f"✅ <b>Thirdwave API Key Successfully Updated!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>New Active Key:</b> <code>{new_masked}</code>\n\n"
        f"The bot is now requesting OTP traffic with this key in real-time.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔑 API Key Settings", callback_data="api_key_menu")],
            [InlineKeyboardButton("📊 Status", callback_data="refresh_dash_msg")]
        ])
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

    new_name = " ".join(context.args).strip() if (context and context.args) else ""
    if not new_name:
        clear_all_admin_states(context)
        text, markup = build_bot_name_view()
        await msg_obj.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    new_name = new_name[:64]
    try:
        await context.bot.set_my_name(name=new_name)
    except Exception as e:
        logger.warning(f"Could not set Telegram bot name: {e}")

    data = load_stored_data()
    data["bot_name"] = new_name
    data["default_bot_name"] = new_name
    save_stored_data(data)
    asyncio.create_task(sync_data_to_secret_db())

    await msg_obj.reply_text(
        f"✅ <b>Bot Name Successfully Updated!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>New Name:</b> <code>{html.escape(new_name)}</code>\n"
        f"• <b>Default Name:</b> <code>{html.escape(new_name)} (Saved as permanent default)</code>\n"
        f"• <b>Telegram API:</b> <code>Applied Officially ✅</code>\n"
        f"• <b>Database:</b> <code>Saved Secretly to Cloud Store 🔒</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Bot Name Settings", callback_data="bot_name_menu")],
            [InlineKeyboardButton("📊 Status", callback_data="refresh_dash_msg")]
        ])
    )

async def perform_zero_restart_handover(target_message=None, manual_restart: bool = False):
    """Executes the clean zero-restart session handover."""
    global _is_handover, _manual_restart
    _is_handover = True
    _manual_restart = manual_restart

    try:
        set_bot_setting("manual_restart", "1" if manual_restart else "0")
        if manual_restart:
            try:
                with open(".manual_restart", "w", encoding="utf-8") as mf:
                    mf.write("1")
            except Exception:
                pass
    except Exception:
        pass

    try:
        with get_db_connection() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    except Exception as e:
        logger.warning(f"Handover PRAGMA notice: {e}")

    if gist_storage.enabled:
        try:
            today_total, total_28h, active_countries = get_dashboard_stats()
            await gist_storage.save_state(
                seen_dict=seen_timestamps,
                is_handover=True,
                total_forwarded=today_total,
                country_counts=active_countries,
                base_url=client.base_url,
                manual_restart=manual_restart,
            )
        except Exception as e:
            logger.warning(f"Handover Gist save notice: {e}")

    if target_message:
        try:
            await target_message.edit_text(
                "🔄 <b>Restarting Bot Engine...</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "✅ <i>State saved securely. Starting new server session now...</i>",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    # Proactively dispatch the new GitHub Actions workflow run session
    try:
        await trigger_proactive_handover(manual_restart=manual_restart)
    except Exception as e:
        logger.warning(f"Admin restart dispatch notice: {e}")

    logger.info(f"Handover triggered (manual_restart={manual_restart}). Exiting cleanly (code 0)...")
    sys.exit(0)

async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command with confirmation prompt to trigger clean zero-restart session handover."""
    user_id = update.effective_user.id if update.effective_user else 0
    if not is_user_authorized(user_id):
        await update.effective_message.reply_text("⛔ Unauthorized access.")
        return

    # Direct force restart if argument passed: /restart force
    if context and context.args and context.args[0].lower() in ("force", "yes", "now"):
        await perform_zero_restart_handover(update.effective_message, manual_restart=True)
        return

    confirm_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm Restart", callback_data="confirm_restart"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_restart"),
        ]
    ])
    await update.effective_message.reply_text(
        "⚠️ <b>Restart Bot Engine</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Are you sure you want to reboot the bot engine?\n\n"
        "• State and counters will be securely saved to Cloud memory.\n"
        "• The bot will reboot cleanly and send you a confirmation message once online.",
        parse_mode=ParseMode.HTML,
        reply_markup=confirm_kb,
    )

async def addadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    msg_obj = update.effective_message
    if not chat or not msg_obj or not user:
        return
    if not is_user_authorized(user.id):
        return
    args = context.args if context and context.args else []
    if not args or not args[0].isdigit():
        await msg_obj.reply_text("💡 Usage: <code>/addadmin &lt;user_id&gt;</code>\nExample: <code>/addadmin 123456789</code>", parse_mode=ParseMode.HTML)
        return
    target_id = int(args[0])
    ok = add_admin(target_id, added_by=user.id)
    if ok:
        await msg_obj.reply_text(
            f"✅ User <code>{target_id}</code> has been promoted to Administrator!\n"
            f"• Cloud DB: <code>Synced & Encrypted 🔒</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👑 Manage Admins", callback_data="admin_manage_admins")]
            ])
        )
        asyncio.create_task(notify_admin_promoted(context.bot, target_id))
    else:
        await msg_obj.reply_text("❌ Could not add administrator.", parse_mode=ParseMode.HTML)

async def removeadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    msg_obj = update.effective_message
    if not chat or not msg_obj or not user:
        return
    if not is_user_authorized(user.id):
        return
    args = context.args if context and context.args else []
    if not args or not args[0].isdigit():
        await msg_obj.reply_text("💡 Usage: <code>/removeadmin &lt;user_id&gt;</code>\nExample: <code>/removeadmin 123456789</code>", parse_mode=ParseMode.HTML)
        return
    target_id = int(args[0])
    if target_id in ADMIN_USER_IDS:
        await msg_obj.reply_text("⛔ Cannot remove Super Admin defined in environment variables / secrets.", parse_mode=ParseMode.HTML)
        return
    ok = remove_admin(target_id)
    if ok:
        await msg_obj.reply_text(
            f"✅ Administrator privileges revoked for <code>{target_id}</code>.\n"
            f"• Cloud DB: <code>Synced & Encrypted 🔒</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👑 Manage Admins", callback_data="admin_manage_admins")]
            ])
        )
        asyncio.create_task(notify_admin_revocation(context.bot, target_id))
    else:
        await msg_obj.reply_text("❌ Could not remove administrator.", parse_mode=ParseMode.HTML)

async def admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    msg_obj = update.effective_message
    if not chat or not msg_obj or not user:
        return
    if not is_user_authorized(user.id):
        return
    clear_all_admin_states(context)
    text_view, markup = build_admin_manage_view()
    await msg_obj.reply_text(text_view, parse_mode=ParseMode.HTML, reply_markup=markup)

async def send_startup_announcement(application: Application):
    """
    Sends startup notification to Admin private chat ONLY in 3 cases:
      1. A new code update was pushed to GitHub (github.event_name == 'push')
      2. The git commit SHA changed from the previously recorded commit
      3. An admin explicitly triggered a manual restart from the admin panel

    COMPLETELY SILENT on:
      - Routine 24/7 session handovers (IS_HANDOVER=true)
      - Scheduled safety-net triggers (STARTUP_TYPE=schedule)
      - Any workflow_dispatch NOT explicitly flagged as manual restart
    """
    global _manual_restart, _startup_alert_sent

    # ── In-process dedup guard ─────────────────────────────────────────────
    # Prevents double-firing if send_startup_announcement is somehow called
    # more than once within the same Python process (e.g. via task re-schedule).
    if _startup_alert_sent:
        logger.info("\u2139\ufe0f send_startup_announcement: already sent in this process — skipping.")
        return
    _startup_alert_sent = True

    # SILENT_STARTUP env override (emergency escape hatch)
    if os.getenv("SILENT_STARTUP", "").strip().lower() in ("true", "1", "yes"):
        logger.info("\U0001f92b SILENT_STARTUP set: startup notification suppressed.")
        return

    current_commit = get_current_commit()
    last_notified_commit = get_bot_setting("last_notified_commit", "")

    # Only trust manual_restart if IS_MANUAL_RESTART env was explicitly set to true
    # by the bot's own restart-dispatch call. Never trust it from Gist alone.
    manual_restart = _manual_restart  # already set from IS_MANUAL_RESTART env at startup

    # ── Rule 1: Scheduled safety-net → always silent ──────────────────────────
    if STARTUP_TYPE == "schedule" and not manual_restart:
        logger.info("\U0001f92b Scheduled safety-net (STARTUP_TYPE=schedule): startup notification suppressed.")
        return

    # ── Rule 2: Session handover → always silent unless IS_MANUAL_RESTART=true ─
    if _is_handover and not manual_restart:
        logger.info(
            f"\U0001f92b Automated 24/7 session handover (IS_HANDOVER=true, commit={current_commit or 'unknown'}): "
            f"startup notification suppressed (zero-downtime mode)."
        )
        return

    # ── Rule 3: workflow_dispatch that is NOT a handover and NOT manual restart → silent ─
    # (e.g. manually triggered from GitHub Actions UI without being a code push)
    if STARTUP_TYPE == "workflow_dispatch" and not manual_restart and not _is_handover:
        # Only alert if commit actually changed (real update snuck in via dispatch)
        is_commit_change = bool(current_commit and last_notified_commit and current_commit != last_notified_commit)
        if not is_commit_change:
            logger.info(
                f"\u2139\ufe0f workflow_dispatch startup without new commit or manual restart: "
                f"startup notification suppressed (commit={current_commit or 'none'})."
            )
            if current_commit and not last_notified_commit:
                set_bot_setting("last_notified_commit", current_commit)
            return

    # ── Determine what kind of alert to send ──────────────────────────────────
    is_git_push = (STARTUP_TYPE == "push")
    is_commit_change = bool(current_commit and last_notified_commit and current_commit != last_notified_commit)
    is_new_update = is_git_push or is_commit_change

    # ── Crash-restart deduplication: already notified for this exact commit? ──
    # If STARTUP_TYPE=="push" but last_notified_commit already matches current_commit,
    # it means a previous restart in this same session already sent the alert.
    # This prevents 20x duplicate alerts from the crash-restart loop.
    if is_git_push and current_commit and current_commit == last_notified_commit and not manual_restart:
        logger.info(
            f"\u2139\ufe0f Push event — startup alert already sent for commit {current_commit[:7]}. "
            f"Skipping duplicate (crash-restart protection)."
        )
        return

    # Final gate: if nothing significant happened, stay silent
    if not is_new_update and not manual_restart:
        logger.info(
            f"\u2139\ufe0f Bot started cleanly in 24/7 background mode "
            f"(commit={current_commit or 'none'}, trigger={STARTUP_TYPE}). No admin alert needed."
        )
        if current_commit and not last_notified_commit:
            set_bot_setting("last_notified_commit", current_commit)
            if gist_storage.enabled:
                try:
                    today_total, total_28h, active_countries = get_dashboard_stats()
                    asyncio.create_task(gist_storage.save_state(
                        seen_dict=seen_timestamps,
                        is_handover=_is_handover,
                        total_forwarded=today_total,
                        country_counts=active_countries,
                        base_url=client.base_url if "client" in globals() else "",
                        last_notified_commit=current_commit,
                    ))
                except Exception:
                    pass
        return

    # ── Clear manual_restart flag immediately so it doesn't re-fire next session ─
    if manual_restart:
        _manual_restart = False
        set_bot_setting("manual_restart", "0")
        if gist_storage.enabled:
            try:
                today_total, total_28h, active_countries = get_dashboard_stats()
                await gist_storage.save_state(
                    seen_dict=seen_timestamps,
                    is_handover=_is_handover,
                    total_forwarded=today_total,
                    country_counts=active_countries,
                    base_url=client.base_url if "client" in globals() else "",
                    manual_restart=False,
                    last_notified_commit=current_commit,
                )
            except Exception as e:
                logger.debug(f"Clear manual_restart gist notice: {e}")

    # ── DEDUP GUARD: record commit to DB *before* sending ─────────────────────
    # This prevents the crash-restart loop from resending the alert if the bot
    # crashes after sending but before recording. On the next restart (same
    # STARTUP_TYPE=push), line 5156 sees current_commit==last_notified_commit
    # and skips the alert. We also push to Gist immediately so the next
    # runner session (fresh DB) also sees it.
    if current_commit and not manual_restart:
        set_bot_setting("last_notified_commit", current_commit)
        if gist_storage.enabled:
            try:
                today_total, total_28h, active_countries = get_dashboard_stats()
                await gist_storage.save_state(
                    seen_dict=seen_timestamps,
                    is_handover=_is_handover,
                    total_forwarded=today_total,
                    country_counts=active_countries,
                    base_url=client.base_url if "client" in globals() else "",
                    last_notified_commit=current_commit,
                    manual_restart=False,
                )
            except Exception as e:
                logger.debug(f"Pre-send Gist commit record notice: {e}")

    # ── Send alert ────────────────────────────────────────────────────────────
    try:
        me = await application.bot.get_me()
        if me and me.first_name:
            set_cached_telegram_name(me.first_name)
    except Exception:
        pass

    bot_name = get_bot_name()
    time_str, date_str = format_bangladesh_12h()

    if manual_restart and not is_new_update:
        trigger_label = "\U0001f501 <b>Admin Manual Restart</b>"
    elif is_git_push:
        trigger_label = "\U0001f680 <b>New Update Deployed</b>"
    else:
        trigger_label = "\U0001f4e6 <b>Code Update Detected</b>"

    admin_msg = (
        f"✅ <b>{html.escape(bot_name)}</b> is now <b>LIVE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ <b>Start Time:</b> <code>{time_str}</code>\n"
        f"📅 <b>Date:</b>       <code>{date_str}</code>\n"
        f"⚡ <b>Trigger:</b>    {trigger_label}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<i>👑 Use /start anytime to open the admin dashboard.</i>"
    )
    for aid in get_all_admin_ids():
        if aid:
            try:
                await send_with_retry(application.bot, aid, admin_msg, reply_markup=get_admin_reply_keyboard())
                logger.info(f"\u2705 Startup alert sent to admin {aid}")
            except Exception as e:
                logger.warning(f"Startup alert failed for admin {aid}: {e}")

    # Re-confirm commit recorded (also covers manual_restart path)
    if current_commit:
        set_bot_setting("last_notified_commit", current_commit)
        if manual_restart and gist_storage.enabled:
            try:
                today_total, total_28h, active_countries = get_dashboard_stats()
                await gist_storage.save_state(
                    seen_dict=seen_timestamps,
                    is_handover=_is_handover,
                    total_forwarded=today_total,
                    country_counts=active_countries,
                    base_url=client.base_url if "client" in globals() else "",
                    last_notified_commit=current_commit,
                    manual_restart=False,
                )
            except Exception as e:
                logger.debug(f"Post-send Gist commit record notice: {e}")


async def periodic_db_cleanup_loop():
    cleanup_counter = 0
    while True:
        try:
            await asyncio.sleep(60.0)  # runs every 60s to prune old 28h records continuously
            cleanup_old_messages(max_age_hours=28)
            cleanup_counter += 1
            # Periodically flush SQLite WAL journal to the permanent database file
            if cleanup_counter % 5 == 0:
                try:
                    with get_db_connection() as conn:
                        conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
                except Exception:
                    pass
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Periodic DB cleanup warning: {e}")
            await asyncio.sleep(5.0)

async def periodic_gist_sync_loop():
    global _gist_dirty
    while True:
        try:
            await asyncio.sleep(15.0)
            if _gist_dirty and gist_storage.enabled:
                _gist_dirty = False
                try:
                    today_total, total_28h, active_countries = get_dashboard_stats()
                    await gist_storage.save_state(
                        seen_dict=seen_timestamps,
                        is_handover=False,
                        total_forwarded=today_total,
                        country_counts=active_countries,
                        base_url=client.base_url,
                    )
                except Exception as gist_err:
                    logger.debug(f"Periodic Gist sync notice: {gist_err}")
                    _gist_dirty = True  # retry next cycle
        except asyncio.CancelledError:
            break
        except Exception as loop_err:
            logger.debug(f"Periodic Gist loop error: {loop_err}")
            await asyncio.sleep(5.0)

async def render_prompt_response(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    prompt_msg_id: Optional[int],
    user_msg,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None
):
    """Deletes admin typed message and edits prompt card in-place (or sends new if edit fails)."""
    if user_msg:
        try:
            await user_msg.delete()
        except Exception:
            pass

    edited = False
    if prompt_msg_id and chat_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=prompt_msg_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )
            edited = True
        except Exception as e:
            if "not modified" in str(e).lower():
                edited = True

    if not edited and chat_id:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.warning(f"Error sending prompt response: {e}")

async def keyboard_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles bottom reply keyboard button presses from admins (private DM only).
    Group messages are completely ignored — only OTP forwards appear in the group.
    """
    chat    = update.effective_chat
    msg_obj = update.effective_message
    user    = update.effective_user
    if not msg_obj or not user or not chat:
        return
    user_id = user.id
    if not is_user_authorized(user_id):
        await msg_obj.reply_text("⛔ <b>Access Restricted</b>: Admins only.", parse_mode=ParseMode.HTML)
        return

    # Keep command menu button and slash commands permanently active
    asyncio.create_task(setup_admin_menu_for_chat(context.bot, chat.id))

    text = (msg_obj.text or "").strip()

    # Clear pending prompt states if an admin clicks any reply menu button
    if text in ("📊 Status", "⚡ OTP Forwarding", "🔑 API Key", "🌐 API URL", "📋 SMS Format", "✏️ Bot Name", "🔗 Link Button", "👥 Linked Groups", "👑 Admins", "🔄 Restart"):
        clear_all_admin_states(context)

    # Cancel command or cancel text
    if text.lower() in ("cancel", "❌ cancel", "/cancel"):
        clear_all_admin_states(context)
        await msg_obj.reply_text(
            "❌ <b>Operation Cancelled.</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "No changes were made. All current settings remain active.",
            parse_mode=ParseMode.HTML,
            reply_markup=get_admin_reply_keyboard(),
        )
        return

    if text == "📊 Status":
        await status_command(update, context)

    elif text in ("⚡ OTP Forwarding", "⚡ Forwarding", "Forwarding", "forwarding"):
        clear_all_admin_states(context)
        await toggle_forwarding_command(update, context)
        return

    elif text == "🔑 API Key":
        # Show current key info / interactive panel
        context.args = []
        await setkey_command(update, context)

    elif text == "🌐 API URL":
        # Show current URL info / interactive panel
        context.args = []
        await seturl_command(update, context)

    elif text == "📋 SMS Format":
        context.args = []
        await toggleformat_command(update, context)

    elif text == "✏️ Bot Name":
        clear_all_admin_states(context)
        text_view, markup = build_bot_name_view()
        await msg_obj.reply_text(text_view, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    elif text == "🔗 Link Button":
        clear_all_admin_states(context)
        msg_text, markup = build_buttons_list_view()
        await msg_obj.reply_text(msg_text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    elif text == "👥 Linked Groups":
        clear_all_admin_states(context)
        msg_text, markup = build_groups_list_view()
        await msg_obj.reply_text(msg_text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    elif text == "👑 Admins":
        clear_all_admin_states(context)
        text_view, markup = build_admin_manage_view()
        await msg_obj.reply_text(text_view, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    elif text == "🔄 Restart":
        clear_all_admin_states(context)
        await restart_command(update, context)
        return

    # Awaiting Add Admin text input
    if context.user_data.get("awaiting_add_admin"):
        context.user_data.pop("awaiting_add_admin", None)
        prompt_msg_id = context.user_data.pop("prompt_msg_id", None)
        prompt_chat_id = context.user_data.pop("prompt_chat_id", None) or chat.id

        clean_text = text.strip()
        if clean_text.isdigit():
            aid = int(clean_text)
            uname = ""
            fname = ""
            try:
                target_chat = await context.bot.get_chat(aid)
                if target_chat:
                    uname = target_chat.username or ""
                    fname = target_chat.first_name or ""
            except Exception:
                pass

            ok = add_admin(aid, added_by=user_id, username=uname, first_name=fname)
            if ok:
                u_tag = f"@{html.escape(uname)}" if uname else "<i>None</i>"
                disp_name = html.escape(fname or "Administrator")
                confirm_text = (
                    "✅ <b>Administrator Successfully Added!</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>User ID:</b> <code>{aid}</code>\n"
                    f"• <b>Name:</b> <code>{disp_name}</code>\n"
                    f"• <b>Username:</b> {u_tag}\n"
                    "• <b>Role:</b> <code>🛡️ Administrator</code>\n"
                    "• <b>Status:</b> <code>Active & Authorized</code>\n"
                    "• <b>Cloud Sync:</b> <code>Synced & Encrypted 🔒</code>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "<i>The user has been promoted and notified in private chat.</i>"
                )
                confirm_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"👑 Manage ID: {aid}", callback_data=f"admin_inspect_{aid}")],
                    [InlineKeyboardButton("📋 Active Admin List", callback_data="admin_manage_admins")],
                    [InlineKeyboardButton("➕ Add Another Admin", callback_data="admin_add_admin_prompt")],
                    [InlineKeyboardButton("❌ Close", callback_data="close_admin_manage")]
                ])

                await render_prompt_response(context, prompt_chat_id, prompt_msg_id, msg_obj, confirm_text, confirm_kb)
                asyncio.create_task(notify_admin_promoted(context.bot, aid))
            else:
                fail_text = "❌ <b>Failed to add administrator.</b> Database write error."
                fail_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Try Again", callback_data="admin_add_admin_prompt")],
                    [InlineKeyboardButton("📋 Admin List", callback_data="admin_manage_admins")]
                ])
                await render_prompt_response(context, prompt_chat_id, prompt_msg_id, msg_obj, fail_text, fail_kb)
        else:
            err_text = (
                "❌ <b>Invalid Account ID</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Please enter digits only (e.g. <code>6798979733</code>).\n\n"
                "💡 <i>Tip: The user can check their ID via @userinfobot.</i>"
            )
            err_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Try Again", callback_data="admin_add_admin_prompt")],
                [InlineKeyboardButton("❌ Cancel", callback_data="admin_manage_admins")]
            ])
            await render_prompt_response(context, prompt_chat_id, prompt_msg_id, msg_obj, err_text, err_kb)
        return

    # Awaiting API URL text input
    if context.user_data.get("awaiting_api_url"):
        context.user_data.pop("awaiting_api_url", None)
        prompt_msg_id = context.user_data.pop("prompt_msg_id", None)
        prompt_chat_id = context.user_data.pop("prompt_chat_id", None) or chat.id
        new_url = client.update_base_url(text)
        data = load_stored_data()
        data["thirdwave_base_url"] = new_url
        save_stored_data(data)
        asyncio.create_task(sync_data_to_secret_db())
        resp_text = (
            f"✅ <b>API Base URL Successfully Updated!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>New Active URL:</b> <code>{html.escape(new_url)}</code>\n"
            f"• <b>Endpoint:</b> <code>{html.escape(new_url)}/api/v1/traffic</code>\n"
            f"• <b>Cloud DB:</b> <code>Synced & Encrypted 🔒</code>\n\n"
            f"The bot is now requesting OTP traffic from this URL in real-time."
        )
        resp_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 API URL Settings", callback_data="api_url_menu")],
            [InlineKeyboardButton("📊 Status", callback_data="refresh_dash_msg")]
        ])
        await render_prompt_response(context, prompt_chat_id, prompt_msg_id, msg_obj, resp_text, resp_kb)
        return

    # Awaiting API Key text input
    if context.user_data.get("awaiting_api_key"):
        context.user_data.pop("awaiting_api_key", None)
        prompt_msg_id = context.user_data.pop("prompt_msg_id", None)
        prompt_chat_id = context.user_data.pop("prompt_chat_id", None) or chat.id
        new_key = client.update_api_key(text)
        data = load_stored_data()
        data["thirdwave_api_key"] = new_key
        save_stored_data(data)
        asyncio.create_task(sync_data_to_secret_db())
        new_masked = f"{new_key[:8]}...{new_key[-4:]}" if len(new_key) > 12 else new_key
        resp_text = (
            f"✅ <b>Thirdwave API Key Successfully Updated!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>New Active Key:</b> <code>{new_masked}</code>\n"
            f"• <b>Status:</b> <code>Active & Forwarding OTPs ⚡</code>\n"
            f"• <b>Cloud DB:</b> <code>Synced & Encrypted 🔒</code>"
        )
        resp_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔑 API Key Settings", callback_data="api_key_menu")],
            [InlineKeyboardButton("📊 Status", callback_data="refresh_dash_msg")]
        ])
        await render_prompt_response(context, prompt_chat_id, prompt_msg_id, msg_obj, resp_text, resp_kb)
        return

    # Awaiting Bot Name text input
    if context.user_data.get("awaiting_bot_name"):
        context.user_data.pop("awaiting_bot_name", None)
        prompt_msg_id = context.user_data.pop("prompt_msg_id", None)
        prompt_chat_id = context.user_data.pop("prompt_chat_id", None) or chat.id
        new_name = text.strip()[:64]
        default_name = get_default_bot_name()
        if new_name.lower() in ("default", "reset"):
            new_name = default_name

        try:
            await context.bot.set_my_name(name=new_name)
        except Exception as e:
            logger.warning(f"Set bot name error: {e}")

        data = load_stored_data()
        data["bot_name"] = new_name
        data["default_bot_name"] = new_name
        save_stored_data(data)
        asyncio.create_task(sync_data_to_secret_db())

        resp_text = (
            f"✅ <b>Bot Name Successfully Updated!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>New Name:</b> <code>{html.escape(new_name)}</code>\n"
            f"• <b>Default Name:</b> <code>{html.escape(new_name)} (Saved as permanent default)</code>\n"
            f"• <b>Telegram API:</b> <code>Applied Officially ✅</code>\n"
            f"• <b>Cloud DB:</b> <code>Saved Secretly to Cloud Store 🔒</code>"
        )
        resp_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Bot Name Settings", callback_data="bot_name_menu")],
            [InlineKeyboardButton("📊 Status", callback_data="refresh_dash_msg")]
        ])
        await render_prompt_response(context, prompt_chat_id, prompt_msg_id, msg_obj, resp_text, resp_kb)
        return

    # Check if admin is linking a group
    if context.user_data.get("awaiting_grp_action") == "add":
        context.user_data.pop("awaiting_grp_action", None)
        prompt_msg_id = context.user_data.pop("prompt_msg_id", None)
        prompt_chat_id = context.user_data.pop("prompt_chat_id", None) or chat.id
        try:
            new_gid = int(text)
            all_current = get_target_group_chat_ids()
            if len(all_current) >= MAX_FORWARDING_GROUPS:
                msg_text, markup = build_groups_list_view()
                limit_text = (
                    f"⚠️ <b>Maximum Group Limit Reached</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"You already have <b>{len(all_current)} / {MAX_FORWARDING_GROUPS} active groups</b>.\n"
                    f"The system allows a minimum of 1 and maximum of 10 groups.\n"
                    f"Please remove an existing group before adding a new one."
                )
                await render_prompt_response(context, prompt_chat_id, prompt_msg_id, msg_obj, limit_text, markup)
                return

            if new_gid in all_current:
                msg_text, markup = build_groups_list_view()
                already_text = (
                    f"ℹ️ <b>Group Already Linked</b>\n"
                    f"Group <code>{new_gid}</code> is already in the forwarding pool."
                )
                await render_prompt_response(context, prompt_chat_id, prompt_msg_id, msg_obj, already_text, markup)
                return

            ok, reason = add_dynamic_linked_group(new_gid)
            if not ok:
                msg_text, markup = build_groups_list_view()
                fail_text = f"⚠️ Could not add group: {reason}"
                await render_prompt_response(context, prompt_chat_id, prompt_msg_id, msg_obj, fail_text, markup)
                return

            try:
                await context.bot.delete_my_commands(scope=BotCommandScopeChat(new_gid))
            except Exception:
                pass
            msg_text, markup = build_groups_list_view()
            new_total = len(get_target_group_chat_ids())
            success_text = (
                f"✅ <b>Group Linked Successfully!</b> <code>{new_gid}</code>\n"
                f"• <b>Active Groups:</b> <code>{new_total} / {MAX_FORWARDING_GROUPS}</code> (Limit: 1 - 10)\n"
                f"• Commands wiped from group menu (0 commands).\n"
                f"• Forwarding active immediately with custom buttons."
            )
            await render_prompt_response(context, prompt_chat_id, prompt_msg_id, msg_obj, success_text, markup)
            return
        except ValueError:
            err_text = (
                "❌ <b>Invalid Group Chat ID</b>\n"
                "Must be a numerical chat ID (e.g. <code>-1004473973263</code>).\n"
                "Please send a valid chat ID or click Cancel:"
            )
            err_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="grp_list")]
            ])
            await render_prompt_response(context, prompt_chat_id, prompt_msg_id, msg_obj, err_text, err_kb)
            return

    # Check if admin was in middle of renaming, updating URL, or adding a button
    awaiting = context.user_data.get("awaiting_btn_action")
    if awaiting and isinstance(awaiting, dict):
        prompt_msg_id = context.user_data.pop("prompt_msg_id", None)
        prompt_chat_id = context.user_data.pop("prompt_chat_id", None) or chat.id
        action = awaiting.get("action")
        target_id = awaiting.get("id")
        buttons = get_custom_link_buttons()

        if action == "rename" and target_id:
            found = next((b for b in buttons if str(b["id"]) == str(target_id)), None)
            if found:
                old_name = found["text"]
                found["text"] = text
                save_custom_link_buttons(buttons)
                context.user_data.pop("awaiting_btn_action", None)
                _, detail_kb = build_button_detail_view(found["id"])
                resp_text = (
                    f"✅ <b>Button Renamed!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>Old:</b> <code>{html.escape(old_name)}</code>\n"
                    f"• <b>New:</b> <code>{html.escape(text)}</code>\n"
                    f"• <b>URL:</b> <code>{html.escape(found['url'])}</code>\n"
                    f"• <b>Synced:</b> <code>Cloud DB 🔒</code>"
                )
                await render_prompt_response(context, prompt_chat_id, prompt_msg_id, msg_obj, resp_text, detail_kb)
                return

        elif action == "reurl" and target_id:
            found = next((b for b in buttons if str(b["id"]) == str(target_id)), None)
            if found:
                clean_url = normalize_button_url(text)
                if not clean_url:
                    err_text = (
                        "❌ <b>Invalid URL or Bot Username</b>\n"
                        "Link must start with <code>https://</code>, <code>http://</code>, or be a bot username like <code>@bot_username</code>.\n"
                        "Please send a valid link or click Cancel:"
                    )
                    err_kb = InlineKeyboardMarkup([
                        [InlineKeyboardButton("❌ Cancel", callback_data=f"link_view_{found['id']}")]
                    ])
                    context.user_data["prompt_msg_id"] = prompt_msg_id
                    context.user_data["prompt_chat_id"] = prompt_chat_id
                    await render_prompt_response(context, prompt_chat_id, prompt_msg_id, msg_obj, err_text, err_kb)
                    return
                found["url"] = clean_url
                save_custom_link_buttons(buttons)
                context.user_data.pop("awaiting_btn_action", None)
                _, detail_kb = build_button_detail_view(found["id"])
                resp_text = (
                    f"✅ <b>Button URL Updated!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>Button:</b> <code>{html.escape(found['text'])}</code>\n"
                    f"• <b>New URL:</b> <code>{html.escape(clean_url)}</code>\n"
                    f"• <b>Synced:</b> <code>Cloud DB 🔒</code>"
                )
                await render_prompt_response(context, prompt_chat_id, prompt_msg_id, msg_obj, resp_text, detail_kb)
                return

        elif action == "add":
            b_name, raw_url = "", ""
            if "|" in text:
                parts = text.split("|", 1)
                b_name = parts[0].strip()
                raw_url = parts[1].strip()
            else:
                parts = text.split()
                if len(parts) >= 2:
                    raw_url = parts[-1].strip()
                    b_name = " ".join(parts[:-1]).strip()
                elif len(parts) == 1:
                    raw_url = parts[0].strip()
                    b_name = raw_url.lstrip("@")

            clean_url = normalize_button_url(raw_url)
            if not b_name or not clean_url:
                err_text = (
                    "❌ <b>Invalid Format</b>\n"
                    "Please provide button name and link or bot username separated by <code>|</code>:\n"
                    "<code>Button Name | @bot_username</code>\n"
                    "<i>or:</i> <code>Button Name | https://yourlink.com</code>\n\n"
                    "<i>Example:</i> <code>🤖 Number Bot | @my_number_bot</code>"
                )
                err_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="link_list")]
                ])
                context.user_data["prompt_msg_id"] = prompt_msg_id
                context.user_data["prompt_chat_id"] = prompt_chat_id
                await render_prompt_response(context, prompt_chat_id, prompt_msg_id, msg_obj, err_text, err_kb)
                return

            new_id = str(len(buttons) + 1)
            buttons.append({"id": new_id, "text": b_name, "url": clean_url})
            save_custom_link_buttons(buttons)
            context.user_data.pop("awaiting_btn_action", None)
            _, detail_kb = build_button_detail_view(new_id)
            resp_text = (
                f"✅ <b>New Custom Button Added!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"• <b>Label:</b> <code>{html.escape(b_name)}</code>\n"
                f"• <b>Link:</b> <code>{html.escape(clean_url)}</code>\n"
                f"• <b>Total Active Buttons:</b> <code>{len(buttons)}</code> (side-by-side in group OTPs)\n"
                f"• <b>Synced:</b> <code>Cloud DB 🔒</code>"
            )
            await render_prompt_response(context, prompt_chat_id, prompt_msg_id, msg_obj, resp_text, detail_kb)
            return

# ==========================================
# 13. Diagnostics (--test mode)
# ==========================================
async def run_diagnostics():
    print("\n=======================================================")
    print("           THIRDWAVE SMS WAVE OTP BOT SYSTEM DIAGNOSTICS")
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

    # 3. Thirdwave API (GET /api/v1/traffic)
    print("\n[3/4] Checking Thirdwave API Connection...")
    init_db()
    try:
        msgs = await client.fetch_incoming_messages(limit=5)
        print(f"  -> Connected to endpoint: {client.base_url}/api/v1/traffic")
        print(f"  -> Total recent live messages fetched: {len(msgs)}")
        if msgs:
            m = msgs[0]
            print(f"  -> Latest SMS Sample: ID {m.get('id')} | Sender: {m.get('sender_id')} | Range: {m.get('range_name')}")
            print(f"  -> Message Preview: \"{str(m.get('sms',''))[:80]}\"")
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
# 14. Main Entry Point
# ==========================================
def validate_config():
    errors = []
    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN is missing")
    else:
        try:
            r = httpx.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe", timeout=6.0)
            if r.status_code == 401 or not r.json().get("ok"):
                logger.warning("⚠️ Notice: Telegram token check returned 401 Unauthorized. Ensure token is valid from @BotFather.")
        except Exception:
            pass
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

async def trigger_proactive_handover(manual_restart: bool = False) -> bool:
    """
    Proactively dispatches the next GitHub Actions workflow run in advance
    so the incoming runner boots up, sets up Python, restores DB cache,
    and installs packages while THIS current bot continues forwarding live OTPs!
    """
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    if not repo:
        logger.info("ℹ️ Proactive handover dispatch skipped (no GITHUB_REPOSITORY).")
        return False

    candidate_tokens: List[str] = []
    # Check all candidate tokens in order: PAT (Personal Access Token), GIST_TOKEN, GH_TOKEN, GITHUB_TOKEN
    for t in [
        os.getenv("PAT", "").strip(),
        os.getenv("GIST_TOKEN", "").strip(),
        os.getenv("GH_TOKEN", "").strip(),
        os.getenv("GITHUB_TOKEN", "").strip(),
    ]:
        if t and t not in candidate_tokens:
            candidate_tokens.append(t)

    if not candidate_tokens:
        logger.info("ℹ️ Proactive handover dispatch skipped (no authentication token found).")
        return False

    # Save state, checkpoint DB, and record active timestamp BEFORE waking next runner
    update_last_active_timestamp()
    try:
        with get_db_connection() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    except Exception:
        pass
    if gist_storage.enabled:
        try:
            today_total, total_28h, active_countries = get_dashboard_stats()
            await gist_storage.save_state(
                seen_dict=seen_timestamps,
                is_handover=True,
                total_forwarded=today_total,
                country_counts=active_countries,
                base_url=client.base_url if "client" in globals() else "",
                manual_restart=manual_restart,
            )
        except Exception:
            pass

    url = f"https://api.github.com/repos/{repo}/actions/workflows/run_bot.yml/dispatches"
    active_url = client.base_url if "client" in globals() else ""
    payload = {
        "ref": "main",
        "inputs": {
            "is_handover": "true",
            "is_manual_restart": "true" if manual_restart else "false",
            "custom_base_url": active_url,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as hclient:
            for token in candidate_tokens:
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "ThirdwaveSMSWaveOTPBot",
                }
                res = await hclient.post(url, headers=headers, json=payload)
                if res.status_code in (204, 201, 200):
                    logger.info("🚀 [PROACTIVE HANDOVER] Next runner triggered in advance! Transition will have 0s downtime.")
                    try:
                        with open(".handover_dispatched", "w", encoding="utf-8") as hf:
                            hf.write(str(time.time()))
                    except Exception:
                        pass
                    return True
                else:
                    logger.warning(f"⚠️ Proactive handover with token ({token[:6]}...) returned HTTP {res.status_code}: {res.text[:100]}")
    except Exception as e:
        logger.warning(f"⚠️ Proactive handover dispatch error: {e}")
    return False

async def main():
    parser = argparse.ArgumentParser(description="Thirdwave SMS Wave OTP Bot")
    parser.add_argument("--test", action="store_true", help="Run diagnostics and exit")
    args = parser.parse_args()

    validate_config()

    if args.test:
        await run_diagnostics()
        return

    logger.info("⚡ Thirdwave SMS Wave OTP Bot starting up...")

    req = HTTPXRequest(
        connection_pool_size=16,
        read_timeout=10.0,
        write_timeout=10.0,
        connect_timeout=5.0,
        pool_timeout=2.0,
    )
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(req)
        .concurrent_updates(True)
        .build()
    )

    # ── All command & keyboard handlers: PRIVATE chat ONLY ──────────────────
    # The group is exclusively for OTP forwarding. Commands are completely
    # invisible to the group — Telegram never even dispatches them here.
    # Strictly ONLY /start is registered as a slash command. All other interactions are button-driven.
    _private = filters.ChatType.PRIVATE
    application.add_handler(CommandHandler("start", start_command, filters=_private))
    application.add_handler(CommandHandler("status", status_command, filters=_private))
    application.add_handler(CommandHandler("cancel", cancel_command, filters=_private))
    application.add_handler(CallbackQueryHandler(admin_callback_handler))
    application.add_handler(MessageHandler(_private & filters.TEXT & ~filters.COMMAND, keyboard_button_handler))

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

        # ── Gist state restore at startup ──────────────────────────────────────
        if gist_storage.enabled:
            try:
                await gist_storage.ensure_gist()
                gist_state = await gist_storage.load_state()

                # manual_restart is ONLY trusted from Gist when IS_MANUAL_RESTART env was
                # explicitly set by the bot's own dispatch (preventing stale flag re-triggering)
                if gist_state.get("manual_restart") and _manual_restart:
                    # Already set from env, flag confirmed — leave as-is
                    set_bot_setting("manual_restart", "1")
                elif gist_state.get("manual_restart") and not _manual_restart:
                    # Stale Gist flag from a previous session — clear it immediately, do NOT re-trigger
                    logger.info("\u2139\ufe0f Stale manual_restart flag found in Gist — clearing (not re-triggering alert).")
                    set_bot_setting("manual_restart", "0")
                    # Immediately clear in Gist so next session doesn't see it either
                    try:
                        today_total, total_28h, active_countries = get_dashboard_stats()
                        asyncio.create_task(gist_storage.save_state(
                            seen_dict=seen_timestamps,
                            is_handover=_is_handover,
                            total_forwarded=today_total,
                            country_counts=active_countries,
                            base_url=client.base_url if "client" in globals() else "",
                            manual_restart=False,
                        ))
                    except Exception:
                        pass

                # Restore last_notified_commit so commit-change detection works across sessions
                gist_last_commit = gist_state.get("last_notified_commit")
                if gist_last_commit:
                    set_bot_setting("last_notified_commit", gist_last_commit)

                # Pre-restore bot configuration settings from cloud database BEFORE tasks start
                saved_bot_data = gist_state.get("bot_data")
                if isinstance(saved_bot_data, dict) and saved_bot_data:
                    local_data = load_stored_data()
                    if saved_bot_data.get("custom_link_buttons_cleared") or saved_bot_data.get("custom_link_buttons") == []:
                        local_data["custom_link_buttons"] = []
                        local_data["custom_link_buttons_cleared"] = True
                        local_data.pop("custom_link_button", None)
                        delete_bot_setting("custom_link_button")
                        delete_bot_setting("custom_link_button_url")
                        delete_bot_setting("custom_link_button_text")
                        set_bot_setting("custom_link_buttons", "[]")
                        set_bot_setting("custom_link_buttons_cleared", "1")
                    # Restore custom API URL and Key if configured by admin
                    custom_url = saved_bot_data.get("thirdwave_base_url") or saved_bot_data.get("base_url")
                    if custom_url and "client" in globals():
                        client.update_base_url(custom_url)
                        set_bot_setting("thirdwave_base_url", custom_url)
                    custom_key = saved_bot_data.get("thirdwave_api_key") or saved_bot_data.get("api_key")
                    if custom_key and "client" in globals():
                        client.update_api_key(custom_key)
                        set_bot_setting("thirdwave_api_key", custom_key)
                    local_data.update(saved_bot_data)
                    save_stored_data(local_data)
            except Exception as e:
                logger.debug(f"Initial Gist check notice: {e}")

        create_safe_task(send_startup_announcement(application))
        create_safe_task(poll_incoming_messages(application))
        create_safe_task(periodic_db_cleanup_loop())
        create_safe_task(periodic_gist_sync_loop())
        async def background_menu_setup():
            try:
                # 1. Fetch & cache Telegram bot name
                try:
                    me = await application.bot.get_me()
                    if me and me.first_name:
                        set_cached_telegram_name(me.first_name)
                except Exception:
                    pass

                # 2. Lock command list and [≡ Menu] button permanently on Default and Private scopes (Zero flickering)
                bot_cmds = get_bot_commands()
                try:
                    await application.bot.set_my_commands(bot_cmds, scope=BotCommandScopeDefault())
                    await application.bot.set_my_commands(bot_cmds, scope=BotCommandScopeAllPrivateChats())
                    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
                except Exception as cmd_err:
                    logger.debug(f"Default commands register notice: {cmd_err}")

                # 3. Wipe commands from groups ONLY (groups will have 0 commands and default menu)
                try:
                    await application.bot.set_my_commands([], scope=BotCommandScopeAllGroupChats())
                except Exception:
                    pass

                for gid in get_target_group_chat_ids():
                    if gid and gid < 0:
                        try:
                            await application.bot.set_my_commands([], scope=BotCommandScopeChat(gid))
                            await application.bot.set_chat_menu_button(chat_id=gid, menu_button=MenuButtonDefault())
                        except Exception:
                            pass

                for aid in get_all_admin_ids():
                    try:
                        await application.bot.set_my_commands(bot_cmds, scope=BotCommandScopeChat(aid))
                        await application.bot.set_chat_menu_button(chat_id=aid, menu_button=MenuButtonCommands())
                    except Exception:
                        pass
                logger.info("✅ Telegram Bot Command Menu: Permanently active in private chat (Zero flickering); groups wiped (0 commands).")
            except Exception as e:
                logger.warning(f"Error configuring command menu scopes: {e}")

        create_safe_task(background_menu_setup())
        logger.info(f"✅ {get_bot_name()} is fully online and monitoring incoming messages...")

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

        _proactive_handover_triggered = False

        while True:
            try:
                now = time.time()
                elapsed = now - start_time

                # ── Proactive Handover: trigger next runner 4 min before session ends ──
                if session_timeout > 0 and elapsed >= (session_timeout - 240) and not _proactive_handover_triggered:
                    _proactive_handover_triggered = True
                    logger.info("🔄 Proactive handover window reached: waking up next runner in advance now...")
                    await trigger_proactive_handover()

                # ── Clean Session Exit: 45s before runner hard limit ─────────────────
                if session_timeout > 0 and elapsed >= (session_timeout - 45):
                    logger.info(f"⏱️ Session limit approaching ({elapsed:.0f}s elapsed of {session_timeout}s).")
                    logger.info("🔄 Pre-timeout analysis: saving workflow state to Gist & SQLite for seamless handover...")
                    update_last_active_timestamp()
                    try:
                        with get_db_connection() as conn:
                            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                    except Exception as e:
                        logger.warning(f"DB checkpoint notice: {e}")

                    if gist_storage.enabled:
                        today_total, total_28h, active_countries = get_dashboard_stats()
                        await gist_storage.save_state(
                            seen_dict=seen_timestamps,
                            is_handover=True,
                            total_forwarded=today_total,
                            country_counts=active_countries,
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
            if "client" in globals():
                await client.close()
        except Exception:
            pass
        update_last_active_timestamp()
        try:
            with get_db_connection() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception:
            pass
        if gist_storage.enabled:
            today_total, total_28h, active_countries = get_dashboard_stats()
            near_timeout = session_timeout > 0 and (time.time() - start_time) >= (session_timeout - 120)
            await gist_storage.save_state(
                seen_dict=seen_timestamps,
                is_handover=near_timeout or _is_handover,
                total_forwarded=today_total,
                country_counts=active_countries,
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
