# 🌊 THIRDWAVE OTP WAVE Bot — 24/7 Hosting & Setup Guide

Standalone, high-performance Telegram bot that receives A2P OTP SMS from the **Thirdwave IPRN panel** and forwards them to your Telegram groups in real-time with **persistent SQLite database (`bot1_database.db`)** and **28-hour cloud memory (`thirdwave_seen_messages.json`)**.

---

## 📁 Files in This Folder

| File | Description |
| :--- | :--- |
| [`bot.py`](file:///c:/thirdwave%20bot/1_THIRDWAVE_BOT/bot.py) | Self-contained single script (auto-installs dependencies, 28h memory) |
| [`.github/workflows/run_bot.yml`](file:///c:/thirdwave%20bot/1_THIRDWAVE_BOT/.github/workflows/run_bot.yml) | 24/7 GitHub Actions always-online runner (zero-downtime loop) |
| [`PUSH_TO_GITHUB.bat`](file:///c:/thirdwave%20bot/1_THIRDWAVE_BOT/PUSH_TO_GITHUB.bat) | 1-Click push script for PC (pushes ONLY bot.py + workflow to GitHub) |
| [`START_BOT.bat`](file:///c:/thirdwave%20bot/1_THIRDWAVE_BOT/START_BOT.bat) | Run bot locally with auto-restart on crash |
| [`TEST_BOT.bat`](file:///c:/thirdwave%20bot/1_THIRDWAVE_BOT/TEST_BOT.bat) | Run complete connection & system diagnostics |
| [`bot1_database.db`](file:///c:/thirdwave%20bot/1_THIRDWAVE_BOT/bot1_database.db) | Dedicated SQLite database for Bot 1 (persisted across restarts) |
| [`.env`](file:///c:/thirdwave%20bot/1_THIRDWAVE_BOT/.env) | Local environment variables & secrets |

---

## 🔑 GitHub Secrets Configuration (For 24/7 Server Hosting)

Repository: 👉 **[https://github.com/HimelKhan006/KKH-OTP-BOT2](https://github.com/HimelKhan006/KKH-OTP-BOT2)**

Go to: **Settings ➔ Secrets and variables ➔ Actions ➔ New repository secret**

### 1. Required Secrets

| Secret Name | Example Value | Description |
| :--- | :--- | :--- |
| `TELEGRAM_BOT_TOKEN` | `8897218550:AAF4-N84Bu...` | Telegram Bot token from [@BotFather](https://t.me/BotFather) |
| `THIRDWAVE_API_KEY` | `tw_live_15b7e38c...` | Thirdwave Live API key |
| `GIST_TOKEN` | `ghp_yourPersonalAccessToken...` | GitHub Token with `gist` scope *(powers 28h memory)* |
| `TELEGRAM_GROUP_CHAT_ID` | `-1004473973263` | Primary Telegram Group Chat ID |

### 2. Optional Secrets

| Secret Name | Example Value | Description |
| :--- | :--- | :--- |
| `SECONDARY_GROUP_CHAT_ID` | `-1003597354059` | Secondary Telegram Group ID for dual forwarding |
| `ADMIN_USER_IDS` | `6798979733` | Telegram Admin User ID (receives restart alerts via private DM) |
| `GIST_ID` | `abc123def456...` | Gist ID *(Optional — bot auto-creates or auto-discovers Gist)* |
| `POLL_INTERVAL_SECONDS` | `2.0` | Polling speed in seconds |
| `THIRDWAVE_BASE_URL` | `https://app.thirdwave.im` | Thirdwave API base URL |

---

## ☁️ How to Generate `GIST_TOKEN` (1-Minute Guide)

1. Open GitHub: **[https://github.com/settings/tokens/new](https://github.com/settings/tokens/new)**
2. Set **Note:** `OTP_BOT_STORAGE`
3. Set **Expiration:** `No expiration` (or desired timeframe)
4. Under **Select scopes**, check only: ✅ **`gist`** (Create gists)
5. Scroll to the bottom and click **Generate token**.
6. Copy the token and save it as the **`GIST_TOKEN`** secret in your GitHub repository!

> 💡 **Automatic Gist Management & Deduplication:**
>
> - You do NOT need to create a Gist manually.
> - The bot automatically searches for its existing Gist (`thirdwave_seen_messages.json`), reuses it, and **automatically deletes any duplicate Gists**.
> - Pushing code updates never deletes or resets your database or Gist memory!

---

## 📱 Mobile Phone Setup & Upload Guide (No PC Required)

You can upload bot files, configure secrets, and start the 24/7 bot directly from your **mobile phone browser** (Chrome / Safari / Firefox):

### 1. How to Upload and Edit bot.py from Mobile

1. Open your repository on mobile: **[https://github.com/HimelKhan006/KKH-OTP-BOT2](https://github.com/HimelKhan006/KKH-OTP-BOT2)**
2. Tap on **`bot.py`**.
3. Tap the **✏️ (Pencil icon)** at the top right of the file.
4. Select all text, delete, and paste your updated `bot.py` code.
5. Scroll to the bottom and tap **`Commit changes...`** ➔ **`Commit changes`**.
6. *(Alternative)*: Tap **`Add file`** ➔ **`Upload files`** ➔ select `bot.py` from your phone's file manager ➔ Tap **`Commit changes`**.

### 2. How to Add GitHub Secrets from Mobile

1. In your repository, tap **`Settings`** (if hidden, enable "Desktop site" in your mobile browser menu).
2. Tap **`Secrets and variables`** ➔ **`Actions`**.
3. Tap the green **`New repository secret`** button.
4. Enter `TELEGRAM_BOT_TOKEN`, `THIRDWAVE_API_KEY`, `GIST_TOKEN`, and `TELEGRAM_GROUP_CHAT_ID`.

### 3. How to Start the Bot from Mobile

1. In your repository, tap the **`Actions`** tab.
2. Tap **`OTP Wave 24/7 Always-Online Bot Runner`** on the left menu.
3. Tap the **`Run workflow`** dropdown ➔ Tap the green **`Run workflow`** button.
4. The bot will start immediately in the cloud and run 24/7 even if your phone is turned off! 🟢

---

## 💻 Running & Deploying from PC

- **1-Click Push from PC:** Double-click `PUSH_TO_GITHUB.bat`
- **Run Diagnostics Locally:** Double-click `TEST_BOT.bat`
- **Start Bot Locally:** Double-click `START_BOT.bat`

---

## ⏰ 24-Hour Restart Schedule

This bot runs on a **single 24-hour scheduled cycle** — it restarts once every day at **00:00 UTC** automatically.

| Schedule | Time | Description |
| :--- | :--- | :--- |
| Daily Restart | `00:00 UTC` | Bot automatically restarts once per day |
| Uptime | ~24 hours | Full continuous uptime between restarts |
| Alert | Admin DM only | 🔄 Restart notification sent privately to admin |

> ✅ **No spam to groups.** All restart notifications go **only** to the Admin's private Telegram DM.

---

## 🔔 Delivery & Notification Flow

| Event | Destination | Description |
| :--- | :--- | :--- |
| **Incoming OTP** | All Linked Telegram Groups | Real-time OTP notification with country, number & code |
| **Bot Restart (Daily 00:00 UTC)** | Admin Private DM Only | 🔄 Restart alert with status, platform & cycle info |
| **Manual Bot Boot** | Admin Private DM Only | 🔄 Online alert with status & platform name |
