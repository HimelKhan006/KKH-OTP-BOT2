# ⚡ THIRDWAVE OTP WAVE Bot — 24/7 Hosting & Setup Guide

Standalone, high-performance Telegram bot that receives A2P OTP SMS from the **Thirdwave IPRN panel** and forwards them to your Telegram groups in real-time with **zero-restart engine**, **28-hour cloud memory**, and **seamless session handover** — no interruptions, no restart messages.

---

## 📁 Files in This Folder

| File | Description |
| :--- | :--- |
| `bot.py` | Self-contained single script (zero-restart, 28h memory, auto-installs dependencies) |
| `.github/workflows/run_bot.yml` | 24/7 GitHub Actions runner (5h 25min sessions, instant self-trigger, zero downtime) |
| `PUSH_TO_GITHUB.bat` | 1-Click push script for PC (pushes ONLY bot.py + workflow to GitHub) |
| `START_BOT.bat` | Run bot locally with auto-restart on crash |
| `TEST_BOT.bat` | Run complete connection & system diagnostics |
| `bot1_database.db` | Dedicated SQLite database (persisted across all sessions via cache) |
| `.env` | Local environment variables & secrets |

---

## 🔒 GitHub Secrets Configuration (For 24/7 Server Hosting)

Go to: **Settings → Secrets and variables → Actions → New repository secret**

### 1. Required Secrets

| Secret Name | Example Value | Description |
| :--- | :--- | :--- |
| `TELEGRAM_BOT_TOKEN` | `8897218550:AAF4-N84Bu...` | Telegram Bot token from [@BotFather](https://t.me/BotFather) |
| `THIRDWAVE_API_KEY` | `tw_live_15b7e38c...` | Thirdwave Live API key |
| `GIST_TOKEN` | `ghp_yourPersonalAccessToken...` | GitHub Token with `gist` scope *(required for zero-restart 28h memory)* |
| `TELEGRAM_GROUP_CHAT_ID` | `-1004473973263` | Primary Telegram Group Chat ID |

### 2. Optional Secrets

| Secret Name | Example Value | Description |
| :--- | :--- | :--- |
| `SECONDARY_GROUP_CHAT_ID` | `-1003597354059` | Secondary Telegram Group ID for dual forwarding |
| `ADMIN_USER_IDS` | `6798979733` | Telegram Admin User ID (receives startup alerts via private DM) |
| `GIST_ID` | `abc123def456...` | Gist ID *(Optional — bot auto-creates or auto-discovers Gist)* |
| `POLL_INTERVAL_SECONDS` | `2.0` | Polling speed in seconds |
| `THIRDWAVE_BASE_URL` | `https://clients.thirdwave.im` | Thirdwave API base URL |

---

## ⏱️ How to Generate `GIST_TOKEN` (1-Minute Guide)

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

You can upload bot files, configure secrets, and start the 24/7 bot directly from your **mobile phone browser**:

### 1. How to Upload bot.py from Mobile

1. Open your repository on mobile.
2. Tap on **`bot.py`**.
3. Tap the **✏️ (Pencil icon)** at the top right of the file.
4. Select all text, delete, and paste your updated `bot.py` code.
5. Scroll to the bottom and tap **`Commit changes...`** → **`Commit changes`**.

### 2. How to Add GitHub Secrets from Mobile

1. In your repository, tap **`Settings`** (if hidden, enable "Desktop site" in your browser menu).
2. Tap **`Secrets and variables`** → **`Actions`**.
3. Tap the green **`New repository secret`** button.
4. Enter `TELEGRAM_BOT_TOKEN`, `THIRDWAVE_API_KEY`, `GIST_TOKEN`, and `TELEGRAM_GROUP_CHAT_ID`.

### 3. How to Start the Bot from Mobile

1. In your repository, tap the **`Actions`** tab.
2. Tap **`OTP Wave 24/7 Always-Online Bot Runner`** on the left menu.
3. Tap the **`Run workflow`** dropdown → Tap the green **`Run workflow`** button.
4. The bot starts immediately in the cloud and runs 24/7 forever! 🌐

---

## 🖥️ Running & Deploying from PC

- **1-Click Push from PC:** Double-click `PUSH_TO_GITHUB.bat`
- **Run Diagnostics Locally:** Double-click `TEST_BOT.bat`
- **Start Bot Locally:** Double-click `START_BOT.bat`

---

## ♾️ Zero-Restart Engine (How It Works)

This bot uses a **professional zero-restart handover system** — it never shows restart messages and never drops OTPs during session switches.

| Stage | What Happens |
| :--- | :--- |
| **Session Running** | Bot polls live OTPs every 2 seconds, 24/7 |
| **60s Before Timeout** | Bot saves full state (seen IDs, counts, countries) to GitHub Gist with `handover=true` |
| **Clean Exit** | Bot exits with code 0 — workflow immediately triggers next session |
| **New Session Starts** | Bot reads Gist, restores all state, silently continues — zero messages |
| **OTP Gap Recovery** | Any OTPs received during the brief runner switch are delivered on startup |

> ✅ **No restart messages.** Admin is never spammed during routine handovers.
> ✅ **No missed OTPs.** Any OTP received during session switch is caught and delivered.
> ✅ **Counts accumulate.** `/start` shows cumulative totals across all sessions.

### Session Schedule

| Setting | Value | Description |
| :--- | :--- | :--- |
| Session Length | 5h 25min (19,500s) | Each runner session before handover |
| Handover Time | ~1–3 min | Gap between session end and new session start |
| Backup Cron | Every 6h | Failsafe if self-trigger ever fails |

---

## 🔔 Delivery & Notification Flow

| Event | Destination | Description |
| :--- | :--- | :--- |
| **Incoming OTP** | All Linked Telegram Groups | Real-time OTP notification with country, number & code |
| **Routine Handover (every 5h 25min)** | *Silent — no message* | 🤫 Zero-restart engine handles silently |
| **Initial Deployment / Push** | Admin Private DM Only | 🔔 One-time online alert sent only when ADMIN_STARTUP_ALERT=true |

---

## 🌐 Changing the API Base URL

### Method 1 — Telegram Command (Instant, No Restart Required)

Send from your **Admin Telegram account**:

```bash
/seturl https://clients.thirdwave.im
```

- ✅ Takes effect **immediately** — no restart needed
- ✅ Persists across all sessions via `bot_data.json`

To view the current active URL:

```bash
/seturl
```

### Method 2 — GitHub Secret `THIRDWAVE_BASE_URL` (Permanent)

1. Open: **Repository → Settings → Secrets and variables → Actions**
2. Find or create `THIRDWAVE_BASE_URL`
3. Set value to your URL
4. Re-run the workflow for the change to take effect

### Priority Order

| Priority | Source | Notes |
| :--- | :--- | :--- |
| 1st | `/seturl` Telegram command | Overrides everything, instant |
| 2nd | `THIRDWAVE_BASE_URL` GitHub Secret | Permanent cloud override |
| 3rd | `.env` file | Local override |
| 4th | Built-in default | `https://clients.thirdwave.im` |

---

## 📊 Admin Commands

| Command | Description |
| :--- | :--- |
| `/start` | Live bot status, session uptime, next handover countdown, OTP stats |
| `/status` | Same as `/start` |
| `/seturl` | View or update the Thirdwave API base URL |
| `/set_icon <service> <url>` | Set a real app logo image URL for a service (e.g. `WhatsApp`, `Telegram`) |
| `/list_icons` | View all configured service logo icons |
| `/remove_icon <service>` | Remove a configured icon for a service |
| `/test` | Send a test OTP notification to all connected groups (verifies full delivery pipeline) |
